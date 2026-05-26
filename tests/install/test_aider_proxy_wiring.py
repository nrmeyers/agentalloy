"""Tests for aider proxy wiring via .aider.conf.yml. Maps to Step 3."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.wire_harness import wire_harness


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestAiderProxyWiring:
    """Tests for aider proxy wiring via .aider.conf.yml. Maps to Step 3."""

    def test_aider_proxy_writes_conf_yml(self, repo_root: Path) -> None:
        """Default aider wiring writes proxy config block to .aider.conf.yml."""
        result = wire_harness("aider", port=7777, root=repo_root)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "aider"

        conf = repo_root / ".aider.conf.yml"
        assert conf.exists()
        content = conf.read_text()
        assert "openai-api-base: http://localhost:7777/v1" in content
        assert "openai-api-key: agentalloy" in content
        assert "model: agentalloy-proxy" in content
        assert ".agentalloy-aider-instructions.md" in content

    def test_aider_proxy_uses_sentinel_markers(self, repo_root: Path) -> None:
        """Proxy block is bounded by sentinel comments for clean removal."""
        wire_harness("aider", port=7777, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_aider_proxy_idempotent(self, repo_root: Path) -> None:
        """Re-running aider proxy wiring replaces the existing block."""
        wire_harness("aider", port=7777, root=repo_root)
        wire_harness("aider", port=9999, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        assert "localhost:9999" in content
        assert "localhost:7777" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1

    def test_aider_proxy_appends_to_existing_conf(self, repo_root: Path) -> None:
        """Proxy block is appended when .aider.conf.yml already has user content."""
        conf = repo_root / ".aider.conf.yml"
        conf.write_text("auto-commits: false\n")
        wire_harness("aider", port=7777, root=repo_root)
        content = conf.read_text()
        assert "auto-commits: false" in content
        assert "openai-api-base" in content

    def test_aider_proxy_files_written_count(self, repo_root: Path) -> None:
        """Aider proxy wiring writes exactly one file entry."""
        result = wire_harness("aider", port=7777, root=repo_root)
        assert len(result["files_written"]) == 1
        entry = result["files_written"][0]
        assert entry["path"].endswith(".aider.conf.yml")
        assert entry["action"] == "injected_block"
