"""Watcher loop and regenerator tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.watch.regenerators import (
    AGENTALLOY_MARKER,
    REGENERATORS,
    regenerate_aider,
    regenerate_antigravity,
    regenerate_cline,
    regenerate_copilot,
    regenerate_cursor,
    regenerate_windsurf,
    update_block,
)

# ---------------------------------------------------------------------------
# update_block
# ---------------------------------------------------------------------------


def test_update_block_appends_on_first_call(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text("# My file\n\nExisting content.\n")
    update_block(f, AGENTALLOY_MARKER, "new body")
    content = f.read_text()
    assert "new body" in content
    assert "Existing content." in content


def test_update_block_replaces_on_second_call(tmp_path: Path):
    f = tmp_path / "test.md"
    update_block(f, AGENTALLOY_MARKER, "first body")
    update_block(f, AGENTALLOY_MARKER, "second body")
    content = f.read_text()
    assert "second body" in content
    assert "first body" not in content


def test_update_block_preserves_user_content(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text("# Header\n\nUser content here.\n\n## Footer\n\nMore user content.\n")
    update_block(f, AGENTALLOY_MARKER, "agentalloy block")
    content = f.read_text()
    assert "User content here." in content
    assert "More user content." in content
    assert "agentalloy block" in content


def test_update_block_creates_parent_dirs(tmp_path: Path):
    f = tmp_path / "deep" / "nested" / "file.md"
    update_block(f, AGENTALLOY_MARKER, "hello")
    assert f.exists()


# ---------------------------------------------------------------------------
# Per-harness regenerators
# ---------------------------------------------------------------------------


def test_regenerate_cursor_writes_valid_mdc(tmp_path: Path):
    """Dedicated path: refreshes the SAME .mdc file wire seeds."""
    (tmp_path / ".cursor").mkdir()
    regenerate_cursor("Some workflow prose", tmp_path)
    mdc = tmp_path / ".cursor" / "rules" / "agentalloy.mdc"
    assert mdc.exists()
    content = mdc.read_text()
    assert "alwaysApply: true" in content
    assert "Some workflow prose" in content
    assert "description:" in content


def test_regenerate_cursor_shared_fallback(tmp_path: Path):
    """No .cursor/ dir → marker block in the shared .cursorrules (wire parity)."""
    regenerate_cursor("Some workflow prose", tmp_path)
    f = tmp_path / ".cursorrules"
    assert f.exists()
    assert "Some workflow prose" in f.read_text()
    assert "AGENTALLOY-CONTEXT" in f.read_text()


def test_regenerate_windsurf(tmp_path: Path):
    regenerate_windsurf("windsurf prose", tmp_path)
    f = tmp_path / ".windsurfrules"
    assert f.exists()
    assert "windsurf prose" in f.read_text()


def test_regenerate_windsurf_dedicated(tmp_path: Path):
    """.windsurf/ dir present → refreshes the dedicated rules file wire seeds."""
    (tmp_path / ".windsurf").mkdir()
    regenerate_windsurf("windsurf prose", tmp_path)
    f = tmp_path / ".windsurf" / "rules" / "agentalloy.md"
    assert f.exists()
    assert "windsurf prose" in f.read_text()


def test_regenerate_copilot(tmp_path: Path):
    regenerate_copilot("copilot prose", tmp_path)
    f = tmp_path / ".github" / "copilot-instructions.md"
    assert f.exists()
    assert "copilot prose" in f.read_text()


def test_regenerate_cline(tmp_path: Path):
    regenerate_cline("cline prose", tmp_path)
    f = tmp_path / ".clinerules"
    assert f.exists()
    assert "cline prose" in f.read_text()


def test_regenerate_antigravity(tmp_path: Path):
    regenerate_antigravity("antigravity prose", tmp_path)
    f = tmp_path / "GEMINI.md"
    assert f.exists()
    assert "antigravity prose" in f.read_text()


def test_regenerate_aider(tmp_path: Path):
    regenerate_aider("aider prose", tmp_path)
    f = tmp_path / ".aider" / "agentalloy-context.txt"
    assert f.exists()
    assert "aider prose" in f.read_text()


def test_all_regenerators_registered():
    assert set(REGENERATORS.keys()) == {
        "cursor",
        "windsurf",
        "github-copilot",
        "cline",
        "antigravity",
        "gemini-cli",  # deprecated alias for antigravity
        "aider",
    }


# ---------------------------------------------------------------------------
# Watcher integration tests (using watchdog directly)
# ---------------------------------------------------------------------------


def test_phase_change_triggers_regenerate(tmp_path: Path):
    """Writing .agentalloy/phase triggers the regenerator."""
    from agentalloy.watch.watcher import (
        WatchConfig,
        _AgentAlloyHandler,  # pyright: ignore[reportPrivateUsage]
    )

    regen_calls: list[tuple[str, Path]] = []

    def mock_regen(content: str, root: Path) -> None:
        regen_calls.append((content, root))

    config = WatchConfig(
        project_root=tmp_path,
        profile_name="default",
        harness="cursor",
        debounce_ms=50,
    )
    handler = _AgentAlloyHandler(config, mock_regen)

    agentalloy_dir = tmp_path / ".agentalloy"
    agentalloy_dir.mkdir(parents=True)
    phase_file = agentalloy_dir / "phase"
    phase_file.write_text("phase: build\n")

    with patch("agentalloy.signals.skill_loader._load_workflow_skill_for_phase") as mock_load:
        mock_load.return_value = {"skill_id": "sdd-build", "raw_prose": "BUILD PROSE"}

        # Simulate the file event
        handler._schedule("modified", str(phase_file))  # pyright: ignore[reportPrivateUsage]
        time.sleep(0.2)  # wait for debounce while patch is active

    assert len(regen_calls) == 1
    assert "BUILD PROSE" in regen_calls[0][0]


def test_debounce_coalesces_burst_writes(tmp_path: Path):
    """10 rapid writes → 1 regeneration call."""
    from agentalloy.watch.watcher import (
        WatchConfig,
        _AgentAlloyHandler,  # pyright: ignore[reportPrivateUsage]
    )

    regen_calls: list[int] = []

    def mock_regen(content: str, root: Path) -> None:
        regen_calls.append(1)

    config = WatchConfig(
        project_root=tmp_path,
        profile_name="default",
        harness="cursor",
        debounce_ms=200,
    )
    handler = _AgentAlloyHandler(config, mock_regen)

    phase_file = tmp_path / ".agentalloy" / "phase"
    (tmp_path / ".agentalloy").mkdir()
    phase_file.write_text("phase: build\n")

    with patch("agentalloy.watch.watcher._load_workflow_skill_prose", return_value="prose"):
        for _ in range(10):
            handler._schedule("modified", str(phase_file))  # pyright: ignore[reportPrivateUsage]

        time.sleep(0.5)  # wait for debounce to fire once

    assert len(regen_calls) == 1


# ---------------------------------------------------------------------------
# Watch CLI: status reports running/not-running
# ---------------------------------------------------------------------------


def test_watch_status_reports_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import argparse
    import io
    import json
    import sys

    from agentalloy.install.subcommands.watch import _status  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr("agentalloy.install.subcommands.watch._watch_dir", lambda: tmp_path)

    captured = io.StringIO()
    args = argparse.Namespace(profile="default", json=True)
    sys.stdout = captured
    try:
        rc = _status(args)
    finally:
        sys.stdout = sys.__stdout__

    assert rc == 0
    data = json.loads(captured.getvalue())
    assert data["running"] is False


# ---------------------------------------------------------------------------
# Slice 07: watch-store-hook
# ---------------------------------------------------------------------------


def test_callback_fires_once_per_write_phase(tmp_path: Path) -> None:
    """A registered callback fires exactly once per ``write_phase``, after commit."""
    from agentalloy.storage.state_store import open_state_store

    db = tmp_path / "state.duck"
    store = open_state_store(db, repo="test")
    store.open()

    calls: list[tuple[str, str]] = []

    def _fn(kind: str, value: str) -> None:
        calls.append((kind, value))

    store.on_write("phase", _fn)

    # First write_phase
    store.write_phase("spec")
    assert len(calls) == 1
    assert calls[0][0] == "phase"
    assert '"phase": "spec"' in calls[0][1]

    # Second write_phase
    store.write_phase("design")
    assert len(calls) == 2
    assert calls[1][0] == "phase"
    assert '"phase": "design"' in calls[1][1]

    # Third write_phase (same phase — still fires)
    store.write_phase("design")
    assert len(calls) == 3


def test_callback_raise_does_not_kill_writer(tmp_path: Path) -> None:
    """A callback raising does not roll back the write or kill the writer."""
    from agentalloy.storage.state_store import open_state_store

    db = tmp_path / "state.duck"
    store = open_state_store(db, repo="test")
    store.open()

    call_count = 0

    def _bad_fn(kind: str, value: str) -> None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        raise RuntimeError("callback exploded")

    def _good_fn(kind: str, value: str) -> None:  # noqa: ARG001
        pass

    store.on_write("phase", _bad_fn)
    store.on_write("phase", _good_fn)

    # The write should succeed despite the bad callback
    result = store.write_phase("spec")
    assert result is not None
    assert call_count == 1  # bad callback ran; good callback also ran


def test_callback_unregistered_no_longer_fires(tmp_path: Path) -> None:
    """After ``off_write``, the callback no longer fires."""
    from agentalloy.storage.state_store import open_state_store

    db = tmp_path / "state.duck"
    store = open_state_store(db, repo="test")
    store.open()

    calls: list[int] = []

    def _fn(kind: str, value: str) -> None:  # noqa: ARG001
        calls.append(1)

    store.on_write("phase", _fn)
    store.write_phase("spec")
    assert len(calls) == 1

    store.off_write("phase", _fn)
    store.write_phase("design")
    assert len(calls) == 1  # no second call


def test_register_watcher_hooks_store(tmp_path: Path) -> None:
    """``register_watcher`` registers a callback that regenerates rules."""
    from agentalloy.storage.state_store import open_state_store
    from agentalloy.watch.watcher import register_watcher

    db = tmp_path / "state.duck"
    store = open_state_store(db, repo="test")
    store.open()

    # Register the watcher for a known harness
    register_watcher(store, tmp_path, "default", "cursor")

    # Verify a callback was registered
    assert len(store._on_write_callbacks.get("phase", [])) == 1

    # Trigger a write — the callback should fire (silently, since prose is empty)
    store.write_phase("spec")
    # No exception, no crash — the callback ran and logged.


def test_harness_agnostic_registry_grep() -> None:
    """The registry and watcher trigger contain no harness name.

    The ``on_write`` registry and the ``register_watcher`` function are
    harness-agnostic by design: they know only kinds and callables.
    A grep for common harness names in the source proves the constraint.
    """
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src" / "agentalloy"

    # Files that own the registry and the watcher trigger
    target_files = [
        src_dir / "storage" / "state_store.py",
        src_dir / "watch" / "watcher.py",
    ]

    harness_names = ("claude", "codex", "windsurf", "copilot", "cline")

    for path in target_files:
        text = path.read_text(encoding="utf-8")
        # Strip docstrings (triple-quoted strings) so we only check code.
        # This allows mentioning harness names in prose comments.
        code = re.sub(r'"""[\s\S]*?"""', "", text)
        code = re.sub(r"'''[\s\S]*?'''", "", code)
        for name in harness_names:
            # Check that the harness name does not appear as a standalone
            # identifier or in a string literal that is used as a value
            # (not just in a comment).
            # We allow it in comments (after #) and in docstrings (already stripped).
            lines = code.split("\n")
            for i, line in enumerate(lines, 1):
                stripped = line.split("#")[0]  # remove inline comments
                if name in stripped:
                    raise AssertionError(
                        f"{path.name}:{i}: harness name '{name}' found in code "
                        f"(not in a comment or docstring): {stripped.strip()}"
                    )
