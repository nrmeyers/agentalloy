"""Tests for OpenCode proxy wiring (repo-local opencode.json provider block).

The old ``.opencode/.agentalloy-env`` + ``system-prompt.md`` carrier was dead:
OpenCode ignores ``OPENAI_API_BASE``, and its built-in openai provider speaks
the Responses API (``/v1/responses``), which the proxy does not serve. The
harness e2e matrix proved the working vector is a custom
``@ai-sdk/openai-compatible`` provider in repo-local ``opencode.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.api.proxy_context import encode_proj_token
from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _read_config(root: Path) -> dict[str, object]:
    data = json.loads((root / "opencode.json").read_text())
    assert isinstance(data, dict)
    return data


class TestOpenCodeProxyWiring:
    def test_writes_agentalloy_provider_block(self, repo_root: Path) -> None:
        result = wire_compat("opencode", port=4321, root=repo_root)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "opencode"

        config = _read_config(repo_root)
        provider = config["provider"]
        assert isinstance(provider, dict)
        agentalloy = provider["agentalloy"]
        assert isinstance(agentalloy, dict)
        # Chat Completions wire — NOT the built-in openai provider (Responses API).
        assert agentalloy["npm"] == "@ai-sdk/openai-compatible"
        options = agentalloy["options"]
        assert isinstance(options, dict)
        token = encode_proj_token(repo_root)
        assert options["baseURL"] == f"http://localhost:4321/proj/{token}/v1"
        assert options["apiKey"] == "agentalloy"
        assert config["model"] == "agentalloy/agentalloy-proxy"

    def test_never_writes_dead_env_carrier(self, repo_root: Path) -> None:
        """The pre-rewrite carriers must stay dead — opencode never read them."""
        wire_compat("opencode", port=4321, root=repo_root)
        assert not (repo_root / ".opencode" / ".agentalloy-env").exists()
        assert not (repo_root / ".opencode" / "system-prompt.md").exists()

    def test_merges_over_existing_config(self, repo_root: Path) -> None:
        """A pre-existing opencode.json keeps its other settings."""
        (repo_root / "opencode.json").write_text(
            json.dumps({"theme": "gruvbox", "provider": {"other": {"name": "Other"}}})
        )
        result = wire_compat("opencode", port=4321, root=repo_root)

        config = _read_config(repo_root)
        assert config["theme"] == "gruvbox"
        provider = config["provider"]
        assert isinstance(provider, dict)
        assert "other" in provider and "agentalloy" in provider
        # Original content captured so uninstall can restore it.
        entry = result["files_written"][0]
        assert entry["action"] == "injected_block"
        assert "gruvbox" in entry["original_content"]

    def test_rewire_is_idempotent(self, repo_root: Path) -> None:
        wire_compat("opencode", port=4321, root=repo_root)
        wire_compat("opencode", port=5555, root=repo_root)

        config = _read_config(repo_root)
        provider = config["provider"]
        assert isinstance(provider, dict)
        agentalloy = provider["agentalloy"]
        assert isinstance(agentalloy, dict)
        options = agentalloy["options"]
        assert isinstance(options, dict)
        assert "localhost:5555" in str(options["baseURL"])

    def test_invalid_existing_json_is_a_hard_error(self, repo_root: Path) -> None:
        (repo_root / "opencode.json").write_text("{not json")
        with pytest.raises(SystemExit):
            wire_compat("opencode", port=4321, root=repo_root)


def test_registry_install_writer_matches_live_wiring(tmp_path: Path) -> None:
    """REGISTRY['opencode'].install_writer produces the real proxy wiring.

    Guards against the provider module regressing to the no-op stub it once
    was (returned [] while the live wiring lived only in wire_harness).
    """
    from agentalloy.providers import REGISTRY

    writer = REGISTRY["opencode"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    assert records, "opencode install_writer must not be a no-op stub"
    assert [r.path for r in records] == [str(tmp_path / "opencode.json")]
    config = _read_config(tmp_path)
    assert config["model"] == "agentalloy/agentalloy-proxy"
