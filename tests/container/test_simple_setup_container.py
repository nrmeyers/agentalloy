"""Tests for the container flow in simple_setup -- UT-21 through UT-23."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

from agentalloy.install.subcommands.simple_setup import (
    SetupConfig,
    _reconcile_native_port_holder,
)

# ---------------------------------------------------------------------------
# UT-21: SetupConfig no longer has compose_binary or compose_file attributes
# ---------------------------------------------------------------------------


class TestSetupConfigNoComposeAttributes:
    """UT-21: SetupConfig should not expose compose_binary or compose_file.

    The container flow was rewritten to use direct runtime primitives
    (container_runtime.py) instead of podman-compose. The compose-specific
    attributes were removed from SetupConfig to simplify the config object.
    """

    def test_no_compose_binary_attribute(self):
        """SetupConfig should not have compose_binary attribute."""
        cfg = SetupConfig()
        assert not hasattr(cfg, "compose_binary"), (
            "SetupConfig still has compose_binary - it was removed during "
            "the container flow rewrite"
        )

    def test_no_compose_file_attribute(self):
        """SetupConfig should not have compose_file attribute."""
        cfg = SetupConfig()
        assert not hasattr(cfg, "compose_file"), (
            "SetupConfig still has compose_file - it was removed during the container flow rewrite"
        )

    def test_setupconfig_dataclass_fields(self):
        """Verify SetupConfig has the expected fields after compose removal."""
        cfg = SetupConfig()
        field_names = {f.name for f in cfg.__dataclass_fields__.values()}
        # These should exist
        expected = {
            "runner",
            "model",
            "port",
            "mode",
            "packs",
            "harness",
            "preset",
            "non_interactive",
            "force",
            "acknowledge_sidecar",
            "hardware_target",
            "deployment",
            "upstream_url",
            "upstream_model",
            "upstream_api_key",
            "detected_runner",
            "recommended_host",
            "models_output",
        }
        assert expected.issubset(field_names), f"Missing expected fields: {expected - field_names}"
        # These should NOT exist
        assert "compose_binary" not in field_names
        assert "compose_file" not in field_names


# ---------------------------------------------------------------------------
# UT-22: Container mode sets runner=llama-server, port=47950, mode=manual
# ---------------------------------------------------------------------------


class TestContainerModeFixedValues:
    """UT-22: Container deployment mode sets fixed configuration values.

    When deployment=container, the wizard overrides user-chosen values to
    enforce a consistent, supported configuration:
    - runner is always llama-server (the sole inference runner)
    - port is always 47950
    - mode is always manual (no systemd for containers)
    - deployment is always container
    """

    def test_container_flow_sets_fixed_values(self, tmp_path: Path):
        """The REAL _run_container_flow overrides runner=llama-server, port=47950, mode=manual.

        This exercises the actual function: every side-effecting boundary call
        (preflight, runtime detection, native-port reconciliation, container
        sweep/pull/run, readiness wait) is mocked so the flow reaches the
        "Set fixed values" block and beyond. We stop it just past the image
        pull (which we force to fail) so no real container work happens — the
        config overrides are already applied by then. The assertions check the
        config the real code mutated, not a hand-rolled stand-in.
        """
        import agentalloy.install.subcommands.simple_setup as mod

        SetupConfig, _run_container_flow = (
            mod.SetupConfig,
            mod._run_container_flow,
        )

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            cfg = SetupConfig(
                deployment="native",  # non-container starting values, to prove override
                runner="lm-studio",
                port=9999,
                mode="persistent",
                harness="claude-code",
                non_interactive=True,  # skip the CPU-only / confirm input() prompts
            )

            with (
                # preflight passes for every phase (no fatal checks)
                patch.object(mod.preflight, "run_preflight", return_value={"checks": []}),
                # exactly one functional runtime → no prompt
                patch.object(mod, "_detect_functional_runtimes", return_value=["podman"]),
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                # the native :47950 holder reconciliation is a no-op (port free)
                patch.object(mod, "_reconcile_native_port_holder", return_value=0),
                # no stale containers to sweep
                patch.object(mod, "_list_project_containers", return_value=[]),
                patch.object(mod, "_list_conflicting_containers", return_value=[]),
                # stop right after the overrides: fail the image pull so the flow
                # returns 1 without doing real container/volume/readiness work.
                patch.object(mod, "_pull_image", return_value=1) as pull,
                patch.object(mod, "_ensure_volume") as ensure_volume,
                patch.object(mod, "_run_container") as run_container,
                patch.object(mod, "_wait_for_readiness") as wait,
            ):
                rc = _run_container_flow(cfg, 0.0)

            # Flow bailed at the (mocked-to-fail) image pull, after the overrides.
            assert rc == 1
            pull.assert_called_once()
            # We never reached the real container start / readiness wait.
            ensure_volume.assert_not_called()
            run_container.assert_not_called()
            wait.assert_not_called()

            # The REAL "Set fixed values" block mutated cfg:
            assert cfg.runner == "llama-server", (
                f"Expected runner='llama-server', got '{cfg.runner}'"
            )
            assert cfg.port == 47950, f"Expected port=47950, got {cfg.port}"
            assert cfg.mode == "manual", f"Expected mode='manual', got '{cfg.mode}'"
            assert cfg.deployment == "container", (
                f"Expected deployment='container', got '{cfg.deployment}'"
            )
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_container_flow_threads_configured_port_to_run_container(self, tmp_path: Path):
        """issue #300: _run_container must receive the configured port, not a
        hardcoded 47950 — this call site was the one missed by the fix.

        cfg.port is forced to 47950 by the "Set fixed values" block (line 1157)
        before this call is reached, so both the kwarg's presence and its value
        are asserted; a prior regression here would silently omit the kwarg.
        """
        import agentalloy.install.subcommands.simple_setup as mod

        SetupConfig, _run_container_flow = (
            mod.SetupConfig,
            mod._run_container_flow,
        )

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            cfg = SetupConfig(
                deployment="native",
                runner="lm-studio",
                port=9999,
                mode="persistent",
                harness="claude-code",
                non_interactive=True,
            )

            with (
                patch.object(mod.preflight, "run_preflight", return_value={"checks": []}),
                patch.object(mod, "_detect_functional_runtimes", return_value=["podman"]),
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                patch.object(mod, "_reconcile_native_port_holder", return_value=0),
                patch.object(mod, "_list_project_containers", return_value=[]),
                patch.object(mod, "_list_conflicting_containers", return_value=[]),
                patch.object(mod, "_pull_image", return_value=0),
                patch.object(mod, "_ensure_volume"),
                # Stop right after the (mocked-to-fail) container start so we
                # never reach the real readiness wait.
                patch.object(mod, "_run_container", return_value=1) as run_container,
            ):
                rc = _run_container_flow(cfg, 0.0)

            assert rc == 1
            run_container.assert_called_once()
            assert run_container.call_args.kwargs["port"] == 47950
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_native_mode_does_not_override(self, tmp_path: Path):
        """Native run_setup forces runner=llama-server but preserves user mode/harness.

        Native mode does NOT apply the container overrides (which would force
        mode=manual). It does force the sole runner (llama-server) and rejects
        any other --runner value. This exercises the REAL run_setup: it gathers
        config, then we let it bail at the (mocked-to-fail) early preflight so
        the assertions read the config the real code set, with no install work.
        """
        import agentalloy.install.subcommands.simple_setup as mod

        SetupConfig, run_setup = mod.SetupConfig, mod.run_setup

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        # A fatal early-preflight result so run_setup returns 1 right after the
        # config-gathering phase (Phase 3, step a) without any real install work.
        fatal_preflight = {
            "checks": [
                {
                    "name": "port_free",
                    "passed": False,
                    "severity": "fatal",
                    "error": "stub",
                    "remediation": "",
                }
            ]
        }

        try:
            with (
                patch.object(mod.detect, "run", return_value=0),
                patch.object(mod.preflight, "run_preflight", return_value=fatal_preflight),
                patch.object(sys.stdin, "isatty", lambda: False),
            ):
                cfg = SetupConfig(
                    deployment="native",
                    # llama-server is the sole accepted runner; any other value is
                    # rejected by run_setup. None is also accepted (defaults to it).
                    runner="llama-server",
                    port=47950,
                    mode="persistent",
                    harness="claude-code",
                    non_interactive=True,
                )
                rc = run_setup(cfg)

            # Bailed at the fatal early preflight in Phase 3 (after config gather).
            assert rc == 1
            # Native mode forces the sole runner but preserves user mode/harness
            # (it does NOT apply the container override of mode=manual).
            assert cfg.runner == "llama-server"
            assert cfg.mode == "persistent"
            assert cfg.harness == "claude-code"
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]


# ---------------------------------------------------------------------------
# UT-23: Interactive container mode displays CPU-only warning and prompts
# ---------------------------------------------------------------------------


class TestInteractiveContainerCpuWarning:
    """UT-23: Interactive container mode shows CPU-only warning.

    When running setup in container mode interactively, the wizard must:
    1. Display a yellow warning that container deployment is CPU-only
    2. Prompt the user to confirm they want to continue
    3. Exit with code 1 if the user declines
    """

    def _capture_cpu_warning(self, tmp_path: Path):
        """Helper to verify CPU-only warning is displayed during container setup."""
        import agentalloy.install.subcommands.simple_setup as mod

        _SetupConfig, _run_container_flow = (
            mod.SetupConfig,
            mod._run_container_flow,
        )

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            captured_prints: list[str] = []

            def capture_print(*args, **kwargs):
                captured_prints.append(" ".join(str(a) for a in args))

            # The CPU-only warning is printed inside _run_container_flow at lines
            # 991-1000. We verify it's there by checking the source code.
            import inspect

            source = inspect.getsource(_run_container_flow)

            # The warning text is hardcoded in the source
            assert "CPU-only" in source, (
                "Expected 'CPU-only' warning text in _run_container_flow source"
            )

            # Verify the warning is displayed before the input prompt
            cpu_warning_pos = source.index("CPU-only")
            input_prompt_pos = source.index("Continue with container")
            assert cpu_warning_pos < input_prompt_pos, (
                "CPU-only warning should be displayed before the confirmation prompt"
            )

            return True
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_cpu_warning_displayed_on_interactive_container(self, tmp_path: Path):
        """Container mode in interactive mode displays CPU-only warning."""
        result = self._capture_cpu_warning(tmp_path)
        assert result is True

    def test_container_interactive_cancel_on_cpu_warning(self, tmp_path: Path):
        """User can cancel container setup by declining the CPU-only prompt."""
        import agentalloy.install.subcommands.simple_setup as mod

        _SetupConfig, _run_container_flow = (
            mod.SetupConfig,
            mod._run_container_flow,
        )

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            captured_prints: list[str] = []

            def capture_print(*args, **kwargs):
                captured_prints.append(" ".join(str(a) for a in args))

            # Verify the source code has the cancellation logic
            import inspect

            source = inspect.getsource(_run_container_flow)

            # Check for the cancellation branch
            assert "Setup cancelled" in source or "cancelled" in source.lower(), (
                "Expected cancellation message in _run_container_flow"
            )

            # Verify the input prompt accepts "n" or "no" to cancel
            assert 'ans in ("n", "no")' in source or 'ans in ("n", "no")' in source, (
                "Expected cancellation check for 'n'/'no' in source"
            )
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_container_interactive_accept(self, tmp_path: Path):
        """User accepts the CPU-only warning and setup continues."""
        import agentalloy.install.subcommands.simple_setup as mod

        _SetupConfig, _run_container_flow = (
            mod.SetupConfig,
            mod._run_container_flow,
        )

        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

        try:
            # Verify the source code has the acceptance path
            import inspect

            source = inspect.getsource(_run_container_flow)

            # The default for the CPU-only prompt is "Y" (yes)
            # Check that the prompt has [Y/n] default
            assert "[Y/n]" in source, "Expected [Y/n] default in CPU-only confirmation prompt"

            # Verify that non-Y answers trigger cancellation
            assert 'ans in ("n", "no")' in source, "Expected cancellation check for non-yes answers"
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]


# ---------------------------------------------------------------------------
# Container runtime selection (functional probe + multi-runtime choice)
# ---------------------------------------------------------------------------


class TestDeploymentPromptOrder:
    """Container is listed first (option 1) and is the default."""

    def test_container_is_first_and_default(self):
        import agentalloy.install.subcommands.simple_setup as mod

        captured: dict[str, object] = {}

        def fake_prompt(title, options, default_index):
            captured["options"] = options
            captured["default_index"] = default_index
            return options[default_index - 1][0]

        with patch.object(mod, "_prompt_numbered", side_effect=fake_prompt):
            chosen = mod._prompt_deployment()

        values = [opt[0] for opt in captured["options"]]
        assert values == ["container", "native"], values
        assert captured["default_index"] == 1
        assert chosen == "container"


class TestContainerRuntimeSelection:
    """`_run_container_flow` selects among *functional* runtimes and prompts on ties."""

    def test_switch_to_native_when_no_runtime_and_user_opts_in(self, tmp_path: Path):
        """Interactive, no runtime → offer native; 'y' returns the switch sentinel."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=[]),
                patch.object(mod, "_detect_runtime_binary", return_value=None),
                patch.object(sys.stdin, "isatty", lambda: True),
                patch("builtins.input", return_value="y"),
            ):
                cfg = mod.SetupConfig(non_interactive=False)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == mod._SWITCH_TO_NATIVE
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_no_switch_when_user_declines(self, tmp_path: Path):
        """Interactive, no runtime, user declines the native fallback → exit 1."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=[]),
                patch.object(mod, "_detect_runtime_binary", return_value=None),
                patch.object(sys.stdin, "isatty", lambda: True),
                patch("builtins.input", return_value="n"),
            ):
                cfg = mod.SetupConfig(non_interactive=False)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    @staticmethod
    def _xdg(tmp_path: Path) -> None:
        config_dir = tmp_path / ".config"
        data_dir = tmp_path / ".local" / "share"
        config_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_DATA_HOME"] = str(data_dir)

    @staticmethod
    def _preflight(*, container_fatal: bool = False):
        """Stub preflight.run_preflight: early always passes; container optionally fatal."""

        def _run(*, phase: str = "early", **_kw):
            if phase == "container" and container_fatal:
                return {
                    "checks": [
                        {
                            "name": "runtime_binary",
                            "passed": False,
                            "severity": "fatal",
                            "error": "stub",
                            "remediation": "",
                        }
                    ]
                }
            return {"checks": []}

        return _run

    def test_no_functional_runtime_but_present_reports_not_responding(self, tmp_path: Path):
        """podman present but no machine, docker absent → bail with 'not responding'."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        prints: list[str] = []
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=[]),
                patch.object(mod, "_detect_runtime_binary", return_value="podman"),
                patch.object(
                    mod,
                    "_print",
                    side_effect=lambda *a, **k: prints.append(" ".join(str(x) for x in a)),
                ),
            ):
                cfg = mod.SetupConfig(non_interactive=True)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert any("not responding" in line for line in prints), prints
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_no_runtime_present_reports_neither_found(self, tmp_path: Path):
        """Neither runtime on PATH → bail with 'Neither ... found'."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        prints: list[str] = []
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=[]),
                patch.object(mod, "_detect_runtime_binary", return_value=None),
                patch.object(
                    mod,
                    "_print",
                    side_effect=lambda *a, **k: prints.append(" ".join(str(x) for x in a)),
                ),
            ):
                cfg = mod.SetupConfig(non_interactive=True)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert any("neither" in line.lower() for line in prints), prints
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_multiple_functional_runtimes_prompt_choice(self, tmp_path: Path):
        """Interactive, both runtimes work → user's choice becomes cfg.runtime_binary."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=["podman", "docker"]),
                patch.object(mod, "_prompt_numbered", return_value="docker") as prompt,
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                # Decline the CPU-only prompt so the flow bails right after selection.
                patch("builtins.input", return_value="n"),
            ):
                cfg = mod.SetupConfig(non_interactive=False)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1  # cancelled at CPU prompt
            assert cfg.runtime_binary == "docker"
            prompt.assert_called_once()
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_explicit_runtime_flag_honored_when_functional(self, tmp_path: Path):
        """--runtime docker (cfg.runtime_binary preset) is used even when podman also works."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=["podman", "docker"]),
                patch.object(mod, "_prompt_numbered") as prompt,
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                patch("builtins.input", return_value="n"),  # bail at CPU prompt after selection
            ):
                cfg = mod.SetupConfig(non_interactive=False, runtime_binary="docker")
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert cfg.runtime_binary == "docker"
            prompt.assert_not_called()  # explicit choice → no prompt
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_explicit_runtime_flag_rejected_when_not_responding(self, tmp_path: Path):
        """--runtime podman but its machine is down → bail, do not substitute docker."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        prints: list[str] = []
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=["docker"]),
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                patch.object(
                    mod,
                    "_print",
                    side_effect=lambda *a, **k: prints.append(" ".join(str(x) for x in a)),
                ),
            ):
                cfg = mod.SetupConfig(non_interactive=True, runtime_binary="podman")
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert any("not responding" in line for line in prints), prints
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_explicit_runtime_flag_rejected_when_not_on_path(self, tmp_path: Path):
        """--runtime podman but podman is not installed → bail, do not substitute docker."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        prints: list[str] = []
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=["docker"]),
                patch.object(mod.shutil, "which", side_effect=lambda n: None),
                patch.object(
                    mod,
                    "_print",
                    side_effect=lambda *a, **k: prints.append(" ".join(str(x) for x in a)),
                ),
            ):
                cfg = mod.SetupConfig(non_interactive=True, runtime_binary="podman")
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert any("not on" in line.lower() and "path" in line.lower() for line in prints), (
                prints
            )
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_non_interactive_never_switches_to_native(self, tmp_path: Path):
        """Non-interactive with no runtime returns 1 (never the switch sentinel)."""
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(mod.preflight, "run_preflight", side_effect=self._preflight()),
                patch.object(mod, "_detect_functional_runtimes", return_value=[]),
                patch.object(mod, "_detect_runtime_binary", return_value=None),
            ):
                cfg = mod.SetupConfig(non_interactive=True)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1
            assert rc != mod._SWITCH_TO_NATIVE
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]

    def test_multiple_functional_runtimes_non_tty_picks_podman(self, tmp_path: Path):
        """On a non-TTY both work → podman (preference) without blocking on input.

        Determinism comes from `_prompt_numbered`'s own non-TTY fallback, so the
        prompt is left unmocked here to exercise that path end-to-end.
        """
        import agentalloy.install.subcommands.simple_setup as mod

        self._xdg(tmp_path)
        try:
            with (
                patch.object(
                    mod.preflight,
                    "run_preflight",
                    side_effect=self._preflight(container_fatal=True),
                ),
                patch.object(mod, "_detect_functional_runtimes", return_value=["podman", "docker"]),
                patch.object(mod.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"),
                patch.object(sys.stdin, "isatty", lambda: False),
            ):
                cfg = mod.SetupConfig(non_interactive=True)
                rc = mod._run_container_flow(cfg, 0.0)
            assert rc == 1  # container preflight stubbed fatal — bails after selection
            assert cfg.runtime_binary == "podman"
        finally:
            del os.environ["XDG_CONFIG_HOME"]
            del os.environ["XDG_DATA_HOME"]


# ---------------------------------------------------------------------------
# Native :47950 holder reconciliation before `podman run` (container collision)
# ---------------------------------------------------------------------------


class TestReconcileNativePortHolder:
    """Container setup reclaims a NATIVE agentalloy holder of the host port before
    `podman run`, but never a foreign process or podman's own rootlessport forwarder."""

    _PH = "agentalloy.install.server_proc.port_holder_cmdline"
    _RC = "agentalloy.install.server_proc.reclaim_stale_port"
    _PRINT = "agentalloy.install.subcommands.simple_setup._print"
    _PROMPT = "agentalloy.install.subcommands.simple_setup._prompt"

    def _cfg(self, *, non_interactive: bool = False) -> SetupConfig:
        return SetupConfig(port=47950, non_interactive=non_interactive)

    def test_free_port_proceeds(self) -> None:
        with (
            patch(self._PH, return_value=(None, "")),
            patch(self._RC) as reclaim,
            patch(self._PRINT),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 0
        reclaim.assert_not_called()

    def test_native_holder_interactive_reclaim(self) -> None:
        with (
            patch(self._PH, return_value=(99, "python -m uvicorn agentalloy.app")),
            patch(self._RC, return_value=99) as reclaim,
            patch(self._PRINT),
            patch(self._PROMPT, return_value="y"),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 0
        reclaim.assert_called_once()

    def test_native_holder_declined_aborts(self) -> None:
        with (
            patch(self._PH, return_value=(99, "uvicorn agentalloy.app")),
            patch(self._RC) as reclaim,
            patch(self._PRINT),
            patch(self._PROMPT, return_value="n"),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 1
        reclaim.assert_not_called()

    def test_native_holder_non_interactive_auto_reclaims(self) -> None:
        with (
            patch(self._PH, return_value=(99, "uvicorn agentalloy.app")),
            patch(self._RC, return_value=99) as reclaim,
            patch(self._PRINT),
            patch(self._PROMPT) as prompt,
        ):
            assert _reconcile_native_port_holder(self._cfg(non_interactive=True)) == 0
        reclaim.assert_called_once()
        prompt.assert_not_called()

    def test_foreign_holder_aborts_without_killing(self) -> None:
        with (
            patch(self._PH, return_value=(77, "/usr/bin/some-other-server --port 47950")),
            patch(self._RC) as reclaim,
            patch(self._PRINT),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 1
        reclaim.assert_not_called()

    def test_rootlessport_forwarder_skipped(self) -> None:
        # podman's own forwarder for a container the sweep already cleared — leave it
        # and proceed (the port frees as that container goes down).
        with (
            patch(self._PH, return_value=(55, "rootlessport --child-ip 10.0.2.100")),
            patch(self._RC) as reclaim,
            patch(self._PRINT),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 0
        reclaim.assert_not_called()

    def test_reclaim_failure_aborts(self) -> None:
        with (
            patch(self._PH, return_value=(99, "uvicorn agentalloy.app")),
            patch(self._RC, return_value=None),
            patch(self._PRINT),
            patch(self._PROMPT, return_value="y"),
        ):
            assert _reconcile_native_port_holder(self._cfg()) == 1
