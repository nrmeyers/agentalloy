"""Tests for per-repo telemetry scoping.

Covers the ``repo`` column round-trip, the prefix-matching repo filter on
``aggregate_savings`` (default = current repo,
``--all`` = aggregate), the schema migration that backfills the column on a
pre-existing DB, and the CLI scope-resolution helpers.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from agentalloy.storage.protocols import CompositionTrace
from agentalloy.storage.telemetry_store import (
    DuckDBTelemetryStore,
    _repo_clause,
    open_telemetry_store,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_trace(
    trace_id: str,
    *,
    repo: str | None = None,
    phase: str = "build",
    status: str = "proxy_composed",
    event_type: str = "proxy_request",
    tokens_returned: int = 0,
    tokens_flat_equivalent: int = 0,
) -> CompositionTrace:
    return CompositionTrace(
        trace_id=trace_id,
        request_ts=int(time.time() * 1000),
        phase=phase,
        task_prompt="test task",
        status=status,
        event_type=event_type,
        repo=repo,
        tokens_returned=tokens_returned,
        tokens_flat_equivalent=tokens_flat_equivalent,
    )


@pytest.fixture
def store(tmp_path: Path) -> DuckDBTelemetryStore:  # type: ignore[misc]
    s = open_telemetry_store(tmp_path / "telemetry.duck")
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# _repo_clause unit
# ---------------------------------------------------------------------------


def test_repo_clause_none_is_empty() -> None:
    clause, params = _repo_clause(None)
    assert clause == ""
    assert params == []


def test_repo_clause_matches_exact_and_subtree() -> None:
    clause, params = _repo_clause("/home/u/repo")
    assert clause == "(repo = ? OR repo LIKE ?)"
    assert params == ["/home/u/repo", "/home/u/repo/%"]


def test_repo_clause_strips_trailing_slash() -> None:
    _, params = _repo_clause("/home/u/repo/")
    assert params == ["/home/u/repo/", "/home/u/repo/%"]


# ---------------------------------------------------------------------------
# repo column round-trip
# ---------------------------------------------------------------------------


def test_repo_roundtrips_via_query(store: DuckDBTelemetryStore) -> None:
    store.record_composition_trace(_mk_trace("t-repo", repo="/home/u/repo"))
    store.record_composition_trace(_mk_trace("t-none", repo=None))
    rows = {r.trace_id: r for r in store.query_traces(limit=10)}
    assert rows["t-repo"].repo == "/home/u/repo"
    assert rows["t-none"].repo is None


# ---------------------------------------------------------------------------
# aggregate_savings scoping
# ---------------------------------------------------------------------------


def test_savings_scoped_to_repo(store: DuckDBTelemetryStore) -> None:
    store.record_composition_trace(
        _mk_trace("a1", repo="/repo/a", tokens_returned=100, tokens_flat_equivalent=400)
    )
    store.record_composition_trace(
        _mk_trace("b1", repo="/repo/b", tokens_returned=50, tokens_flat_equivalent=300)
    )

    only_a = store.aggregate_savings("/repo/a")
    assert only_a["total_composes"] == 1
    assert only_a["tokens_returned"] == 100
    assert only_a["tokens_flat_equivalent"] == 400

    all_repos = store.aggregate_savings()
    assert all_repos["total_composes"] == 2
    assert all_repos["tokens_returned"] == 150


def test_savings_repo_prefix_matches_subdirectory(store: DuckDBTelemetryStore) -> None:
    """A trace recorded from a subdirectory still counts for the repo root."""
    store.record_composition_trace(
        _mk_trace("sub", repo="/repo/a/services/api", tokens_returned=10, tokens_flat_equivalent=40)
    )
    scoped = store.aggregate_savings("/repo/a")
    assert scoped["total_composes"] == 1


def test_savings_excludes_unattributed_from_repo_scope(store: DuckDBTelemetryStore) -> None:
    store.record_composition_trace(
        _mk_trace("legacy", repo=None, tokens_returned=99, tokens_flat_equivalent=200)
    )
    # The current-repo view excludes the unattributed row...
    assert store.aggregate_savings("/repo/a")["total_composes"] == 0
    # ...but the all-repos view includes it.
    assert store.aggregate_savings()["total_composes"] == 1


def test_savings_prefix_does_not_leak_sibling_repo(store: DuckDBTelemetryStore) -> None:
    """``/repo/a`` must not match ``/repo/ab`` (a different repo)."""
    store.record_composition_trace(
        _mk_trace("ab", repo="/repo/ab", tokens_returned=10, tokens_flat_equivalent=40)
    )
    assert store.aggregate_savings("/repo/a")["total_composes"] == 0


# Schema migration: the v5.3 additive-ALTER ``repo``-column backfill is obsolete
# in v5 — telemetry.duck is created from a single canonical CREATE (every column
# folded in, ``DuckDBTelemetryStore`` does no per-open ALTER), so a "pre-existing
# DB missing the repo column" can no longer occur. The former
# ``test_migration_adds_repo_column`` exercised that deleted code path and was
# removed.


# ---------------------------------------------------------------------------
# CLI scope resolution
# ---------------------------------------------------------------------------


def test_resolve_scope_all_returns_none() -> None:
    from agentalloy.install.subcommands import telemetry as tel

    args = argparse.Namespace(all_repos=True)
    assert tel._resolve_scope(args) is None


def test_resolve_scope_default_is_current_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentalloy.install.subcommands import telemetry as tel

    monkeypatch.setattr(tel, "_current_repo_key", lambda: "/repo/here")
    args = argparse.Namespace(all_repos=False)
    assert tel._resolve_scope(args) == "/repo/here"


def test_current_repo_key_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``git rev-parse`` fails, the cwd is used as the repo key."""
    import subprocess

    from agentalloy.install.subcommands import telemetry as tel

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: Path("/fallback/cwd")))
    assert tel._current_repo_key() == "/fallback/cwd"


def test_scope_label() -> None:
    from agentalloy.install.subcommands import telemetry as tel

    assert tel._scope_label(None) == "all repos"
    assert tel._scope_label("/repo/a") == "this repo · /repo/a"
