"""Tests for agentalloy.signals.skill_loader — extracted domain helpers.

The functions in skill_loader are pure-domain (no CLI deps); these tests
exercise them in isolation without going through the signal CLI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.support import seed_phase

# ---------------------------------------------------------------------------
# _read_phase
# ---------------------------------------------------------------------------


def test_read_phase_returns_none_when_missing(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    assert _read_phase(tmp_path) is None


def test_read_phase_reads_the_store_row(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase

    seed_phase(tmp_path, "build")
    assert _read_phase(tmp_path) == "build"


def test_read_phase_reads_a_bare_string_row(tmp_path: Path) -> None:
    """Pre-blob rows hold a bare phase string; they must still read."""
    from agentalloy.api.state_router import scoped_state_store
    from agentalloy.signals.skill_loader import _read_phase
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None
    scoped_state_store(store, tmp_path).write("phase", "spec")
    assert _read_phase(tmp_path) == "spec"


def test_read_phase_ignores_a_leftover_phase_file(tmp_path: Path) -> None:
    """A file left over from before the migration is not a source of truth."""
    from agentalloy.signals.skill_loader import _read_phase

    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    phase_file.write_text("phase: build\n")

    assert _read_phase(tmp_path) is None


# ---------------------------------------------------------------------------
# _read_lifecycle_mode / _write_lifecycle_mode
# ---------------------------------------------------------------------------


def test_lifecycle_mode_defaults_to_full_when_absent(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    # No .agentalloy/config at all -> historical behavior must be preserved.
    assert _read_lifecycle_mode(tmp_path) == "full"


def test_lifecycle_mode_round_trips_each_mode(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import (
        LIFECYCLE_MODES,
        _read_lifecycle_mode,
        _write_lifecycle_mode,
    )

    # Two-mode world after the hook transport was removed: full / off.
    assert LIFECYCLE_MODES == ("full", "off")
    for mode in LIFECYCLE_MODES:
        _write_lifecycle_mode(tmp_path, mode)
        assert (tmp_path / ".agentalloy" / "config").read_text() == f"lifecycle_mode: {mode}\n"
        assert _read_lifecycle_mode(tmp_path) == mode


def test_lifecycle_mode_legacy_assist_reads_as_off(tmp_path: Path) -> None:
    """Legacy ``assist`` collapsed to ``off`` when the hook transport was removed.

    It must NOT fall through to the ``full`` default (which would wrongly
    re-enable composition for repos that had opted into assist).
    """
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("lifecycle_mode: assist\n")

    assert _read_lifecycle_mode(tmp_path) == "off"


def test_lifecycle_mode_unknown_value_falls_back_to_full(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("lifecycle_mode: bananas\n")  # not a valid mode

    # An unrecognized value must never silently disable the lifecycle.
    assert _read_lifecycle_mode(tmp_path) == "full"


def test_lifecycle_mode_malformed_file_falls_back_to_full(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_lifecycle_mode

    config = tmp_path / ".agentalloy" / "config"
    config.parent.mkdir(parents=True)
    config.write_text(": : not yaml : :\n[broken")

    assert _read_lifecycle_mode(tmp_path) == "full"


def test_write_lifecycle_mode_rejects_invalid_mode(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _write_lifecycle_mode

    with pytest.raises(ValueError, match="invalid lifecycle mode"):
        _write_lifecycle_mode(tmp_path, "turbo")


# ---------------------------------------------------------------------------
# _write_phase_atomic
# ---------------------------------------------------------------------------


def test_write_phase_atomic_records_the_phase(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase, _write_phase_atomic

    _write_phase_atomic(tmp_path, "design")
    assert _read_phase(tmp_path) == "design"


def test_write_phase_atomic_overwrites_existing(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_phase, _write_phase_atomic

    _write_phase_atomic(tmp_path, "spec")
    _write_phase_atomic(tmp_path, "design")
    assert _read_phase(tmp_path) == "design"


def test_write_phase_atomic_needs_no_repo_directory(tmp_path: Path) -> None:
    """The row is keyed by repo identity; the repo need not exist on disk."""
    from agentalloy.signals.skill_loader import _read_phase, _write_phase_atomic

    nested = tmp_path / "project"
    _write_phase_atomic(nested, "build")
    assert _read_phase(nested) == "build"
    assert not (nested / ".agentalloy").exists()


# ---------------------------------------------------------------------------
# _write_phase_atomic / _read_transitioned_by — transition attribution
# ---------------------------------------------------------------------------


def test_transitioned_by_recorded_on_real_transition(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_transitioned_by, _write_phase_atomic

    _write_phase_atomic(tmp_path, "spec", session_key="sess-a")
    _write_phase_atomic(tmp_path, "design", session_key="sess-b")
    assert _read_transitioned_by(tmp_path) == "sess-b"


def test_transitioned_by_none_when_session_key_unknown(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _read_transitioned_by, _write_phase_atomic

    _write_phase_atomic(tmp_path, "design")  # e.g. a bare CLI phase set, no session
    assert _read_transitioned_by(tmp_path) is None


def test_transitioned_by_preserved_on_idempotent_rewrite(tmp_path: Path) -> None:
    """Rewriting the SAME phase must not clobber who caused the last real transition."""
    from agentalloy.signals.skill_loader import _read_transitioned_by, _write_phase_atomic

    _write_phase_atomic(tmp_path, "design", session_key="sess-a")
    _write_phase_atomic(tmp_path, "design", session_key="sess-b")  # idempotent — no-op actor
    assert _read_transitioned_by(tmp_path) == "sess-a"


def test_transitioned_by_absent_with_no_session_history() -> None:
    from agentalloy.signals.skill_loader import _read_transitioned_by

    assert _read_transitioned_by(Path("/no/such/repo/xyz")) is None


# ---------------------------------------------------------------------------
# _record_phase_start_ref / phase-start ref stamping on transition
# ---------------------------------------------------------------------------


def _rev_parse_run(sha: str):
    """A fake subprocess.run that only intercepts ``git rev-parse``."""

    orig = subprocess.run

    class _R:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode = rc
            self.stdout = out

    def fake(args: Any, **kw: Any) -> Any:
        if isinstance(args, list) and "rev-parse" in args:
            return _R(0, f"{sha}\n")
        return orig(args, **kw)

    return fake


def test_record_phase_start_ref_writes_markered_sha(tmp_path: Path) -> None:
    """A phase transition stamps the current HEAD into the store's phase blob."""
    from agentalloy.signals.skill_loader import _record_phase_start_ref
    from agentalloy.storage.state_store import DuckDBStateStore

    db = tmp_path / "test.db"
    store = DuckDBStateStore(db).open()
    store.migrate()
    store.write_phase("build")  # need a phase row for _record_phase_start_ref to find
    with (
        patch("agentalloy.signals.skill_loader._phase_view", return_value=store),
        patch(
            "agentalloy.signals.skill_loader.subprocess.run", side_effect=_rev_parse_run("deadbeef")
        ),
    ):
        _record_phase_start_ref(tmp_path)
    got = store.read_phase()
    assert got is not None
    assert got.phase_start_ref == "deadbeef"
    store.close()


