"""Tests for OpenCode proxy wiring. Maps to Step 5."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.wire_harness import SENTINEL_BEGIN
from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestOpenCodeProxyWiring:
    """Tests for opencode proxy wiring. Maps to Step 5."""

    def test_opencode_proxy_writes_env_file(self, repo_root: Path) -> None:
        """Default opencode wiring writes .opencode/.agentalloy-env with exports."""
        result = wire_compat("opencode", port=4321, root=repo_root)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "opencode"

        env_path = repo_root / ".opencode" / ".agentalloy-env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "export OPENAI_API_BASE=http://localhost:4321/v1" in content
        assert "export OPENAI_API_KEY=agentalloy" in content

    def test_opencode_proxy_writes_system_prompt(self, repo_root: Path) -> None:
        """Default opencode wiring also writes proxy guidance to system-prompt.md."""
        wire_compat("opencode", port=4321, root=repo_root)

        prompt_path = repo_root / ".opencode" / "system-prompt.md"
        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:4321" in content

    def test_opencode_proxy_files_written_count(self, repo_root: Path) -> None:
        """Opencode proxy wiring writes exactly two file entries (env + prompt)."""
        result = wire_compat("opencode", port=4321, root=repo_root)
        assert len(result["files_written"]) == 2
        paths = [e["path"] for e in result["files_written"]]
        assert any(".agentalloy-env" in p for p in paths)
        assert any("system-prompt.md" in p for p in paths)
