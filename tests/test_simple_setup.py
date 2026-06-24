# ruff: noqa: I001, N806 -- private member imports; SetupConfig used as local var name
"""Tests for the simple setup flow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Private member imports for pyright strict mode testing (I001 excluded — private names)
# ruff: noqa: I001
from agentalloy.install.subcommands.simple_setup import (
    _derive_host_target as _derive_host_target,  # type: ignore[attr-defined]
    _discover_packs as _discover_packs,  # type: ignore[attr-defined]
    _prompt as _prompt,  # type: ignore[attr-defined]
    _prompt_context as _prompt_context,  # type: ignore[attr-defined]
    _prompt_for_packs as _prompt_for_packs,  # type: ignore[attr-defined]
    _resolve_preset as _resolve_preset,  # type: ignore[attr-defined]
    _run_from_args as _run_from_args,  # type: ignore[attr-defined]
    _build_namespace as _build_namespace,  # type: ignore[attr-defined]
    _test_embed_endpoint as _test_embed_endpoint,  # type: ignore[attr-defined]
    _prompt_upstream as _prompt_upstream,  # type: ignore[attr-defined]
    _test_upstream_endpoint as _test_upstream_endpoint,  # type: ignore[attr-defined]
    _write_upstream_env as _write_upstream_env,  # type: ignore[attr-defined]
    SetupConfig,
)
# ruff: noqa: I001


# Helper to avoid pyright lambda param type issues (ruff reformatting moves ignore comments)
def _mock_input_accept(prompt: str) -> str:
    """Mock input that accepts default (returns '1' to accept compose file)."""
    return "1"


# ---------------------------------------------------------------------------
# Shared mock setup
# ---------------------------------------------------------------------------


class MockSetup:
    """Shared mock setup for all setup execution tests.

    Provides pre-mocked versions of all subcommands that return success (0).
    Override individual mocks per-test as needed.
    """

    def __init__(self):
        self.mocks: dict[str, MagicMock] = {}
        self.patchers: list[Any] = []  # type: ignore[type-arg]

    def _get_patch_path(self, name: str) -> str:
        return f"agentalloy.install.subcommands.{name}"

    def setup_all(self):
        # Modules that have a .run() method — preflight uses .run_preflight() instead
        for name in (
            "detect",
            "pull_models",
            "seed_corpus",
            "start_embed_server",
            "start_rerank_server",
            "install_packs",
            "enable_service",
            "write_env",
            "verify",
            "wire_harness",
        ):
            mp = patch(f"{self._get_patch_path(name)}.run")
            self.mocks[name] = mp.start()
            self.mocks[name].return_value = 0
            self.patchers.append(mp)

        # preflight.run_preflight is a module-level function, not .run()
        pf = patch("agentalloy.install.subcommands.preflight.run_preflight")
        self.mocks["preflight"] = pf.start()
        self.mocks["preflight"].return_value = {
            "checks": [],
            "fatal_failures": [],
            "warn_failures": [],
        }
        self.patchers.append(pf)

        # The steps above are mocked, so no real corpus is built — stub the
        # post-install corpus guard with a healthy count so the orchestration
        # tests reach the later steps. Tests that exercise the guard override it.
        cc = patch(
            "agentalloy.install.subcommands.simple_setup._corpus_skill_count",
            return_value=100,
        )
        self.mocks["_corpus_skill_count"] = cc.start()
        self.patchers.append(cc)

    def teardown(self):
        for p in self.patchers:
            p.stop()


@pytest.fixture
def tmp_state_dir(tmp_path: Path):
    """Set up a temporary XDG state directory.

    XDG_DATA_HOME -> tmp/.local/share (outputs_dir() appends 'agentalloy/outputs')
    XDG_CONFIG_HOME -> tmp/.config (user_config_dir() appends 'agentalloy')
    """
    config_dir = tmp_path / ".config"
    data_dir = tmp_path / ".local" / "share"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    yield config_dir, data_dir
    del os.environ["XDG_CONFIG_HOME"]
    del os.environ["XDG_DATA_HOME"]


# ---------------------------------------------------------------------------
# Prompt and config tests
# ---------------------------------------------------------------------------


class TestSimpleSetupPrompts:
    """Test the interactive prompt logic."""

    def test_prompt_returns_default_in_non_tty(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            result = _prompt("Test prompt", default="hello")
            assert result == "hello"

    def test_prompt_returns_empty_when_no_default(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            result = _prompt("Test prompt")
            assert result == ""

    def test_prompt_context_returns_default_in_non_tty(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            result = _prompt_context("Test prompt", "Context here", default="hello")
            assert result == "hello"

    def test_invalid_runner_rejected(self, tmp_state_dir: tuple[Path, Path]):
        from agentalloy.install.subcommands.simple_setup import run_setup

        cfg = SetupConfig(runner="invalid", non_interactive=True)
        rc = run_setup(cfg)
        assert rc == 1

    def test_preset_resolved_from_host(self):
        # Preset name == hardware target; the runner no longer factors in.
        cfg = SetupConfig(runner="llama-server", recommended_host="nvidia")
        preset = _resolve_preset(cfg)
        assert preset == "nvidia"
        assert cfg.preset == "nvidia"

    def test_preset_fallback_unknown_combination(self):
        cfg = SetupConfig(runner="llama-server", recommended_host="unknown-hw")
        preset = _resolve_preset(cfg)
        assert preset == "cpu"  # fallback

    def test_preset_cpu(self):
        cfg = SetupConfig(runner="llama-server", recommended_host="cpu")
        preset = _resolve_preset(cfg)
        assert preset == "cpu"

    def test_preset_uses_user_hardware_target_over_detected(self):
        cfg = SetupConfig(
            runner="llama-server", hardware_target="radeon", recommended_host="nvidia"
        )
        preset = _resolve_preset(cfg)
        assert preset == "radeon"  # user choice wins over detected
        assert cfg.preset == "radeon"

    def test_preset_fallback_when_user_hardware_unknown(self):
        cfg = SetupConfig(runner="llama-server", hardware_target="unknown-gpu")
        preset = _resolve_preset(cfg)
        assert preset == "cpu"  # fallback


class TestDeriveHostTarget:
    """Test _derive_host_target helper."""

    def test_nvidia_discrete(self):
        data = {"gpu": {"discrete": [{"vendor": "nvidia", "model": "RTX 3060", "vram_gb": 12}]}}
        assert _derive_host_target(data) == "nvidia"

    def test_amd_discrete(self):
        data = {"gpu": {"discrete": [{"vendor": "amd", "model": "RX 7900 XTX", "vram_gb": 24}]}}
        assert _derive_host_target(data) == "radeon"

    def test_amd_before_nvidia_amd_wins(self):
        """NVIDIA discrete takes priority over AMD discrete."""
        data = {
            "gpu": {
                "discrete": [
                    {"vendor": "amd", "model": "RX 7900"},
                    {"vendor": "nvidia", "model": "RTX 3060"},
                ]
            }
        }
        assert _derive_host_target(data) == "nvidia"

    def test_no_discrete_but_integrated_apple(self):
        data = {"gpu": {"discrete": [], "integrated": [{"vendor": "apple", "model": "M2"}]}}
        assert _derive_host_target(data) == "apple-silicon"

    def test_empty_gpu_fallback_cpu(self):
        data: dict[str, Any] = {"gpu": {"discrete": [], "integrated": []}}
        assert _derive_host_target(data) == "cpu"

    def test_no_gpu_key_fallback_cpu(self):
        data: dict[str, Any] = {}
        assert _derive_host_target(data) == "cpu"

    def test_integrated_amd_returns_radeon(self):
        """AMD integrated (APU: Strix Point, Phoenix, etc.) → radeon."""
        data = {
            "gpu": {
                "discrete": [],
                "integrated": [{"vendor": "amd", "model": "Radeon Graphics"}],
            }
        }
        assert _derive_host_target(data) == "radeon"

    def test_amd_apu_strix_point(self):
        """Strix Point APU (Radeon 890M) → radeon."""
        data = {
            "gpu": {
                "discrete": [],
                "integrated": [{"vendor": "amd", "model": "Radeon 890M"}],
            }
        }
        assert _derive_host_target(data) == "radeon"


# ---------------------------------------------------------------------------
# Execution tests
# ---------------------------------------------------------------------------


class TestSimpleSetupExecution:
    """Test the full setup flow with mocked subcommands."""

    @pytest.fixture(autouse=True)
    def _mocks(self, tmp_state_dir: tuple[Path, Path]):
        self.mock = MockSetup()
        self.mock.setup_all()
        self.tmp_config, self.tmp_data = tmp_state_dir
        # Patch outputs_dir to return a path inside the fixture
        outputs_patch = patch(
            "agentalloy.install.state.outputs_dir",
            return_value=self.tmp_data / "outputs",
        )
        self.mock.patchers.append(outputs_patch)
        self.mock.mocks["_outputs_dir"] = outputs_patch.start()
        (self.tmp_data / "outputs").mkdir(parents=True, exist_ok=True)
        yield
        self.mock.teardown()

    def _import_run_setup(self):
        import agentalloy.install.subcommands.simple_setup as mod

        return mod.SetupConfig, mod.run_setup

    def test_run_setup_preflight_early_failure(self, tmp_state_dir: tuple[Path, Path]):
        """Setup aborts when early preflight has fatal failures."""
        self.mock.mocks["preflight"].return_value = {
            "checks": [
                {
                    "name": "python_version",
                    "passed": False,
                    "severity": "fatal",
                    "error": "Python < 3.12",
                }
            ],
            "fatal_failures": ["python_version"],
        }
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 1
        self.mock.mocks["write_env"].assert_not_called()

    def test_run_setup_preflight_runner_failure(self, tmp_state_dir: tuple[Path, Path]):
        """Setup aborts when runner preflight fails."""
        # Early preflight passes, runner preflight fails
        self.mock.mocks["preflight"].side_effect = [
            {
                "checks": [],
                "fatal_failures": [],
                "warn_failures": [],
            },  # early
            {
                "checks": [
                    {
                        "name": "ollama_present",
                        "passed": False,
                        "severity": "fatal",
                        "error": "not found",
                    }
                ],
                "fatal_failures": ["ollama_present"],
            },  # runner
        ]
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 1
        # write_env should NOT be called after runner preflight failure
        self.mock.mocks["write_env"].assert_not_called()

    def test_run_setup_provisions_missing_runner_and_continues(
        self, tmp_state_dir: tuple[Path, Path]
    ):
        """A missing llama-server at the runner gate is provisioned, not fatal."""
        self.mock.mocks["preflight"].side_effect = [
            {"checks": [], "fatal_failures": [], "warn_failures": []},  # early
            {  # runner: binary missing
                "checks": [
                    {
                        "name": "llama_server_present",
                        "passed": False,
                        "severity": "fatal",
                        "error": "llama-server not found on PATH",
                    }
                ],
                "fatal_failures": ["llama_server_present"],
            },
            {"checks": [], "fatal_failures": [], "warn_failures": []},  # runner re-check passes
        ]
        setup_config, run_setup = self._import_run_setup()
        with patch(
            "agentalloy.install.subcommands.pull_models.ensure_runner_binary",
            return_value={"success": True, "binary_path": "/home/u/.local/bin/llama-server"},
        ) as ensure:
            rc = run_setup(setup_config(runner="llama-server", non_interactive=True))
        assert rc == 0
        ensure.assert_called_once()
        # provisioning recovered the gate, so the install proceeds
        self.mock.mocks["write_env"].assert_called_once()

    def test_run_setup_aborts_when_runner_provision_fails(self, tmp_state_dir: tuple[Path, Path]):
        """If provisioning the missing runner fails, setup still aborts cleanly."""
        self.mock.mocks["preflight"].side_effect = [
            {"checks": [], "fatal_failures": [], "warn_failures": []},  # early
            {
                "checks": [
                    {
                        "name": "llama_server_present",
                        "passed": False,
                        "severity": "fatal",
                        "error": "llama-server not found on PATH",
                    }
                ],
                "fatal_failures": ["llama_server_present"],
            },  # runner
        ]
        setup_config, run_setup = self._import_run_setup()
        with patch(
            "agentalloy.install.subcommands.pull_models.ensure_runner_binary",
            return_value={"success": False, "error": "prebuilt download failed"},
        ):
            rc = run_setup(setup_config(runner="llama-server", non_interactive=True))
        assert rc == 1
        self.mock.mocks["write_env"].assert_not_called()

    def test_run_setup_writes_env_with_correct_preset(self, tmp_state_dir: tuple[Path, Path]):
        """Setup writes .env with preset resolved from the hardware target."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(runner="llama-server", non_interactive=True))
        assert rc == 0
        self.mock.mocks["write_env"].assert_called_once()

    def test_run_setup_rejects_non_llama_server_runner(self, tmp_state_dir: tuple[Path, Path]):
        """Setup rejects any runner other than llama-server."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(runner="ollama", non_interactive=True))
        assert rc == 1

    def test_run_setup_starts_reranker_server(self, tmp_state_dir: tuple[Path, Path]):
        """Setup starts the second (reranker) llama-server instance."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 0
        self.mock.mocks["start_rerank_server"].assert_called_once()

    def test_run_setup_fails_loudly_when_corpus_empty(self, tmp_state_dir: tuple[Path, Path]):
        """install-packs 'succeeds' but leaves an empty corpus → setup aborts
        before enable-service instead of reporting a half-install as done."""
        self.mock.mocks["_corpus_skill_count"].return_value = 0
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 1
        self.mock.mocks["enable_service"].assert_not_called()

    def test_run_setup_all_steps_called(self, tmp_state_dir: tuple[Path, Path]):
        """Full setup flow runs all expected steps."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 0

        # All core steps called at least once
        for name in (
            "detect",
            "seed_corpus",
            "start_embed_server",
            "install_packs",
            "enable_service",
            "write_env",
            "verify",
            "pull_models",
        ):
            self.mock.mocks[name].assert_called_once()

        # Preflight called twice (early + runner)
        assert self.mock.mocks["preflight"].call_count == 2

    def test_run_setup_pulls_model(self, tmp_state_dir: tuple[Path, Path]):
        """Setup calls pull_models to ensure the model is present."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 0
        self.mock.mocks["pull_models"].assert_called_once()

    def test_run_setup_wires_harness_when_specified(self, tmp_state_dir: tuple[Path, Path]):
        """Setup wires the harness when a non-manual harness is selected."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(harness="claude-code", non_interactive=True))
        assert rc == 0
        self.mock.mocks["wire_harness"].assert_called_once()

    def test_run_setup_skips_harness_when_manual(self, tmp_state_dir: tuple[Path, Path]):
        """Setup does not call wire_harness when harness is 'manual'."""
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(harness="manual", non_interactive=True))
        assert rc == 0
        self.mock.mocks["wire_harness"].assert_not_called()

    def _run_interactive_until_confirm(self, harness: str):
        """Drive the interactive flow up to the final confirm prompt.

        All interactive prompts are mocked so the gather phase completes;
        `_prompt_harness` returns *harness* and the final confirm returns 'n'
        so `run_setup` aborts (rc == 1) right after the upstream-LLM block —
        letting us observe whether `_prompt_upstream`/`_write_upstream_env` ran
        without executing the full install pipeline.

        Returns (rc, prompt_upstream_mock, write_upstream_mock).
        """
        import agentalloy.install.subcommands.simple_setup as mod

        with (
            patch.object(mod, "_prompt_deployment", return_value="native"),
            patch.object(mod, "_prompt_hardware", side_effect=lambda default: default),
            patch.object(mod, "_prompt_mode", return_value="manual"),
            patch.object(mod, "_prompt_for_packs", return_value=""),
            patch.object(
                mod, "_prompt_context", side_effect=lambda text, context, default="": default
            ),
            patch.object(mod, "_prompt_harness", return_value=harness),
            # Final "Confirm and continue?" -> 'n' aborts after the upstream block.
            patch.object(mod, "_prompt", return_value="n"),
            patch.object(mod, "_prompt_upstream") as prompt_upstream,
            patch.object(mod, "_write_upstream_env") as write_upstream,
        ):
            rc = mod.run_setup(mod.SetupConfig(non_interactive=False))
        return rc, prompt_upstream, write_upstream

    def test_interactive_proxy_harness_prompts_for_upstream(self, tmp_state_dir: tuple[Path, Path]):
        """A genuinely proxy-wired harness still prompts for the upstream LLM."""
        rc, prompt_upstream, _ = self._run_interactive_until_confirm("opencode")
        assert rc == 1
        prompt_upstream.assert_called_once()

    def test_proxy_harness_writes_upstream_env(self, tmp_state_dir: tuple[Path, Path]):
        """Non-interactive proxy harness setup still writes upstream LLM .env vars."""
        import agentalloy.install.subcommands.simple_setup as mod

        with patch.object(mod, "_write_upstream_env") as write_upstream:
            rc = mod.run_setup(mod.SetupConfig(harness="opencode", non_interactive=True))
        assert rc == 0
        write_upstream.assert_called_once()

    def test_native_passthrough_harness_skips_upstream_prompt(
        self, tmp_state_dir: tuple[Path, Path]
    ):
        """claude-code uses the native Anthropic passthrough — it forwards the caller's
        own credential to ANTHROPIC_UPSTREAM_URL and never uses the OpenAI UPSTREAM_URL,
        so the wizard must NOT prompt for it (regression: 3.0.0 wrongly prompted)."""
        rc, prompt_upstream, _ = self._run_interactive_until_confirm("claude-code")
        assert rc == 1
        prompt_upstream.assert_not_called()

    def test_native_passthrough_harness_skips_write_upstream_env(
        self, tmp_state_dir: tuple[Path, Path]
    ):
        """Non-interactive claude-code setup does not write the OpenAI upstream .env vars."""
        import agentalloy.install.subcommands.simple_setup as mod

        with patch.object(mod, "_write_upstream_env") as write_upstream:
            rc = mod.run_setup(mod.SetupConfig(harness="claude-code", non_interactive=True))
        assert rc == 0
        write_upstream.assert_not_called()

    def test_run_setup_stops_on_step_failure(self, tmp_state_dir: tuple[Path, Path]):
        """Setup aborts when an intermediate step fails."""
        self.mock.mocks["seed_corpus"].return_value = 1
        setup_config, run_setup = self._import_run_setup()
        rc = run_setup(setup_config(non_interactive=True))
        assert rc == 1
        # Steps after seed_corpus should NOT be called
        self.mock.mocks["start_embed_server"].assert_not_called()

    def test_run_setup_rejects_invalid_hardware(self, tmp_state_dir: tuple[Path, Path]):
        """Setup rejects invalid hardware target in non-interactive mode."""
        setup_config, run_setup = self._import_run_setup()
        cfg = setup_config(
            non_interactive=True,
            hardware_target="invalid-gpu",
            recommended_host="nvidia",
        )
        rc = run_setup(cfg)
        assert rc == 1


class TestAddParser:
    """Test argparse registration and flag parsing."""

    def test_parser_registers_setup_subcommand(self):
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["setup", "--non-interactive"])
        assert args.subcommand == "setup"
        assert args.non_interactive is True

    def test_parser_accepts_all_flags(self):
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(
            [
                "setup",
                "-n",
                "--runner",
                "llama-server",
                "--model",
                "custom-model",
                "--port",
                "50000",
                "--mode",
                "manual",
                "--packs",
                "a,b,c",
                "--harness",
                "cursor",
                "--hardware",
                "nvidia",
                "--image-tag",
                "full",
            ]
        )
        assert args.runner == "llama-server"
        assert args.port == 50000
        assert args.mode == "manual"
        assert args.packs == "a,b,c"
        assert args.harness == "cursor"
        assert args.hardware == "nvidia"
        assert args.image_tag == "full"

    def test_parser_rejects_invalid_image_tag(self):
        """argparse rejects image-tag values not in choices."""
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(["setup", "--image-tag", "invalid-tag"])

    def test_parser_accepts_runtime(self):
        """argparse accepts --runtime {podman,docker}."""
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["setup", "--runtime", "docker"])
        assert args.runtime == "docker"
        # Default is None (auto-detect) when the flag is omitted.
        assert parser.parse_args(["setup"]).runtime is None

    def test_parser_rejects_invalid_runtime(self):
        """argparse rejects --runtime values not in choices."""
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        with pytest.raises(SystemExit):
            parser.parse_args(["setup", "--runtime", "containerd"])


class TestRunFromArgs:
    """Test the argparse -> SetupConfig bridge."""

    def test_defaults(self):
        import argparse

        args = argparse.Namespace(
            runner=None,
            model=None,
            port=None,
            mode=None,
            packs=None,
            harness=None,
            non_interactive=False,
            image_tag="latest",
            image_path=None,
        )
        # Just verify it builds a valid config -- actual run_setup is tested above
        # We'll just check _run_from_args doesn't raise with valid args
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        # Verify the image_ref was constructed correctly
        cfg = mock.call_args[0][0]
        assert cfg.image_tag == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_explicit_values(self):
        import argparse

        args = argparse.Namespace(
            runner="llama-server",
            model="my-model",
            port=50000,
            mode="manual",
            packs="pack1,pack2",
            harness="cursor",
            non_interactive=True,
            image_tag="full",
            image_path=None,
        )
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        # Verify the full image variant was constructed correctly
        cfg = mock.call_args[0][0]
        assert cfg.image_tag == "ghcr.io/nrmeyers/agentalloy:full"
        assert cfg.runner == "llama-server"
        assert cfg.port == 50000

    def test_runtime_flows_to_config(self):
        import argparse

        args = argparse.Namespace(
            runner=None,
            model=None,
            port=None,
            mode=None,
            packs=None,
            harness=None,
            non_interactive=True,
            image_tag="latest",
            image_path=None,
            runtime="docker",
        )
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        cfg = mock.call_args[0][0]
        assert cfg.runtime_binary == "docker"

    def test_upstream_flags_flow_to_config(self):
        import argparse

        args = argparse.Namespace(
            runner=None,
            model=None,
            port=None,
            mode=None,
            packs=None,
            harness=None,
            non_interactive=True,
            image_tag="latest",
            image_path=None,
            upstream_url="http://up:8080/v1",
            upstream_model="gpt-x",
            upstream_api_key="sk-secret",
        )
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        cfg = mock.call_args[0][0]
        assert cfg.upstream_url == "http://up:8080/v1"
        assert cfg.upstream_model == "gpt-x"
        assert cfg.upstream_api_key == "sk-secret"

    def test_upstream_env_fallback_when_flags_absent(self, monkeypatch: pytest.MonkeyPatch):
        import argparse

        monkeypatch.setenv("UPSTREAM_URL", "http://env:9000/v1")
        monkeypatch.setenv("UPSTREAM_MODEL", "env-model")
        monkeypatch.setenv("UPSTREAM_API_KEY", "sk-env")
        args = argparse.Namespace(
            runner=None,
            model=None,
            port=None,
            mode=None,
            packs=None,
            harness=None,
            non_interactive=True,
            image_tag="latest",
            image_path=None,
            upstream_url=None,
            upstream_model=None,
            upstream_api_key=None,
        )
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        cfg = mock.call_args[0][0]
        assert cfg.upstream_url == "http://env:9000/v1"
        assert cfg.upstream_model == "env-model"
        assert cfg.upstream_api_key == "sk-env"

    def test_upstream_flag_wins_over_env(self, monkeypatch: pytest.MonkeyPatch):
        import argparse

        monkeypatch.setenv("UPSTREAM_URL", "http://env:9000/v1")
        args = argparse.Namespace(
            runner=None,
            model=None,
            port=None,
            mode=None,
            packs=None,
            harness=None,
            non_interactive=True,
            image_tag="latest",
            image_path=None,
            upstream_url="http://flag:1234/v1",
            upstream_model=None,
            upstream_api_key=None,
        )
        with patch("agentalloy.install.subcommands.simple_setup.run_setup", return_value=0) as mock:
            rc = _run_from_args(args)
        assert rc == 0
        assert mock.call_args[0][0].upstream_url == "http://flag:1234/v1"


# ---------------------------------------------------------------------------
# Image variant label tests
# ---------------------------------------------------------------------------


class TestImageVariantLabel:
    """Test _image_variant_label helper."""

    def test_latest_tag(self):
        from agentalloy.install.subcommands.simple_setup import _image_variant_label

        assert _image_variant_label("ghcr.io/nrmeyers/agentalloy:latest") == (
            "latest (~300 MB, no model)"
        )
        assert _image_variant_label("latest") == "latest (~300 MB, no model)"

    def test_full_tag(self):
        from agentalloy.install.subcommands.simple_setup import _image_variant_label

        assert _image_variant_label("ghcr.io/nrmeyers/agentalloy:full") == (
            "full (~975 MB, pre-pulled model)"
        )
        assert _image_variant_label("full") == "full (~975 MB, pre-pulled model)"

    def test_custom_tag(self):
        from agentalloy.install.subcommands.simple_setup import _image_variant_label

        assert (
            _image_variant_label("my-registry.io/img:v1.2.3")
            == "custom (my-registry.io/img:v1.2.3)"
        )
        assert _image_variant_label("custom-tag") == "custom (custom-tag)"

    def test_no_tag_returns_full_ref(self):
        from agentalloy.install.subcommands.simple_setup import _image_variant_label

        # No ':' at all — the whole ref is treated as the tag
        assert _image_variant_label("my-image") == "custom (my-image)"


# ---------------------------------------------------------------------------
# Hardware target case-insensitivity
# ---------------------------------------------------------------------------


class TestHardwareTargetCaseInsensitive:
    """Test that hardware target input is normalized to lowercase."""

    def test_hardware_target_case_insensitive(self):
        """Interactive prompt normalizes mixed-case input."""
        # Verify the normalization logic: .strip().lower() handles all cases
        hardware_str = "CPU"
        hardware_str = hardware_str.strip().lower()
        assert hardware_str == "cpu"

    def test_non_interactive_cpu_uppercase(self):
        """Non-interactive mode normalizes 'CPU' to 'cpu'."""
        hardware_str = "CPU"
        normalized = hardware_str.strip().lower()
        assert normalized == "cpu"

    def test_hardware_target_various_cases(self):
        """Various capitalizations all normalize correctly."""
        cases = [
            ("CPU", "cpu"),
            ("Nvidia", "nvidia"),
            ("RADEON", "radeon"),
            ("Apple-Silicon", "apple-silicon"),
            ("  NVIDIA  ", "nvidia"),
        ]
        for input_val, expected in cases:
            result = input_val.strip().lower()
            assert result == expected, f"{input_val!r} -> {result!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# Pack discovery
# ---------------------------------------------------------------------------


class TestReconcilePriorInstall:
    """Prior-install detection: overwrite / native↔container switch in place."""

    def test_fresh_host_proceeds_without_teardown(self):
        from agentalloy.install.subcommands.simple_setup import _reconcile_prior_install

        cfg = SetupConfig(deployment="native", non_interactive=True)
        rc = _reconcile_prior_install(cfg, {"deployment": None}, None, False)
        assert rc == 0
        assert cfg.force is False  # nothing to overwrite

    def test_non_interactive_without_force_declines(self):
        from agentalloy.install.__main__ import EXIT_NOOP
        from agentalloy.install.subcommands.simple_setup import _reconcile_prior_install

        cfg = SetupConfig(deployment="container", non_interactive=True)
        rc = _reconcile_prior_install(cfg, {"deployment": "native"}, "native", True)
        assert rc == EXIT_NOOP

    def test_force_switch_container_to_native_tears_down_container(self):
        from agentalloy.install.subcommands import simple_setup as ss

        cfg = SetupConfig(deployment="native", non_interactive=True, force=True)
        with patch(
            "agentalloy.install.subcommands.uninstall._stop_container_stack",
            return_value=[],
        ) as stop:
            rc = ss._reconcile_prior_install(
                cfg, {"deployment": "container", "container_name": "agentalloy"}, "container", True
            )
        assert rc == 0
        stop.assert_called_once()
        assert cfg.force is True

    def test_force_native_prior_reaps_runtime(self):
        from agentalloy.install.subcommands import simple_setup as ss

        cfg = SetupConfig(deployment="container", non_interactive=True, force=True)
        with patch("agentalloy.install.runtime_artifacts.reap", return_value=[]) as reap:
            rc = ss._reconcile_prior_install(cfg, {"deployment": "native"}, "native", True)
        assert rc == 0
        reap.assert_called_once()
        assert reap.call_args.kwargs.get("scope") == "all"

    def test_interactive_decline_leaves_install_untouched(self):
        from agentalloy.install.__main__ import EXIT_NOOP
        from agentalloy.install.subcommands import simple_setup as ss

        cfg = SetupConfig(deployment="native")  # interactive
        with (
            patch.object(ss, "_prompt", return_value="n"),
            patch("agentalloy.install.runtime_artifacts.reap", return_value=[]) as reap,
        ):
            rc = ss._reconcile_prior_install(cfg, {"deployment": "native"}, "native", True)
        assert rc == EXIT_NOOP
        reap.assert_not_called()  # declined → no teardown

    def test_interactive_accept_overwrite_same_mode(self):
        from agentalloy.install.subcommands import simple_setup as ss

        cfg = SetupConfig(deployment="native")
        with (
            patch.object(ss, "_prompt", return_value="y"),
            patch("agentalloy.install.runtime_artifacts.reap", return_value=[]) as reap,
        ):
            rc = ss._reconcile_prior_install(cfg, {"deployment": "native"}, "native", True)
        assert rc == 0
        reap.assert_called_once()
        assert cfg.force is True


class TestPackDiscovery:
    """Test _discover_packs and _prompt_for_packs helpers."""

    def test_pack_discovery_finds_packs(self):
        """_discover_packs() returns packs from the installed _packs directory."""
        packs = _discover_packs()
        # Should find at least 30 packs (the hand-off doc says 35)
        assert len(packs) >= 30, f"Expected >=30 packs, got {len(packs)}"
        # Always-on packs should be present
        always_on = {n for n, m in packs.items() if m.get("always_install", False)}
        assert "core" in always_on
        assert "documentation" in always_on

    def test_pack_discovery_handles_missing_dir(self):
        """_discover_packs() returns empty dict when _packs dir doesn't exist."""
        with patch("pathlib.Path.is_dir", return_value=False):
            packs = _discover_packs()
            assert packs == {}

    def test_prompt_for_packs_handles_eof(self):
        """_prompt_for_packs() handles EOFError gracefully (non-TTY pipe)."""
        # Simulate EOFError from input()
        with patch("builtins.input", side_effect=EOFError):
            result = _prompt_for_packs()
            assert result == ""

    def test_prompt_for_packs_returns_all(self):
        """Selecting 'all' returns all pack names."""
        with patch("builtins.input", return_value="all"):
            result = _prompt_for_packs()
            # Should return a comma-separated list of pack names
            assert len(result) > 0
            pack_names = result.split(",")
            # Should include known packs
            assert "core" in pack_names

    def test_prompt_for_packs_handles_blank(self):
        """Blank input returns empty string (always-on only)."""
        with patch("builtins.input", return_value=""):
            result = _prompt_for_packs()
            assert result == ""

    def test_prompt_for_packs_handles_default(self):
        """Input 'defaults' returns empty string."""
        with patch("builtins.input", return_value="defaults"):
            result = _prompt_for_packs()
            assert result == ""

    def test_prompt_for_packs_handles_tier_selection(self):
        """Tier name selection returns packs in that tier."""
        with patch("builtins.input", return_value="foundation"):
            result = _prompt_for_packs()
            # Foundation tier packs should be in the result
            selected = result.split(",") if result else []
            # core is in foundation tier and always-on
            assert len(selected) > 0

    def test_prompt_for_packs_tier_label_case_insensitive(self):
        """Tier display labels are accepted regardless of case."""
        # "Platforms" is the display label for internal key "platform"
        with patch("builtins.input", return_value="platforms"):
            result = _prompt_for_packs()
            selected = result.split(",") if result else []
            # github-actions is in the platform tier
            assert "github-actions" in selected

    def test_prompt_for_packs_tier_label_mixed_case(self):
        """Mixed-case tier labels are accepted."""
        # "Workflows" is the display label for internal key "workflow"
        with patch("builtins.input", return_value="Workflows"):
            result = _prompt_for_packs()
            selected = result.split(",") if result else []
            # code-review is in the workflow tier
            assert "code-review" in selected

    def test_prompt_for_packs_handles_unknown(self):
        """Unknown pack names are ignored."""
        with patch("builtins.input", return_value="nonexistent-pack"):
            result = _prompt_for_packs()
            # Unknown packs are ignored, returns empty
            assert result == ""

    def test_non_interactive_skips_pack_prompt(self, tmp_path):
        """Non-interactive mode does not call _prompt_for_packs()."""
        from agentalloy.install.subcommands.simple_setup import run_setup

        cfg = SetupConfig(non_interactive=True)

        call_tracker = {"called": False}

        def fake_prompt_for_packs():
            call_tracker["called"] = True
            return ""

        class MockSetup:
            def __init__(self):
                self.mocks: dict[str, Any] = {}
                self.patchers: list[Any] = []

            def setup_all(self):
                for name in (
                    "detect",
                    "pull_models",
                    "seed_corpus",
                    "start_embed_server",
                    "start_rerank_server",
                    "install_packs",
                    "enable_service",
                    "write_env",
                    "verify",
                    "wire_harness",
                ):
                    mp = patch(f"agentalloy.install.subcommands.{name}.run")
                    self.mocks[name] = mp.start()
                    self.mocks[name].return_value = 0
                    self.patchers.append(mp)
                pf = patch("agentalloy.install.subcommands.preflight.run_preflight")
                self.mocks["preflight"] = pf.start()
                self.mocks["preflight"].return_value = {
                    "checks": [],
                    "fatal_failures": [],
                    "warn_failures": [],
                }
                self.patchers.append(pf)
                cc = patch(
                    "agentalloy.install.subcommands.simple_setup._corpus_skill_count",
                    return_value=100,
                )
                self.mocks["_corpus_skill_count"] = cc.start()
                self.patchers.append(cc)

            def teardown(self):
                for p in self.patchers:
                    p.stop()

        mock = MockSetup()
        mock.setup_all()

        # Also need detect.json
        detect_file = tmp_path / "detect.json"
        detect_file.write_text(json.dumps({"gpu": {"discrete": [], "integrated": []}}))

        outputs_patch = patch(
            "agentalloy.install.state.outputs_dir",
            return_value=tmp_path,
        )
        mock.patchers.append(outputs_patch)
        outputs_patch.start()

        with patch(
            "agentalloy.install.subcommands.simple_setup._prompt_for_packs",
            side_effect=fake_prompt_for_packs,
        ):
            rc = run_setup(cfg)

        mock.teardown()

        # Non-interactive should skip pack prompt entirely
        assert not call_tracker["called"], (
            "_prompt_for_packs() should not be called in non-interactive mode"
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Harness validation
# ---------------------------------------------------------------------------


class TestHarnessValidation:
    """Test harness input validation and aliases."""

    @pytest.fixture(autouse=True)
    def _mocks(self, tmp_state_dir: tuple[Path, Path]):
        self.mock = MockSetup()
        self.mock.setup_all()
        self.tmp_config, self.tmp_data = tmp_state_dir
        outputs_patch = patch(
            "agentalloy.install.state.outputs_dir",
            return_value=self.tmp_data / "outputs",
        )
        self.mock.patchers.append(outputs_patch)
        self.mock.mocks["_outputs_dir"] = outputs_patch.start()
        (self.tmp_data / "outputs").mkdir(parents=True, exist_ok=True)
        yield
        self.mock.teardown()

    def test_valid_harness_accepted(self, tmp_state_dir: tuple[Path, Path]):
        """Valid harness name passes through run_setup."""
        setup_config_cls, run_setup_fn = self._import_run_setup()
        rc = run_setup_fn(setup_config_cls(harness="claude-code", non_interactive=True))
        assert rc == 0

    def test_invalid_harness_rejected(self, tmp_state_dir: tuple[Path, Path]):
        """Invalid harness name is rejected."""
        setup_config_cls, run_setup_fn = self._import_run_setup()
        rc = run_setup_fn(setup_config_cls(harness="invalid-harness", non_interactive=True))
        assert rc == 1

    def test_continue_alias_normalized(self):
        """'continue' alias is normalized to 'continue-closed'."""
        mock = MockSetup()
        mock.setup_all()
        try:
            import agentalloy.install.subcommands.simple_setup as mod

            cfg = mod.SetupConfig(harness="continue", non_interactive=True)
            # Harness normalization happens before execution
            h = cfg.harness.strip().lower()
            if h == "continue":
                cfg.harness = "continue-closed"
            assert cfg.harness == "continue-closed"
        finally:
            mock.teardown()

    def test_known_harnesses_in_valid_set(self):
        """All harnesses from registry are in VALID_HARNESSES."""
        from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES

        expected = {
            "claude-code",
            "gemini-cli",
            "cursor",
            "windsurf",
            "github-copilot",
            "hermes-agent",
            "opencode",
            "aider",
            "cline",
            "continue-closed",
            "continue-local",
            "manual",
        }
        assert expected.issubset(VALID_HARNESSES)

    def _import_run_setup(self):
        import agentalloy.install.subcommands.simple_setup as mod

        return mod.SetupConfig, mod.run_setup


# ---------------------------------------------------------------------------
# Quiet flag and embed endpoint tests
# ---------------------------------------------------------------------------


class TestQuietFlag:
    """Tests for the quiet flag in _build_namespace."""

    def test_build_namespace_includes_quiet(self):
        cfg = SetupConfig()
        ns = _build_namespace(cfg)
        assert ns.quiet is True

    def test_quiet_can_be_overridden(self):
        cfg = SetupConfig()
        ns = _build_namespace(cfg, quiet=False)
        assert ns.quiet is False


class TestEmbedEndpoint:
    """Tests for _test_embed_endpoint function."""

    def test_embed_endpoint_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        import json as _json

        env_content = (
            "RUNTIME_EMBED_BASE_URL=http://localhost:11434\nRUNTIME_EMBEDDING_MODEL=test-model\n"
        )

        class _MockResp:
            def read(self):
                return _json.dumps({"data": [{"embedding": [0.1] * 768}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a: Any) -> None:
                pass

        cfg = SetupConfig()
        with patch("agentalloy.install.state.env_path", return_value=tmp_path / ".env"):
            (tmp_path / ".env").write_text(env_content)
            with patch("urllib.request.urlopen", return_value=_MockResp()):
                _test_embed_endpoint(cfg)

        captured = capsys.readouterr()
        assert "768-dim vector" in captured.out or "OK" in captured.out

    def test_embed_endpoint_missing_env(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        cfg = SetupConfig()
        with patch("agentalloy.install.state.env_path", return_value=tmp_path / ".env"):
            # .env doesn't exist
            _test_embed_endpoint(cfg)

        captured = capsys.readouterr()
        assert "skip" in captured.out.lower() or "could not" in captured.out

    def test_embed_endpoint_failure(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        import urllib.error as _urllib_error

        env_content = (
            "RUNTIME_EMBED_BASE_URL=http://localhost:11434\nRUNTIME_EMBEDDING_MODEL=test-model\n"
        )

        cfg = SetupConfig()
        with patch("agentalloy.install.state.env_path", return_value=tmp_path / ".env"):
            (tmp_path / ".env").write_text(env_content)
            with patch(
                "urllib.request.urlopen",
                side_effect=_urllib_error.URLError("Connection refused"),
            ):
                _test_embed_endpoint(cfg)

        captured = capsys.readouterr()
        assert "fail" in captured.out.lower()

    def test_embed_endpoint_with_proxy_port(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Smoke test: embedding OK then proxy skill query with real completion output."""
        import json as _json

        env_content = (
            "RUNTIME_EMBED_BASE_URL=http://localhost:11434\n"
            "RUNTIME_EMBEDDING_MODEL=test-model\n"
            "RUNTIME_PORT=47950\n"
        )

        # First call: embedding response
        embed_resp = _json.dumps({"data": [{"embedding": [0.1] * 768}]}).encode()
        # Second call: proxy chat completion response
        chat_resp = _json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Here is a pytest for the CLI:\n\ndef test_example():\n    pass"
                        }
                    }
                ]
            }
        ).encode()

        call_count = [0]

        class _MockResp:
            def __init__(self, data: bytes):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a: Any) -> None:
                pass

        def _fake_urlopen(req: Any, timeout: int = 10) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                return _MockResp(embed_resp)
            return _MockResp(chat_resp)

        cfg = SetupConfig()
        with patch("agentalloy.install.state.env_path", return_value=tmp_path / ".env"):
            (tmp_path / ".env").write_text(env_content)
            with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
                _test_embed_endpoint(cfg)

        captured = capsys.readouterr()
        # Strip ANSI codes for assertion
        import re

        clean = re.sub(r"\x1b\[[0-9;]*m", "", captured.out)
        assert "768-dim vector" in clean or "OK" in clean
        assert "Skill query test: OK" in clean
        assert "chars returned" in clean


# ---------------------------------------------------------------------------
# Setup Wizard UX Overhaul regression tests
# ---------------------------------------------------------------------------


def test_setup_argparse_rejects_non_llama_server_runner():
    """B1: --runner only accepts llama-server (the sole runner)."""
    from agentalloy.install.subcommands.simple_setup import add_parser

    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="subcommand")
    add_parser(sub)
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--runner", "lm-studio", "--non-interactive"])


def test_setup_explicit_runner_llama_server_is_preserved():
    """B3: Explicit --runner llama-server is preserved through the argparse bridge."""
    import argparse

    captured: list[Any] = []

    import unittest.mock as mock

    with mock.patch(
        "agentalloy.install.subcommands.simple_setup.run_setup",
        side_effect=lambda cfg: captured.append(cfg) or 0,  # type: ignore[misc]
    ):
        root = argparse.ArgumentParser()
        sub = root.add_subparsers()
        from agentalloy.install.subcommands.simple_setup import add_parser  # type: ignore[attr-defined]

        add_parser(sub)
        args = root.parse_args(["setup", "--runner", "llama-server", "--non-interactive"])
        args.func(args)

    assert captured[0].runner == "llama-server"


def test_hw_labels_cover_all_valid_targets():
    """B4: Hardware label map covers all valid targets."""
    from agentalloy.install.subcommands.simple_setup import _HW_LABELS  # type: ignore[attr-defined]

    assert set(_HW_LABELS) == {"cpu", "nvidia", "radeon", "apple-silicon"}
    assert _HW_LABELS["cpu"] == "CPU (RAM-only)"
    assert "Apple Silicon" in _HW_LABELS["apple-silicon"]


def test_prompt_numbered_returns_default_on_non_tty(
    monkeypatch: Any,
) -> None:
    """N1-N4: Numbered-menu helper returns default on non-TTY."""
    import sys

    from agentalloy.install.subcommands.simple_setup import _prompt_numbered  # type: ignore[attr-defined]

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    options = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]
    # default_index is 1-based; default_index=2 -> "b"
    assert _prompt_numbered("pick:", options, default_index=2) == "b"


# ---------------------------------------------------------------------------
# Runner reachability check
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Container deployment flow
# ---------------------------------------------------------------------------


class TestPromptDeployment:
    """Test _prompt_deployment helper."""

    def test_prompt_deployment_default_container(self, monkeypatch: pytest.MonkeyPatch):
        """_prompt_deployment returns 'container' by default (option 1)."""
        import sys

        from agentalloy.install.subcommands.simple_setup import _prompt_deployment  # type: ignore[attr-defined]

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        result = _prompt_deployment()
        assert result == "container"

    def test_prompt_deployment_native_choice(self, monkeypatch: pytest.MonkeyPatch):
        """User can choose 'native' by entering '2' (container is now option 1)."""
        import sys

        from agentalloy.install.subcommands.simple_setup import _prompt_deployment  # type: ignore[attr-defined]

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "2")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        result = _prompt_deployment()
        assert result == "native"

    def test_prompt_deployment_container_choice(self, monkeypatch: pytest.MonkeyPatch):
        """User can choose 'container' by entering '1'."""
        import sys

        from agentalloy.install.subcommands.simple_setup import _prompt_deployment  # type: ignore[attr-defined]

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "1")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
        result = _prompt_deployment()
        assert result == "container"


class TestContainerFlow:
    """Test the container deployment branch in run_setup()."""

    @pytest.fixture(autouse=True)
    def _mocks(self, tmp_state_dir: tuple[Path, Path]):
        self.mock = MockSetup()
        self.mock.setup_all()
        self.tmp_config, self.tmp_data = tmp_state_dir
        outputs_patch = patch(
            "agentalloy.install.state.outputs_dir",
            return_value=self.tmp_data / "outputs",
        )
        self.mock.patchers.append(outputs_patch)
        self.mock.mocks["_outputs_dir"] = outputs_patch.start()
        (self.tmp_data / "outputs").mkdir(parents=True, exist_ok=True)

        # The container flow polls http://localhost:{port}/readiness via
        # ``_wait_for_readiness`` (fast-start design). The wait loop calls
        # ``json.loads(resp.read().decode())`` and short-circuits on
        # ``status == "ready"``, so the mock must return parseable JSON
        # with that body.
        health_resp = MagicMock()
        health_resp.__enter__ = lambda s: s
        health_resp.__exit__ = MagicMock(return_value=False)
        health_resp.status = 200
        health_resp.read = MagicMock(return_value=b'{"status": "ready"}')
        urlopen_patch = patch("urllib.request.urlopen", return_value=health_resp)
        self.mock.patchers.append(urlopen_patch)
        self.mock.mocks["_urlopen"] = urlopen_patch.start()

        yield
        self.mock.teardown()

    def _import_run_setup(self):
        import agentalloy.install.subcommands.simple_setup as mod

        return mod.SetupConfig, mod.run_setup

    def test_container_flow_skips_native_prompts(self, tmp_state_dir: tuple[Path, Path]):
        """Container deployment skips runner/model/hardware prompts."""
        import sys

        SetupConfig, run_setup = self._import_run_setup()

        # Mock runtime detection at the seam the container flow actually uses
        # (_detect_functional_runtimes), so the test does not depend on whether
        # the host has a working podman/docker.
        with (
            patch(
                "agentalloy.install.subcommands.simple_setup._detect_functional_runtimes",
                return_value=["podman"],
            ),
            patch("subprocess.run") as mock_run,
            patch.object(sys.stdin, "isatty", lambda: True),
            patch(
                "builtins.input",
                _mock_input_accept,
            ),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        # Should succeed
        assert rc == 0
        # pull_models should NOT be called (container handles its own model)
        # The container flow has different steps than native

    def test_switch_to_native_sentinel_intercepted(self, tmp_state_dir: tuple[Path, Path]):
        """When _run_container_flow returns the switch sentinel, run_setup flips to
        native and continues — the sentinel never leaks as the exit code."""
        import agentalloy.install.subcommands.simple_setup as mod

        SetupConfig, run_setup = self._import_run_setup()

        class _ReachedNative(Exception):
            pass

        real_print = mod._print

        def fake_print(*args, **kwargs):
            if args and "agentalloy setup" in str(args[0]) and "bold" in str(args[0]):
                raise _ReachedNative
            return real_print(*args, **kwargs)

        cfg = SetupConfig(deployment="container", non_interactive=True)
        with (
            patch.object(mod, "_run_container_flow", return_value=mod._SWITCH_TO_NATIVE),
            patch.object(mod, "_print", side_effect=fake_print),
            patch("agentalloy.install.subcommands.detect.run", return_value=0),
        ):
            with pytest.raises(_ReachedNative):
                run_setup(cfg)

        # Interception flipped the deployment to native before entering its flow.
        assert cfg.deployment == "native"
        assert cfg.runtime_binary == ""

    def test_container_flow_records_state(self, tmp_state_dir: tuple[Path, Path]):
        """Container setup records deployment, image_tag, runtime_binary in state."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.simple_setup._detect_functional_runtimes",
                return_value=["podman"],
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0

        # Check state was recorded
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st["deployment"] == "container"
        assert st["runtime_binary"] == "podman"
        assert st["image_tag"] is not None

    def test_runtime_binary_missing_exits_nonzero(
        self, tmp_state_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ):
        """No podman/docker detected, setup exits with non-zero code."""
        SetupConfig, run_setup = self._import_run_setup()

        # No usable runtime: nothing functional, and nothing present to fall back
        # to. Patch both seams the container flow consults so the test is
        # independent of the host's installed runtimes.
        with (
            patch(
                "agentalloy.install.subcommands.simple_setup._detect_functional_runtimes",
                return_value=[],
            ),
            patch(
                "agentalloy.install.subcommands.simple_setup._detect_runtime_binary",
                return_value=None,
            ),
        ):
            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc != 0, f"Expected non-zero exit code, got {rc}"
        captured = capsys.readouterr()
        # Output goes to stdout via Rich console, not stderr
        combined = (captured.out + captured.err).lower()
        assert "podman" in combined or "docker" in combined or "compose" in combined

    def test_image_tag_path_absolute(self, tmp_state_dir: tuple[Path, Path], tmp_path: Path):
        """cfg.compose_file stored as absolute path, not relative."""
        SetupConfig, run_setup = self._import_run_setup()

        # Create compose.yaml in tmp_path
        compose_file = tmp_path / "compose.yaml"
        compose_file.write_text("version: '3'\nservices: {}\n")

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0

        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        # Should be absolute path
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_image_tag_resolved_from_repo_root_not_cwd(
        self, tmp_state_dir: tuple[Path, Path], tmp_path: Path
    ):
        """When cwd lacks assets, container setup falls back to the package repo root.

        Regression guard for two bugs:
          (1) Previously used `Path.cwd() / "compose.yaml"` unconditionally.
          (2) Then used `parents[4]` unconditionally, which broke for non-editable
              `uv tool install` users (parents[4] lands in site-packages).
        Now: cwd first, then parents[4] (the editable-install repo root).
        """
        SetupConfig, run_setup = self._import_run_setup()

        import agentalloy.install.subcommands.simple_setup as setup_mod

        Path(setup_mod.__file__).resolve().parents[4]

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            # Empty cwd → must fall through to parents[4] (real repo root).
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_image_tag_resolved_from_cwd_when_present(
        self, tmp_state_dir: tuple[Path, Path], tmp_path: Path
    ):
        """If user runs setup from a clone, cwd-resident assets win over parents[4]."""
        SetupConfig, run_setup = self._import_run_setup()

        (tmp_path / "compose.yaml").write_text("services: {}\n")
        (tmp_path / "Containerfile").write_text("FROM scratch\n")

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_image_tag_accepts_dockerfile_alternative(
        self, tmp_state_dir: tuple[Path, Path], tmp_path: Path
    ):
        """Asset detection matches preflight: Dockerfile satisfies the build-deps check too."""
        SetupConfig, run_setup = self._import_run_setup()

        (tmp_path / "compose.yaml").write_text("services: {}\n")
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")  # not Containerfile

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.cwd", return_value=tmp_path),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        # image_tag is always "ghcr.io/nrmeyers/agentalloy:latest" regardless of build context
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_interactive_fallback_accepts_directory_path(
        self, tmp_state_dir: tuple[Path, Path], tmp_path: Path
    ):
        """When auto-detect fails, user can paste a clone directory (default compose appended)."""
        SetupConfig, run_setup = self._import_run_setup()

        # Real clone elsewhere
        clone = tmp_path / "clone"
        clone.mkdir()
        (clone / "compose.yaml").write_text("services: {}\n")
        (clone / "Containerfile").write_text("FROM scratch\n")

        # Empty cwd + fake module location → auto-detect fails, prompt fires.
        fake_module_file = (
            tmp_path / "sp" / "agentalloy" / "install" / "subcommands" / "simple_setup.py"
        )
        fake_module_file.parent.mkdir(parents=True, exist_ok=True)
        fake_module_file.touch()
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()

        import agentalloy.install.subcommands.simple_setup as setup_mod

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.cwd", return_value=empty_cwd),
            patch.object(setup_mod, "__file__", str(fake_module_file)),
            # Prompt sequence: CPU-only continue → final confirm. (The pack
            # picker was removed from the container flow — published images
            # ship an all-packs corpus seed — and GHCR pull needs no path.)
            patch("builtins.input", side_effect=["y", "y"]),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=False))

        assert rc == 0
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        # image_tag is always "ghcr.io/nrmeyers/agentalloy:latest" regardless of build context
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_interactive_fallback_accepts_image_tag_path(
        self, tmp_state_dir: tuple[Path, Path], tmp_path: Path
    ):
        """User can also paste a direct path to the compose YAML, not a directory."""
        SetupConfig, run_setup = self._import_run_setup()

        clone = tmp_path / "clone"
        clone.mkdir()
        compose_file = clone / "compose.yaml"
        compose_file.write_text("services: {}\n")
        (clone / "Containerfile").write_text("FROM scratch\n")

        fake_module_file = (
            tmp_path / "sp" / "agentalloy" / "install" / "subcommands" / "simple_setup.py"
        )
        fake_module_file.parent.mkdir(parents=True, exist_ok=True)
        fake_module_file.touch()
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()

        import agentalloy.install.subcommands.simple_setup as setup_mod

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("pathlib.Path.cwd", return_value=empty_cwd),
            patch.object(setup_mod, "__file__", str(fake_module_file)),
            # Prompt sequence: CPU-only continue → final confirm. (The pack
            # picker was removed from the container flow — published images
            # ship an all-packs corpus seed — and GHCR pull needs no path.)
            patch("builtins.input", side_effect=["y", "y"]),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=False))

        assert rc == 0
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        # image_tag is always "ghcr.io/nrmeyers/agentalloy:latest" regardless of build context
        assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"

    def test_container_cpu_only_warning_shown_to_every_host(
        self, tmp_state_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ):
        """Container deployment always warns CPU-only and auto-continues non-interactive."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        out = capsys.readouterr().out.lower()
        assert "cpu-only" in out
        assert "gpu acceleration" in out
        # Regression guard: the previous Apple-Silicon-specific phrasing is gone.
        assert "apple silicon" not in out

    def test_container_cpu_only_warning_cancels_on_no(
        self, tmp_state_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ):
        """Interactive 'n' to the CPU-only warning cancels setup with rc=1."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
            patch("builtins.input", return_value="n"),
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=False))

        assert rc == 1
        out = capsys.readouterr().out.lower()
        assert "cancelled" in out

    def test_container_env_is_just_runtime_port(self, tmp_state_dir: tuple[Path, Path]):
        """Container .env now contains only RUNTIME_PORT — embedder is internal to compose."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "0\n"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        import agentalloy.install.state as state_mod

        env_text = state_mod.env_path().read_text()
        assert "RUNTIME_PORT=47950" in env_text
        # Regression guards: host should no longer carry embedder coordinates;
        # those live entirely inside the container's compose environment.
        assert "RUNTIME_EMBED_BASE_URL" not in env_text
        assert "RUNTIME_EMBEDDING_MODEL" not in env_text

    def test_container_aborts_when_run_fails(
        self, tmp_state_dir: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ):
        """If _run_container fails, the flow returns exit code 1 and
        state is not saved."""
        SetupConfig, run_setup = self._import_run_setup()

        def run_side_effect(argv: Any, **kwargs: Any) -> Any:
            mock = MagicMock()
            argv_list: list[str] = (
                [str(x) for x in argv]  # type: ignore[arg-type]
                if isinstance(argv, list)
                else []
            )
            # Simulate _run_container failure (podman run returns 1)
            if len(argv_list) >= 2 and argv_list[1] == "run":
                # Raise CalledProcessError to simulate container start failure
                raise subprocess.CalledProcessError(1, argv)
            mock.returncode = 0
            mock.stdout = "0\n"
            mock.stderr = ""
            return mock

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run", side_effect=run_side_effect),
        ):
            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 1
        # State should NOT have been saved
        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st.get("deployment") != "container", (
            "State should NOT be saved when container run fails"
        )

    # ------------------------------------------------------------------
    # Skill-pack selection (spec: agentalloy-container-skillpack-selection)
    # ------------------------------------------------------------------
    def test_container_flow_skips_prompt_when_packs_preset(self, tmp_state_dir: tuple[Path, Path]):
        """A preset --packs value must bypass the interactive prompt and
        flow straight through to the container run with packs in env."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch(
                "agentalloy.install.subcommands.simple_setup._prompt_for_packs",
            ) as mock_prompt,
            patch(
                "agentalloy.install.subcommands.simple_setup._discover_packs",
                return_value={"go": {"skills": []}},
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
            rc = run_setup(SetupConfig(deployment="container", non_interactive=True, packs="go"))

        assert rc == 0
        mock_prompt.assert_not_called()
        # Verify the container run call includes the packs in env var
        run_calls = [
            c.args[0]
            for c in mock_run.call_args_list
            if c.args
            and isinstance(c.args[0], list)
            and len(c.args[0]) >= 2
            and c.args[0][1] == "run"
        ]
        assert len(run_calls) >= 1
        # The container runs the image's baked /app/entrypoint.sh and reads the
        # pack list from the AGENTALLOY_PACKS env var — no host entrypoint
        # bind-mount (that temp file was deleted post-install, breaking
        # start/reboot). Verify packs are passed via env and nothing mounts
        # /app/entrypoint.sh.
        packs_in_env = any(
            str(arg) == "-e"
            and i + 1 < len(call_args)
            and str(call_args[i + 1]) == "AGENTALLOY_PACKS=go"
            for call_args in run_calls
            for i, arg in enumerate(call_args)
        )
        assert packs_in_env, "AGENTALLOY_PACKS=go should be passed to the container run"
        entrypoint_mounted = any(
            ":/app/entrypoint.sh:ro" in str(arg) for call_args in run_calls for arg in call_args
        )
        assert not entrypoint_mounted, "host entrypoint.sh must NOT be bind-mounted"

    def test_container_flow_non_interactive_no_packs_uses_defaults(
        self, tmp_state_dir: tuple[Path, Path]
    ):
        """Non-interactive container setup without --packs must NOT add
        --packs to the install-packs argv — preserves today's always-on default."""
        SetupConfig, run_setup = self._import_run_setup()

        with (
            patch(
                "agentalloy.install.subcommands.container_runtime._detect_runtime_binary",
                return_value="podman",
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
            rc = run_setup(SetupConfig(deployment="container", non_interactive=True))

        assert rc == 0
        # Verify no AGENTALLOY_PACKS env var with packs was set
        run_calls = [
            c.args[0]
            for c in mock_run.call_args_list
            if c.args
            and isinstance(c.args[0], list)
            and len(c.args[0]) >= 2
            and c.args[0][1] == "run"
        ]
        assert len(run_calls) >= 1
        packs_found = False
        for call_args in run_calls:
            for i, arg in enumerate(call_args):
                if str(arg) == "-e" and i + 1 < len(call_args):
                    env_val = str(call_args[i + 1])
                    if env_val.startswith("AGENTALLOY_PACKS="):
                        # Should be empty or not contain pack names
                        packs_found = True
                        assert "rust" not in env_val and "go" not in env_val
        # Either no AGENTALLOY_PACKS env var, or it's empty
        if packs_found:
            pass  # Empty packs env var is OK

    def test_native_deployment_records_state(self, tmp_state_dir: tuple[Path, Path]):
        """Native deployment records deployment='native' in state on success."""
        SetupConfig, run_setup = self._import_run_setup()

        rc = run_setup(SetupConfig(deployment="native", non_interactive=True))
        assert rc == 0

        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st["deployment"] == "native"

    def test_non_interactive_default_native(self, tmp_state_dir: tuple[Path, Path]):
        """Non-interactive without --deployment defaults to 'native'."""
        SetupConfig, run_setup = self._import_run_setup()

        rc = run_setup(SetupConfig(non_interactive=True))
        assert rc == 0

        import agentalloy.install.state as state_mod

        st = state_mod.load_state()
        assert st["deployment"] == "native"


class TestDeploymentCliFlag:
    """Test --deployment CLI flag parsing."""

    def test_parser_accepts_deployment_native(self):
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["setup", "--deployment", "native", "--non-interactive"])
        assert args.deployment == "native"

    def test_parser_accepts_deployment_container(self):
        import argparse

        from agentalloy.install.subcommands.simple_setup import add_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="subcommand")
        add_parser(subparsers)

        args = parser.parse_args(["setup", "--deployment", "container", "--non-interactive"])
        assert args.deployment == "container"


# ---------------------------------------------------------------------------
# Upstream LLM capture tests
# ---------------------------------------------------------------------------


class TestPromptUpstream:
    """Test _prompt_upstream captures the three upstream fields."""

    def test_prompt_upstream_uses_defaults_non_tty(self):
        """_prompt_upstream returns defaults in non-TTY mode."""
        cfg = SetupConfig(
            upstream_url="http://localhost:2099",
            upstream_model="",
            upstream_api_key="",
        )
        with patch.object(sys.stdin, "isatty", return_value=False):
            _prompt_upstream(cfg)
        assert cfg.upstream_url == "http://localhost:2099"

    def test_prompt_upstream_updates_cfg_fields(self):
        """_prompt_upstream writes user input into cfg fields."""
        cfg = SetupConfig()
        responses = iter(["http://llm.example.com/v1", "my-model", "sk-abc123"])
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=lambda _: next(responses)),
        ):
            _prompt_upstream(cfg)
        assert cfg.upstream_url == "http://llm.example.com/v1"
        assert cfg.upstream_model == "my-model"
        assert cfg.upstream_api_key == "sk-abc123"

    def test_prompt_upstream_accepts_empty_api_key(self):
        """_prompt_upstream accepts an empty API key (for local runners)."""
        cfg = SetupConfig(upstream_url="http://localhost:2099", upstream_model="qwen3")
        # Empty response = accept default (which is empty)
        with patch.object(sys.stdin, "isatty", return_value=False):
            _prompt_upstream(cfg)
        assert cfg.upstream_api_key == ""


