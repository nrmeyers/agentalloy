"""Tests for per-repo telemetry scoping.

Covers the ``repo`` column round-trip, the prefix-matching repo filter on
``aggregate_savings`` / ``aggregate_hook_coverage`` (default = current repo,
``--all`` = aggregate), the schema migration that backfills the column on a
pre-existing DB, and the CLI scope-resolution helpers.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from agentalloy.storage.vector_store import (
    CompositionTrace,
    VectorStore,
    _repo_clause,
    open_or_create,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_trace(
    trace_id: str,
    *,
    repo: str | None = None,
    phase: str = "build",
    status: str = "compose",
    event_type: str = "compose",
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
def store(tmp_path: Path) -> VectorStore:  # type: ignore[misc]
    with open_or_create(tmp_path / "test.duck") as s:
        yield s


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


def test_repo_roundtrips_via_query(store: VectorStore) -> None:
    store.record_composition_trace(_mk_trace("t-repo", repo="/home/u/repo"))
    store.record_composition_trace(_mk_trace("t-none", repo=None))
    rows = {r.trace_id: r for r in store.query_traces(limit=10)}
    assert rows["t-repo"].repo == "/home/u/repo"
    assert rows["t-none"].repo is None


# ---------------------------------------------------------------------------
# aggregate_savings scoping
# ---------------------------------------------------------------------------


def test_savings_scoped_to_repo(store: VectorStore) -> None:
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


def test_savings_repo_prefix_matches_subdirectory(store: VectorStore) -> None:
    """A trace recorded from a subdirectory still counts for the repo root."""
    store.record_composition_trace(
        _mk_trace("sub", repo="/repo/a/services/api", tokens_returned=10, tokens_flat_equivalent=40)
    )
    scoped = store.aggregate_savings("/repo/a")
    assert scoped["total_composes"] == 1


def test_savings_excludes_unattributed_from_repo_scope(store: VectorStore) -> None:
    store.record_composition_trace(
        _mk_trace("legacy", repo=None, tokens_returned=99, tokens_flat_equivalent=200)
    )
    # The current-repo view excludes the unattributed row...
    assert store.aggregate_savings("/repo/a")["total_composes"] == 0
    # ...but the all-repos view includes it.
    assert store.aggregate_savings()["total_composes"] == 1


def test_savings_prefix_does_not_leak_sibling_repo(store: VectorStore) -> None:
    """``/repo/a`` must not match ``/repo/ab`` (a different repo)."""
    store.record_composition_trace(
        _mk_trace("ab", repo="/repo/ab", tokens_returned=10, tokens_flat_equivalent=40)
    )
    assert store.aggregate_savings("/repo/a")["total_composes"] == 0


# ---------------------------------------------------------------------------
# aggregate_hook_coverage scoping
# ---------------------------------------------------------------------------


def test_coverage_scoped_to_repo(store: VectorStore) -> None:
    store.record_composition_trace(
        _mk_trace("pa", repo="/repo/a", status="composed", event_type="prompt_submit")
    )
    store.record_composition_trace(
        _mk_trace("pa2", repo="/repo/a", status="no_compose", event_type="prompt_submit")
    )
    store.record_composition_trace(
        _mk_trace("pb", repo="/repo/b", status="composed", event_type="prompt_submit")
    )

    only_a = store.aggregate_hook_coverage("/repo/a")
    assert only_a["prompts_total"] == 2
    assert only_a["prompts_composed"] == 1
    assert only_a["prompts_no_compose"] == 1

    all_repos = store.aggregate_hook_coverage()
    assert all_repos["prompts_total"] == 3


def test_coverage_prefix_matches_subdirectory(store: VectorStore) -> None:
    store.record_composition_trace(
        _mk_trace("sub", repo="/repo/a/pkg", status="composed", event_type="prompt_submit")
    )
    assert store.aggregate_hook_coverage("/repo/a")["prompts_total"] == 1


def test_coverage_by_event_respects_repo(store: VectorStore) -> None:
    store.record_composition_trace(
        _mk_trace("sa", repo="/repo/a", event_type="system_skill_applied")
    )
    store.record_composition_trace(
        _mk_trace("sb", repo="/repo/b", event_type="system_skill_applied")
    )
    scoped = store.aggregate_hook_coverage("/repo/a")
    assert scoped["system_skill_pulls"] == 1
    by_event = {(e["event_type"], e["status"]): e["count"] for e in scoped["by_event"]}  # type: ignore[index]
    assert by_event[("system_skill_applied", "compose")] == 1


# ---------------------------------------------------------------------------
# Schema migration: pre-existing DB without the repo column
# ---------------------------------------------------------------------------


def test_migration_adds_repo_column(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.duck"
    import duckdb

    conn = duckdb.connect(str(db_path))
    # A historical schema (through ``reranked``) that predates the ``repo`` column;
    # ``open_or_create`` must backfill it via the additive migration.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS composition_traces (
            trace_id VARCHAR PRIMARY KEY,
            correlation_id VARCHAR,
            request_ts BIGINT NOT NULL,
            phase VARCHAR NOT NULL,
            category VARCHAR,
            task_prompt VARCHAR NOT NULL,
            selected_fragment_ids VARCHAR[],
            source_skill_ids VARCHAR[],
            system_skill_ids VARCHAR[],
            assembly_tier VARCHAR,
            assembly_model VARCHAR,
            retrieval_latency_ms INTEGER,
            assembly_latency_ms INTEGER,
            total_latency_ms INTEGER,
            status VARCHAR NOT NULL,
            error_code VARCHAR,
            response_size_chars INTEGER,
            prompt_version VARCHAR,
            workflow_skill_ids VARCHAR[],
            event_type VARCHAR NOT NULL DEFAULT 'compose',
            pre_filter_matched VARCHAR,
            gates_met VARCHAR[],
            gates_unmet VARCHAR[],
            qwen_calls INTEGER NOT NULL DEFAULT 0,
            contract_path VARCHAR,
            contract_tags VARCHAR[],
            bm25_source VARCHAR NOT NULL DEFAULT 'rule-extracted',
            reranked BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO composition_traces (trace_id, request_ts, phase, task_prompt, status)
        VALUES ('legacy', 0, 'build', 'legacy', 'compose')
        """
    )
    conn.close()

    with open_or_create(db_path) as s:
        s.record_composition_trace(_mk_trace("new", repo="/repo/a"))
        rows = {r.trace_id: r for r in s.query_traces(limit=10)}
        # Legacy row backfills to NULL repo; new row carries its repo.
        assert rows["legacy"].repo is None
        assert rows["new"].repo == "/repo/a"
        # Legacy row is unattributed -> only visible in the all-repos view.
        assert s.aggregate_savings("/repo/a")["total_composes"] == 1
        assert s.aggregate_savings()["total_composes"] == 2


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
