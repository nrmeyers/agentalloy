"""Tests for Hermes Agent proxy wiring (per-repo HERMES_HOME interception)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentalloy.api.proxy_context import decode_proj_token, encode_proj_token
from agentalloy.install.subcommands import wire_harness
from tests._wire_compat import wire_compat

# Bound at import time so TestRestartHermesGateway exercises the real function
# even though conftest's autouse _never_launch_hermes_gateway fixture replaces
# the wire_harness module attribute.
_real_restart_hermes_gateway = wire_harness._restart_hermes_gateway  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _read_repo_config(root: Path) -> dict[str, object]:
    data = yaml.safe_load((root / ".hermes" / "config.yaml").read_text())
    assert isinstance(data, dict)
    return data


class TestHermesAgentProxyWiring:
    """hermes-agent is inherently per-repo: it wires the repo-local carrier at
    *root* regardless of the requested scope (like claude-code), never touching
    the user's global ~/.hermes/config.yaml."""

    @pytest.fixture(autouse=True)
    def stub_gateway_restart(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Never launch a real hermes gateway from tests; record restart calls."""
        calls: list[Path] = []
        monkeypatch.setattr(
            wire_harness, "_restart_hermes_gateway", lambda root: calls.append(root) or True
        )
        return calls

    @pytest.fixture(autouse=True)
    def both_managers_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin activation-manager detection so tests don't depend on the host PATH."""
        monkeypatch.setattr(wire_harness, "_activation_managers", lambda: {"direnv", "mise"})

    @pytest.fixture(autouse=True)
    def stub_mise_trust(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Never touch the real mise trust database; record trust calls."""
        calls: list[Path] = []
        monkeypatch.setattr(wire_harness, "_mise_trust", lambda path: calls.append(path) or True)
        return calls

    def test_scope_is_ignored_always_repo_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User scope still wires the repo-local carrier and never the global config."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("hermes-agent", port=5555, root=tmp_path, scope="user")

        assert result["integration_vector"] == "proxy"
        assert (tmp_path / ".hermes" / "config.yaml").exists()
        assert not (fake_home / ".hermes" / "config.yaml").exists()

    def test_repo_scope_writes_proxy_model_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo scope redirects ``model`` at the proxy's per-repo /proj/<token> URL."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")
        assert result["integration_vector"] == "proxy"

        token = encode_proj_token(tmp_path)
        config = _read_repo_config(tmp_path)
        model = config["model"]
        assert isinstance(model, dict)
        assert model["provider"] == "custom"
        assert model["base_url"] == f"http://localhost:6666/proj/{token}/v1"
        assert model["default"] == "agentalloy-proxy"

        # The token round-trips to this repo (how the proxy resolves per-repo state).
        assert decode_proj_token(token) == Path(tmp_path).resolve()

        # Auth-by-adoption: no dummy key, no fictional custom_providers block.
        assert "api_key" not in model
        assert "custom_providers" not in config

    def test_repo_scope_writes_hermes_home_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo scope writes a sourceable env file pinning HERMES_HOME to the repo dir."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        env = tmp_path / ".hermes" / ".agentalloy-env"
        assert env.exists()
        assert 'HERMES_HOME="$PWD/.hermes"' in env.read_text()

    def test_repo_scope_preserves_global_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The repo-local config keeps the user's other global tuning, only swapping model.*."""
        fake_home = tmp_path / "home"
        (fake_home / ".hermes").mkdir(parents=True)
        (fake_home / ".hermes" / "config.yaml").write_text(
            "model:\n"
            "  provider: custom\n"
            "  base_url: http://10.0.0.1:60000/v1\n"
            "  default: qwen3.6\n"
            "  context_length: 8192\n"
            "context_compression:\n"
            "  enabled: true\n"
        )
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        config = _read_repo_config(tmp_path)
        model = config["model"]
        assert isinstance(model, dict)
        # Untouched user tuning survives...
        assert model["context_length"] == 8192
        assert config["context_compression"] == {"enabled": True}
        # ...but the endpoint is redirected at the proxy.
        assert model["base_url"].startswith("http://localhost:6666/proj/")
        assert model["default"] == "agentalloy-proxy"

    def test_repo_scope_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-wiring overwrites the model block in place (no stacking)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=5555, root=tmp_path, scope="repo")
        wire_compat("hermes-agent", port=9999, root=tmp_path, scope="repo")

        config = _read_repo_config(tmp_path)
        model = config["model"]
        assert isinstance(model, dict)
        assert ":9999/proj/" in model["base_url"]
        assert "5555" not in model["base_url"]

    def test_writes_envrc_direnv_carrier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiring creates an .envrc sourcing the HERMES_HOME env file."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        envrc = (tmp_path / ".envrc").read_text()
        assert "source_env .hermes/.agentalloy-env" in envrc

    def test_existing_envrc_content_preserved_and_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing .envrc keeps its content; re-wiring never stacks blocks."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        (tmp_path / ".envrc").write_text("export FOO=bar\n")

        wire_compat("hermes-agent", port=5555, root=tmp_path, scope="repo")
        wire_compat("hermes-agent", port=9999, root=tmp_path, scope="repo")

        envrc = (tmp_path / ".envrc").read_text()
        assert "export FOO=bar" in envrc
        assert envrc.count("source_env .hermes/.agentalloy-env") == 1

    def test_mise_env_carrier_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mise on PATH: wiring writes mise.toml with HERMES_HOME under [env]."""
        import tomllib

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        data = tomllib.loads((tmp_path / "mise.toml").read_text())
        assert data["env"]["HERMES_HOME"] == "{{config_root}}/.hermes"

    def test_mise_existing_env_table_gets_key_inserted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing [env] table gains the key without a duplicate table."""
        import tomllib

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        (tmp_path / "mise.toml").write_text('[tools]\nnode = "24"\n\n[env]\nFOO = "bar"\n')

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        data = tomllib.loads((tmp_path / "mise.toml").read_text())
        assert data["env"]["HERMES_HOME"] == "{{config_root}}/.hermes"
        assert data["env"]["FOO"] == "bar"
        assert data["tools"]["node"] == "24"

    def test_mise_rewire_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-wiring never stacks sentinel blocks or duplicates the key."""
        import tomllib

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=5555, root=tmp_path, scope="repo")
        wire_compat("hermes-agent", port=9999, root=tmp_path, scope="repo")

        content = (tmp_path / "mise.toml").read_text()
        assert content.count("HERMES_HOME") == 1
        tomllib.loads(content)

    def test_mise_created_file_is_auto_trusted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_mise_trust: list[Path],
    ) -> None:
        """A mise.toml we author in full is auto-trusted."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        assert stub_mise_trust == [tmp_path / "mise.toml"]

    def test_mise_preexisting_file_never_auto_trusted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stub_mise_trust: list[Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A pre-existing mise config is edited but never trusted on the user's behalf."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        (tmp_path / "mise.toml").write_text('[tools]\nnode = "24"\n')

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        assert stub_mise_trust == []
        assert "run `mise trust` once" in capsys.readouterr().err

    def test_no_managers_prints_manual_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Neither direnv nor mise: no carrier files, manual source hint printed."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(wire_harness, "_activation_managers", lambda: set())

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        assert not (tmp_path / ".envrc").exists()
        assert not (tmp_path / "mise.toml").exists()
        assert "source .hermes/.agentalloy-env" in capsys.readouterr().err

    def test_existing_envrc_wired_even_without_direnv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing .envrc is wired even when direnv isn't on PATH."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(wire_harness, "_activation_managers", lambda: set())
        (tmp_path / ".envrc").write_text("export FOO=bar\n")

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        envrc = (tmp_path / ".envrc").read_text()
        assert "source_env .hermes/.agentalloy-env" in envrc

    def test_gateway_restart_invoked_for_repo_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_gateway_restart: list[Path]
    ) -> None:
        """Wiring (re)starts the repo-scoped gateway so the config takes effect."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=6666, root=tmp_path, scope="repo")

        assert stub_gateway_restart == [tmp_path]

    def test_registry_install_writer_matches_live_wiring(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGISTRY['hermes-agent'].install_writer produces the real proxy wiring.

        Guards against the provider module diverging from the live
        ``_wire_proxy_hermes_agent`` path (it once wrote a dead SOUL.md/AGENTS.md
        markdown variant instead).
        """
        from agentalloy.providers import REGISTRY

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        writer = REGISTRY["hermes-agent"].install_writer
        assert writer is not None
        records = writer(6666, tmp_path, False)

        paths = {r.path for r in records}
        assert str(tmp_path / ".hermes" / "config.yaml") in paths
        assert str(tmp_path / ".hermes" / ".agentalloy-env") in paths
        # The dead markdown variant must stay dead.
        assert not (fake_home / ".hermes" / "SOUL.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()

        config = _read_repo_config(tmp_path)
        model = config["model"]
        assert isinstance(model, dict)
        token = encode_proj_token(tmp_path)
        assert model["base_url"] == f"http://localhost:6666/proj/{token}/v1"


class TestRestartHermesGateway:
    """_restart_hermes_gateway never fails the wiring; it degrades to guidance."""

    def test_hermes_missing_prints_manual_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(wire_harness.shutil, "which", lambda _: None)

        assert _real_restart_hermes_gateway(tmp_path) is False
        err = capsys.readouterr().err
        assert "hermes gateway restart" in err

    def test_restart_runs_with_repo_hermes_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            seen["cmd"] = cmd
            seen["env"] = kwargs["env"]

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            return Proc()

        monkeypatch.setattr(wire_harness.shutil, "which", lambda _: "/usr/bin/hermes")
        monkeypatch.setattr(wire_harness.subprocess, "run", fake_run)

        assert _real_restart_hermes_gateway(tmp_path) is True
        assert seen["cmd"] == ["/usr/bin/hermes", "gateway", "restart"]
        env = seen["env"]
        assert isinstance(env, dict)
        assert env["HERMES_HOME"] == str(tmp_path / ".hermes")

    def test_nonzero_exit_prints_manual_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> object:
            class Proc:
                returncode = 1
                stdout = ""
                stderr = "boom\n"

            return Proc()

        monkeypatch.setattr(wire_harness.shutil, "which", lambda _: "/usr/bin/hermes")
        monkeypatch.setattr(wire_harness.subprocess, "run", fake_run)

        assert _real_restart_hermes_gateway(tmp_path) is False
        err = capsys.readouterr().err
        assert "exited 1" in err
        assert "source .hermes/.agentalloy-env" in err


class TestExtractUpstream:
    """extract_upstream must handle both hermes config shapes: the inline
    model.base_url form and the shipping provider-reference form
    (model.provider: custom:<key> → custom_providers entry)."""

    def _write_home_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, object]
    ) -> None:
        home = tmp_path / "home"
        (home / ".hermes").mkdir(parents=True)
        (home / ".hermes" / "config.yaml").write_text(yaml.safe_dump(data))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    def test_inline_base_url_form(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {"model": {"base_url": "http://127.0.0.1:8080/v1/", "default": "qwen3"}},
        )
        up = extract_upstream(tmp_path)
        assert up is not None
        assert up.url == "http://127.0.0.1:8080/v1"
        assert up.model == "qwen3"
        assert up.key_env == "OPENAI_API_KEY"

    def test_provider_reference_resolves_custom_providers_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The shipping hermes format — this exact shape used to fall through
        # to "No upstream found" because model has no base_url.
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {
                "model": {"default": "qwen3.6-27b", "provider": "custom:llama-heavy"},
                "custom_providers": [
                    {
                        "name": "llama-heavy",
                        "base_url": "http://100.100.1.1:60000/v1",
                        "model": "qwen3.6-27b",
                    }
                ],
            },
        )
        up = extract_upstream(tmp_path)
        assert up is not None
        assert up.url == "http://100.100.1.1:60000/v1"
        assert up.model == "qwen3.6-27b"

    def test_provider_reference_matches_provider_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {
                "model": {"default": "m1", "provider": "custom:EP-1"},
                "custom_providers": [
                    {"name": "other", "base_url": "http://a/v1", "model": "mx"},
                    {"provider_key": "ep-1", "name": "one", "base_url": "http://b/v1"},
                ],
            },
        )
        up = extract_upstream(tmp_path)
        assert up is not None
        assert up.url == "http://b/v1"
        assert up.model == "m1"  # model.default wins over the entry's model

    def test_dangling_key_with_single_entry_adopts_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Observed in the wild: model.provider names a key no entry carries,
        # but there is exactly one endpoint — not ambiguous, adopt it.
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {
                "model": {"default": "qwen3.6-27b", "provider": "custom:my-endpoint"},
                "custom_providers": [
                    {"name": "llama-heavy", "base_url": "http://10.0.0.9:60000/v1"}
                ],
            },
        )
        up = extract_upstream(tmp_path)
        assert up is not None
        assert up.url == "http://10.0.0.9:60000/v1"

    def test_dangling_key_with_multiple_entries_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {
                "model": {"default": "m", "provider": "custom:nope"},
                "custom_providers": [
                    {"name": "a", "base_url": "http://a/v1"},
                    {"name": "b", "base_url": "http://b/v1"},
                ],
            },
        )
        assert extract_upstream(tmp_path) is None

    def test_non_custom_provider_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.providers.hermes_agent.install import extract_upstream

        self._write_home_config(
            tmp_path,
            monkeypatch,
            {"model": {"default": "claude-sonnet-5", "provider": "anthropic"}},
        )
        assert extract_upstream(tmp_path) is None

    def test_missing_config_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.providers.hermes_agent.install import extract_upstream

        home = tmp_path / "empty-home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        assert extract_upstream(tmp_path) is None
