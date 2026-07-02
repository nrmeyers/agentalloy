"""Tests for Hermes Agent proxy wiring (per-repo HERMES_HOME interception)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentalloy.api.proxy_context import decode_proj_token, encode_proj_token
from tests._wire_compat import wire_compat


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