def test_record_phase_start_ref_fail_soft_without_git(tmp_path: Path) -> None:
    """No git / git failure: marker untouched, never raises."""
    from agentalloy.signals.skill_loader import _record_phase_start_ref
    from agentalloy.storage.state_store import DuckDBStateStore

    db = tmp_path / "test.db"
    store = DuckDBStateStore(db).open()
    store.migrate()
    store.write_phase("build")
    with (
        patch("agentalloy.signals.skill_loader._phase_view", return_value=store),
        patch(
            "agentalloy.signals.skill_loader.subprocess.run",
            side_effect=FileNotFoundError("no git"),
        ),
    ):
        _record_phase_start_ref(tmp_path)  # must not raise
    got = store.read_phase()
    assert got is not None
    assert got.phase_start_ref is None
    store.close()


def test_record_phase_start_ref_ignores_empty_rev(tmp_path: Path) -> None:
    """A rev-parse that succeeds but yields no SHA leaves no marker."""
    from agentalloy.signals.skill_loader import _record_phase_start_ref
    from agentalloy.storage.state_store import DuckDBStateStore

    class _R:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode = rc
            self.stdout = out

    db = tmp_path / "test.db"
    store = DuckDBStateStore(db).open()
    store.migrate()
    store.write_phase("build")
    with (
        patch("agentalloy.signals.skill_loader._phase_view", return_value=store),
        patch(
            "agentalloy.signals.skill_loader.subprocess.run",
            return_value=_R(0, ""),
        ),
    ):
        _record_phase_start_ref(tmp_path)
    got = store.read_phase()
    assert got is not None
    assert got.phase_start_ref is None
    store.close()


