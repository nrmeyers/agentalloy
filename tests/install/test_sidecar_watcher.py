"""Tests for sidecar watcher skipping in proxy vs legacy paths. Maps to Step 6."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands.wire_harness import wire_harness


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestSidecarWatcherBehavior:
    """Verify sidecar watcher is skipped in proxy mode but triggered in legacy mode."""

    def test_proxy_wiring_skips_sidecar_watcher(self, repo_root: Path) -> None:
        """Proxy wiring never calls _wire_sidecar_watcher_config — use a truly
        proxy-wired harness (aider) for this assertion rather than a sidecar
        harness (cursor) which is now wired via legacy=True."""
        with patch(
            "agentalloy.install.subcommands.wire_harness._wire_sidecar_watcher_config"
        ) as mock_sidecar:
            wire_harness("aider", port=8000, root=repo_root)
            mock_sidecar.assert_not_called()

    def test_legacy_wiring_triggers_sidecar_watcher(self, repo_root: Path) -> None:
        """Legacy wiring calls _wire_sidecar_watcher_config for sidecar harnesses."""
        (repo_root / ".cursor").mkdir()
        with patch(
            "agentalloy.install.subcommands.wire_harness._wire_sidecar_watcher_config"
        ) as mock_sidecar:
            wire_harness("cursor", port=8000, root=repo_root, legacy=True)
            mock_sidecar.assert_called_once_with("cursor", repo_root)
