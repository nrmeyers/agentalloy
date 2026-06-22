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


# Repo-root ``.agentalloy/`` — a developer's live, gitignored phase/config state.
_REPO_AGENTALLOY = Path(__file__).resolve().parents[2] / ".agentalloy"


@pytest.fixture(autouse=True)
def _guard_real_repo_agentalloy() -> Iterator[None]:
    """Fail (and restore) if an install test mutates the real repo's ``.agentalloy/``.

    An install test that resolves the cwd repo root (e.g. ``uninstall()``'s
    ``root or _repo_root()`` default) and tears it down silently wipes a
    developer's live phase/config when the suite runs from the project root —
    pass an explicit ``root=tmp_path`` instead. This guard snapshots the dir,
    restores it on any change, and fails the offending test so the missing
    ``root=`` is caught at the source. No-op in CI, where a fresh clone has no
    ``.agentalloy/``.
    """
    root = _REPO_AGENTALLOY
    if not root.exists():
        yield
        return
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    yield
    current = {p for p in root.rglob("*") if p.is_file()}
    mutated = current != set(snapshot) or any(
        p.read_bytes() != snapshot[p] for p in snapshot if p.exists()
    )
    if mutated:
        for extra in current - set(snapshot):
            extra.unlink(missing_ok=True)
        for p, data in snapshot.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        pytest.fail(
            "an install test mutated the real repo .agentalloy/ — "
            "pass an explicit root=tmp_path to uninstall()/teardown helpers"
        )
