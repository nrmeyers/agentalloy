"""Tests for Hermes Agent proxy wiring. Maps to Step 4."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.wire_harness import SENTINEL_BEGIN
from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestHermesAgentProxyWiring:
    """Tests for hermes-agent proxy wiring. Maps to Step 4."""

    def test_user_scope_writes_config_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User scope writes custom_providers block to ~/.hermes/config.yaml."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("hermes-agent", port=5555, root=tmp_path, scope="user")
        assert result["integration_vector"] == "proxy"

        config_path = fake_home / ".hermes" / "config.yaml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "custom_providers:" in content
        assert "agentalloy:" in content
        assert "base_url: http://localhost:5555/v1" in content
        assert "api_key: agentalloy" in content

    def test_user_scope_uses_sentinel_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User-scope config block is bounded by sentinel comments."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=5555, root=tmp_path, scope="user")
        content = (fake_home / ".hermes" / "config.yaml").read_text()
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_user_scope_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-running user-scope wiring replaces the existing block."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("hermes-agent", port=5555, root=tmp_path, scope="user")
        wire_compat("hermes-agent", port=9999, root=tmp_path, scope="user")
        content = (fake_home / ".hermes" / "config.yaml").read_text()
        assert "localhost:9999" in content
        assert "localhost:5555" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1

    def test_repo_scope_writes_agents_md(self, repo_root: Path) -> None:
        """Repo scope writes proxy instruction block to AGENTS.md."""
        result = wire_compat("hermes-agent", port=6666, root=repo_root, scope="repo")
        assert result["integration_vector"] == "proxy"

        agents_md = repo_root / "AGENTS.md"
        assert agents_md.exists()
        content = agents_md.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:6666" in content

    def test_repo_scope_preserves_existing_content(self, repo_root: Path) -> None:
        """Repo scope appends to existing AGENTS.md without clobbering it."""
        agents_md = repo_root / "AGENTS.md"
        agents_md.write_text("# Existing agents guidance\n\nKeep this.\n")
        wire_compat("hermes-agent", port=6666, root=repo_root, scope="repo")
        content = agents_md.read_text()
        assert "# Existing agents guidance" in content
        assert "Keep this." in content
        assert "localhost:6666" in content
