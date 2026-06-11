"""Tests for Claude Code proxy wiring. Maps to Step 8."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestClaudeCodeProxyWiring:
    """Tests for claude-code proxy wiring via ~/.agentalloy/claude-code-env.sh."""

    def test_claude_code_proxy_writes_env_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default claude-code wiring writes claude-code-env.sh to ~/.agentalloy/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("claude-code", port=7070, root=tmp_path)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "claude-code"

        env_path = fake_home / ".agentalloy" / "claude-code-env.sh"
        assert env_path.exists()
        content = env_path.read_text()
        # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL.
        assert "ANTHROPIC_BASE_URL=http://localhost:7070" in content
        assert "7070/v1" not in content
        assert "ANTHROPIC_API_KEY=" in content

    def test_claude_code_proxy_uses_sentinel_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env script block is bounded by sentinel comments."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("claude-code", port=7070, root=tmp_path)
        content = (fake_home / ".agentalloy" / "claude-code-env.sh").read_text()
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_claude_code_proxy_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running claude-code proxy wiring replaces the existing block."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        wire_compat("claude-code", port=7070, root=tmp_path)
        wire_compat("claude-code", port=8080, root=tmp_path)
        content = (fake_home / ".agentalloy" / "claude-code-env.sh").read_text()
        assert "localhost:8080" in content
        assert "localhost:7070" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1
