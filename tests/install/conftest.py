"""Shared pytest fixtures for install subcommand tests.

XDG isolation (redirecting XDG_CONFIG_HOME / XDG_DATA_HOME to per-test tmp
dirs) lives in the root conftest and applies to the whole suite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Set up a temporary XDG state directory for install tests.

    XDG_DATA_HOME -> tmp/.local/share (outputs_dir() appends 'agentalloy/outputs')
    XDG_CONFIG_HOME -> tmp/.config (user_config_dir() appends 'agentalloy')
    """
    config_dir = tmp_path / ".config"
    data_dir = tmp_path / ".local" / "share"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    yield config_dir, data_dir
    del os.environ["XDG_CONFIG_HOME"]
    del os.environ["XDG_DATA_HOME"]