def test_write_phase_atomic_stamps_phase_start_ref_on_real_transition(tmp_path: Path) -> None:
    """Entering a new phase stamps the marker; an idempotent rewrite does not."""
    from agentalloy.signals.skill_loader import _write_phase_atomic
    from agentalloy.storage.state_store import DuckDBStateStore

    db = tmp_path / "test.db"
    store = DuckDBStateStore(db).open()
    store.migrate()

    with patch("agentalloy.signals.skill_loader._phase_view", return_value=store):
        with patch(
            "agentalloy.signals.skill_loader.subprocess.run", side_effect=_rev_parse_run("sha-1")
        ):
            _write_phase_atomic(tmp_path, "build")  # prev None != "build" -> stamp
        got = store.read_phase()
        assert got is not None
        assert got.phase_start_ref == "sha-1"

        # Idempotent rewrite (prev == phase) leaves the prior marker untouched.
        with patch(
            "agentalloy.signals.skill_loader.subprocess.run", side_effect=_rev_parse_run("sha-2")
        ):
            _write_phase_atomic(tmp_path, "build")
        got = store.read_phase()
        assert got is not None
        assert got.phase_start_ref == "sha-1"
    store.close()


# ---------------------------------------------------------------------------
# _load_workflow_skill_for_phase — packs fallback
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_load_workflow_skill_for_phase_falls_back_to_packs(tmp_path: Path) -> None:
    """When DB access raises an exception, fall through to _load_workflow_skill_from_packs."""
    from agentalloy.signals.skill_loader import _load_workflow_skill_for_phase

    skill_data: dict[str, Any] = {
        "skill_id": "sdd-build-packs",
        "skill_class": "workflow",
        "raw_prose": "Build phase instructions.",
        "applies_to_phases": ["build"],
        "exit_gates": {},
        "signal_keywords": ["done", "ready"],
    }

    with (
        patch("agentalloy.profiles.detect_profile", side_effect=RuntimeError("db broken")),
        patch(
            "agentalloy.signals.skill_loader._load_workflow_skill_from_packs",
            return_value=skill_data,
        ) as mock_packs,
    ):
        result = _load_workflow_skill_for_phase("build")
        mock_packs.assert_called_once_with("build")

    assert result is not None
    assert result["skill_id"] == "sdd-build-packs"


def test_load_workflow_skill_returns_none_for_unknown_phase() -> None:
    from agentalloy.signals.skill_loader import _load_workflow_skill_for_phase

    with (
        patch("agentalloy.profiles.detect_profile", return_value=None),
        patch(
            "agentalloy.profiles.profile_datastore_path",
            return_value=Path("/nonexistent/db.duck"),
        ),
        patch(
            "agentalloy.signals.skill_loader._load_workflow_skill_from_packs",
            return_value=None,
        ),
    ):
        result = _load_workflow_skill_for_phase("nonexistent_phase")

    assert result is None


# ---------------------------------------------------------------------------
# _load_workflow_skill_for_phase — shipped-first lock + invariant guard
# ---------------------------------------------------------------------------


def test_workflow_override_supplies_only_prose(tmp_path: Path) -> None:
    """A profile override contributes raw_prose (+domain_tags); the load-bearing
    structured fields (exit_gates etc.) are re-sourced from the shipped skill."""
    from agentalloy.signals import skill_loader
    from agentalloy.signals.invariants import derive_invariants

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    # Reworded prose that still contains every load-bearing invariant token.
    reworded = "REWORDED design guidance. Keeps: " + " ".join(derive_invariants(shipped))

    with patch.object(
        skill_loader, "_load_workflow_prose_override", return_value=(reworded, ["t"])
    ):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == reworded  # override prose applied
    assert result["exit_gates"] == shipped["exit_gates"]  # locked: from shipped
    assert result["domain_tags"] == ["t"]