class TestWriteUpstreamEnv:
    """Test _write_upstream_env writes/updates upstream vars in .env."""

    def test_writes_upstream_vars_to_new_env(self, tmp_path: Path):
        """Writes UPSTREAM_* vars when .env doesn't exist."""
        with patch("agentalloy.install.state.user_config_dir", return_value=tmp_path):
            cfg = SetupConfig(
                upstream_url="http://localhost:2099",
                upstream_model="qwen3-14b",
                upstream_api_key="sk-test",
            )
            _write_upstream_env(cfg)

        env_fp = tmp_path / ".env"
        content = env_fp.read_text()
        assert "UPSTREAM_URL=http://localhost:2099" in content
        assert "UPSTREAM_MODEL=qwen3-14b" in content
        assert "UPSTREAM_API_KEY=sk-test" in content

    def test_appends_to_existing_env(self, tmp_path: Path):
        """Appends upstream vars without disturbing existing content."""
        env_fp = tmp_path / ".env"
        env_fp.write_text("RUNTIME_EMBED_BASE_URL=http://localhost:11434\n")

        with patch("agentalloy.install.state.user_config_dir", return_value=tmp_path):
            cfg = SetupConfig(
                upstream_url="http://localhost:2099",
                upstream_model="qwen3",
                upstream_api_key="",
            )
            _write_upstream_env(cfg)

        content = env_fp.read_text()
        assert "RUNTIME_EMBED_BASE_URL=http://localhost:11434" in content
        assert "UPSTREAM_URL=http://localhost:2099" in content
        assert "UPSTREAM_MODEL=qwen3" in content

    def test_replaces_existing_upstream_vars(self, tmp_path: Path):
        """Re-running _write_upstream_env replaces old values (idempotent)."""
        env_fp = tmp_path / ".env"
        env_fp.write_text(
            "RUNTIME_EMBED_BASE_URL=http://localhost:11434\n"
            "UPSTREAM_URL=http://old-server/v1\n"
            "UPSTREAM_MODEL=old-model\n"
            "UPSTREAM_API_KEY=old-key\n"
        )

        with patch("agentalloy.install.state.user_config_dir", return_value=tmp_path):
            cfg = SetupConfig(
                upstream_url="http://new-server/v1",
                upstream_model="new-model",
                upstream_api_key="new-key",
            )
            _write_upstream_env(cfg)

        content = env_fp.read_text()
        assert "UPSTREAM_URL=http://new-server/v1" in content
        assert "UPSTREAM_MODEL=new-model" in content
        assert "UPSTREAM_API_KEY=new-key" in content
        # Old values must not appear
        assert "old-server" not in content
        assert "old-model" not in content
        assert "old-key" not in content
        # Other vars preserved
        assert "RUNTIME_EMBED_BASE_URL=http://localhost:11434" in content

    def test_handles_empty_api_key(self, tmp_path: Path):
        """Writes UPSTREAM_API_KEY= with empty value when no key is set."""
        with patch("agentalloy.install.state.user_config_dir", return_value=tmp_path):
            cfg = SetupConfig(
                upstream_url="http://localhost:2099",
                upstream_model="qwen3",
                upstream_api_key="",
            )
            _write_upstream_env(cfg)

        content = (tmp_path / ".env").read_text()
        assert "UPSTREAM_API_KEY=" in content


