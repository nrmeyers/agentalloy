"""Tests for aider proxy wiring via .aider.conf.yml. Maps to Step 3."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestAiderProxyWiring:
    """Tests for aider proxy wiring via .aider.conf.yml. Maps to Step 3."""

    def test_aider_proxy_writes_conf_yml(self, repo_root: Path) -> None:
        """Default aider wiring writes proxy config block to .aider.conf.yml."""
        result = wire_compat("aider", port=7777, root=repo_root)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "aider"

        from agentalloy.api.proxy_context import encode_proj_token

        conf = repo_root / ".aider.conf.yml"
        assert conf.exists()
        content = conf.read_text()
        token = encode_proj_token(repo_root)
        assert f"openai-api-base: http://localhost:7777/proj/{token}/v1" in content
        assert "openai-api-key: agentalloy" in content
        assert "model: openai/agentalloy-proxy" in content
        # Proxy mode does NOT create a separate instructions file
        assert ".agentalloy-aider-instructions.md" not in content

    def test_aider_proxy_uses_sentinel_markers(self, repo_root: Path) -> None:
        """Proxy block is bounded by sentinel comments for clean removal."""
        wire_compat("aider", port=7777, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_aider_proxy_idempotent(self, repo_root: Path) -> None:
        """Re-running aider proxy wiring replaces the existing block."""
        wire_compat("aider", port=7777, root=repo_root)
        wire_compat("aider", port=9999, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        assert "localhost:9999" in content
        assert "localhost:7777" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1

    def test_aider_proxy_appends_to_existing_conf(self, repo_root: Path) -> None:
        """Proxy block is appended when .aider.conf.yml already has user content."""
        conf = repo_root / ".aider.conf.yml"
        conf.write_text("auto-commits: false\n")
        wire_compat("aider", port=7777, root=repo_root)
        content = conf.read_text()
        assert "auto-commits: false" in content
        assert "openai-api-base" in content

    def test_aider_proxy_files_written_count(self, repo_root: Path) -> None:
        """Aider proxy wiring writes exactly one file entry."""
        result = wire_compat("aider", port=7777, root=repo_root)
        assert len(result["files_written"]) == 1
        entry = result["files_written"][0]
        assert entry["path"].endswith(".aider.conf.yml")
        assert entry["action"] == "injected_block"

    def test_aider_proxy_stored_sha_covers_inner_content(self, repo_root: Path) -> None:
        """bughunt 6.9: the stored content_sha256 covers the content BETWEEN
        the sentinels (markers excluded) — the convention the re-wire and
        uninstall tamper checks hash against. Storing the sha of the full
        block (markers included) made every re-wire/uninstall a false hit."""
        from agentalloy.install.subcommands.wire_harness import _sha256

        result = wire_compat("aider", port=7777, root=repo_root)
        entry = result["files_written"][0]
        assert entry["path"].endswith(".aider.conf.yml")

        content = (repo_root / ".aider.conf.yml").read_text()
        begin = content.index(entry["sentinel_begin"]) + len(entry["sentinel_begin"])
        end = content.index(entry["sentinel_end"])
        inner = content[begin:end].strip()
        assert entry["content_sha256"] == _sha256(inner)
        # Sanity: the inner content is the proxy config, not the markers.
        assert "openai-api-base" in inner
        assert "agentalloy install" not in inner

    def test_aider_conf_read_entry_sha_covers_inner_content(self, repo_root: Path) -> None:
        """bughunt 6.9 (instructions vector): same convention for the
        .aider.conf.yml read-list block written by _wire_aider_conf."""
        from agentalloy.install.subcommands.wire_harness import (
            _sha256,
            _wire_aider_conf,
        )

        entries = _wire_aider_conf(repo_root)
        entry = entries[0]
        assert entry["path"].endswith(".aider.conf.yml")

        content = (repo_root / ".aider.conf.yml").read_text()
        begin = content.index(entry["sentinel_begin"]) + len(entry["sentinel_begin"])
        end = content.index(entry["sentinel_end"])
        inner = content[begin:end].strip()
        assert entry["content_sha256"] == _sha256(inner)
        assert inner == "read:\n  - .agentalloy-aider-instructions.md"