def test_workflow_override_missing_invariant_falls_back_to_shipped(tmp_path: Path) -> None:
    from agentalloy.signals import skill_loader
    from agentalloy.signals.invariants import derive_invariants

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    assert derive_invariants(shipped)  # design has load-bearing tokens to drop

    bad = "REWORDED but drops every load-bearing path and command."
    with patch.object(skill_loader, "_load_workflow_prose_override", return_value=(bad, None)):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == shipped["raw_prose"]  # shipped prose served
    assert result["exit_gates"] == shipped["exit_gates"]


def test_workflow_no_override_returns_shipped(tmp_path: Path) -> None:
    from agentalloy.signals import skill_loader

    shipped = skill_loader._load_workflow_skill_from_packs("design")
    assert shipped is not None
    with patch.object(skill_loader, "_load_workflow_prose_override", return_value=(None, None)):
        result = skill_loader._load_workflow_skill_for_phase("design")

    assert result is not None
    assert result["raw_prose"] == shipped["raw_prose"]
    assert result["exit_gates"] == shipped["exit_gates"]


# ---------------------------------------------------------------------------
# _build_predicate_context
# ---------------------------------------------------------------------------


def test_build_predicate_context_basic(tmp_path: Path) -> None:
    from agentalloy.signals.predicates import PredicateContext
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="build", prompt_text="hello")
    assert isinstance(ctx, PredicateContext)
    assert ctx.project_root == tmp_path
    assert ctx.current_phase == "build"
    assert ctx.recent_prompt_text == "hello"
    assert ctx.recent_tool_use is None
    assert hasattr(ctx, "store")  # store replaces contracts_root