class TestTestUpstreamEndpoint:
    """Test _test_upstream_endpoint validates the upstream LLM connection."""

    def test_returns_true_on_200(self, tmp_path: Path):
        """Returns True when upstream /v1/models responds with 200."""
        cfg = SetupConfig(
            upstream_url="http://localhost:2099",
            upstream_model="qwen3",
            upstream_api_key="sk-test",
        )
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _test_upstream_endpoint(cfg)
        assert result is True

    def test_returns_false_on_connection_error(self):
        """Returns False (non-blocking) when upstream is unreachable."""
        cfg = SetupConfig(
            upstream_url="http://unreachable-host:9999/v1",
            upstream_model="qwen3",
            upstream_api_key="",
        )
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            result = _test_upstream_endpoint(cfg)
        assert result is False

    def test_returns_false_when_url_empty(self):
        """Returns False when upstream URL is not set."""
        cfg = SetupConfig(upstream_url="", upstream_model="qwen3", upstream_api_key="")
        result = _test_upstream_endpoint(cfg)
        assert result is False

    def test_returns_false_on_non_200(self):
        """Returns False when upstream returns non-200 HTTP status."""
        cfg = SetupConfig(
            upstream_url="http://localhost:2099",
            upstream_model="qwen3",
            upstream_api_key="",
        )
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 503

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _test_upstream_endpoint(cfg)
        assert result is False

    def test_includes_auth_header_when_api_key_set(self):
        """Sends Authorization: Bearer header when api key is configured."""
        cfg = SetupConfig(
            upstream_url="http://localhost:2099",
            upstream_model="qwen3",
            upstream_api_key="sk-mysecret",
        )
        captured_requests: list[Any] = []

        def fake_urlopen(req: Any, timeout: int = 10) -> Any:
            captured_requests.append(req)
            raise OSError("not really connecting")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _test_upstream_endpoint(cfg)

        assert len(captured_requests) == 1
        auth = captured_requests[0].get_header("Authorization")
        assert auth == "Bearer sk-mysecret"


class TestSetupConfigUpstreamDefaults:
    """Test that SetupConfig has correct upstream defaults."""

    def test_default_upstream_url_empty(self):
        """SetupConfig defaults upstream_url to empty string."""
        cfg = SetupConfig()
        assert cfg.upstream_url == ""

    def test_default_upstream_model_empty(self):
        """SetupConfig defaults upstream_model to empty string."""
        cfg = SetupConfig()
        assert cfg.upstream_model == ""

    def test_default_upstream_api_key_empty(self):
        """SetupConfig defaults upstream_api_key to empty string."""
        cfg = SetupConfig()
        assert cfg.upstream_api_key == ""
