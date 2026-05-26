"""Tests for Tier 3 watcher skipping in proxy vs legacy paths. Maps to Step 6."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands.wire_harness import wire_harness


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestTier3WatcherBehavior:
    """Verify Tier 3 watcher is skipped in proxy mode but triggered in legacy mode."""

    def test_proxy_wiring_skips_tier3_watcher(self, repo_root: Path) -> None:
        """Proxy wiring never calls _wire_tier3_watcher_config."""
        with patch(
            "agentalloy.install.subcommands.wire_harness._wire_tier3_watcher_config"
        ) as mock_tier3:
            wire_harness("cursor", port=8000, root=repo_root)
            mock_tier3.assert_not_called()

    def test_legacy_wiring_triggers_tier3_watcher(self, repo_root: Path) -> None:
        """Legacy wiring calls _wire_tier3_watcher_config for Tier 3 harnesses."""
        (repo_root / ".cursor").mkdir()
        with patch(
            "agentalloy.install.subcommands.wire_harness._wire_tier3_watcher_config"
        ) as mock_tier3:
            wire_harness("cursor", port=8000, root=repo_root, legacy=True)
            mock_tier3.assert_called_once_with("cursor", repo_root)