def test_build_predicate_context_with_tool(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(
        tmp_path,
        phase="spec",
        tool_name="git commit",
        tool_path="/repo",
    )
    assert ctx.recent_tool_use == {"tool": "git commit", "path": "/repo", "args": {}}


def test_build_predicate_context_no_tool(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="design")
    assert ctx.recent_tool_use is None


def test_build_predicate_context_no_phase(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase=None)
    assert ctx.current_phase is None


def test_build_predicate_context_file_events(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    events = [tmp_path / "a.py", tmp_path / "b.py"]
    ctx = _build_predicate_context(tmp_path, phase="build", file_events=events)
    assert ctx.file_events_since == events


def test_build_predicate_context_empty_file_events(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import _build_predicate_context

    ctx = _build_predicate_context(tmp_path, phase="build")
    assert ctx.file_events_since == []


# ---------------------------------------------------------------------------
# Runtime-state relocation (AGENTALLOY_RUNTIME_STATE_DIR)
# ---------------------------------------------------------------------------


class TestRuntimeStateRelocation:
    """Cadence keys (composed, cursor, etc.) are now store-only.

    The old env-var-based relocation to ``AGENTALLOY_RUNTIME_STATE_DIR``
    and disk fallbacks are sunset. These tests verify the current store-backed
    behavior: writes go to the bound process store, reads come from the same
    store, and ``_state_view(None)`` means no writes happen.
    """

    def test_composed_writes_to_store(self, tmp_path: Path) -> None:
        """_write_composed_atomic writes to the bound process store."""
        from agentalloy.signals.skill_loader import _read_composed, _write_composed_atomic

        repo = tmp_path / "repo"
        repo.mkdir()

        _write_composed_atomic(repo, "build/thing.md")

        # No disk file — cadence is store-only.
        assert not (repo / ".agentalloy" / "composed").exists()
        # Read comes back from the bound store.
        assert _read_composed(repo) == "build/thing.md"

    def test_cursor_writes_to_store(self, tmp_path: Path) -> None:
        """_write_cursor_atomic writes to the bound process store, not disk."""
        from agentalloy.signals.skill_loader import _read_cursor, _write_cursor_atomic

        repo = tmp_path / "repo"
        repo.mkdir()

        _write_cursor_atomic(repo, "build/thing.md")

        # No disk file — cursor is store-only.
        assert not (repo / ".agentalloy" / "cursor").exists()
        assert _read_cursor(repo) == "build/thing.md"

    def test_composed_overwrites_in_store(self, tmp_path: Path) -> None:
        """Subsequent writes replace the prior value in the store."""
        from agentalloy.signals.skill_loader import _read_composed, _write_composed_atomic

        repo = tmp_path / "repo"
        repo.mkdir()

        _write_composed_atomic(repo, "spec/thing.md")
        assert _read_composed(repo) == "spec/thing.md"

        _write_composed_atomic(repo, "build/thing.md")
        assert _read_composed(repo) == "build/thing.md"
        assert not (repo / ".agentalloy" / "composed").exists()

    def test_no_store_drops_cadence_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no store is bound, cadence writes are silently dropped."""
        from agentalloy.signals.skill_loader import _read_composed, _write_composed_atomic
        from agentalloy.storage.state_store import bind_process_store, process_store

        repo = tmp_path / "repo"
        repo.mkdir()

        # Unbind the store temporarily
        bound = process_store()
        bind_process_store(None)
        try:
            _write_composed_atomic(repo, "build/thing.md")
            assert _read_composed(repo) is None
        finally:
            # Restore the original store
            bind_process_store(bound)

    def test_clear_state_removes_legacy_disk_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_clear_state removes legacy disk copies best-effort; cadence is
        now store-only so the store path is the authoritative one."""
        from agentalloy.signals.skill_loader import (
            _clear_state,
        )

        repo = tmp_path / "repo"
        (repo / ".agentalloy").mkdir(parents=True)
        (repo / ".agentalloy" / "composed").write_text("stale\n")

        _clear_state(repo, "composed")

        # Legacy disk copy removed.
        assert not (repo / ".agentalloy" / "composed").exists()


# ---------------------------------------------------------------------------
# ensure_migrated — auto-migrate legacy flat contracts on first read
# ---------------------------------------------------------------------------


def _legacy_contract(root: Path, rel: str, phase: str) -> Path:
    p = root / ".agentalloy" / "contracts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nphase: {phase}\ntask_slug: {p.stem}\ndomain_tags: [x]\n---\nbody\n")
    return p


def test_ensure_migrated_moves_and_rewrites_cursor(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import ensure_migrated
    from agentalloy.storage.state_store import process_store

    _legacy_contract(tmp_path, "build/01-a.md", "build")
    _legacy_contract(tmp_path, "flat.md", "spec")
    (tmp_path / ".agentalloy" / "cursor").write_text("build/01-a.md")

    moved = ensure_migrated(tmp_path)

    # Step 1: flat→tree (2 files), Step 2: tree→store (2 contracts) = 4 total.
    assert moved == 4
    # Flat layout moved to tree on disk, then tree → store (disk files removed).
    assert not (tmp_path / ".agentalloy" / "contracts" / "build" / "01-a.md").is_file()
    assert not (tmp_path / ".agentalloy" / "contracts" / "flat.md").is_file()
    # Store now holds the contracts (queried via the bound process store).
    store = process_store()
    assert store is not None
    contracts = store.list_contracts()
    ids = {c["contract_id"] for c in contracts}
    assert "01-a" in ids
    assert "flat" in ids
    # Cursor follows the move into the store (no disk file).
    # The cursor value is the store key, not a disk path.
    assert (tmp_path / ".agentalloy" / "cursor").read_text().strip() == "active/build/01-a.md"


def test_ensure_migrated_noop_when_already_tree(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import ensure_migrated

    _legacy_contract(tmp_path, "active/build/01-a.md", "build")  # already tree layout
    # tree→store migration still runs: 0 flat + 1 store = 1
    assert ensure_migrated(tmp_path) == 1


def test_ensure_migrated_noop_when_no_contracts(tmp_path: Path) -> None:
    from agentalloy.signals.skill_loader import ensure_migrated

    assert ensure_migrated(tmp_path) == 0
