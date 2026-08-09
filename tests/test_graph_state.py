# pyright: reportPrivateUsage=false
"""Tests for the LangGraph graph-state schema and stream key (task 02).

Covers test-plan.md T1, T2, T4 and the lease-aware key derivation of task 02.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.signals.graph import (
    ThreadKey,
    initial_phase_graph_state,
    leased_graph_kinds,
    make_thread_key,
    to_phase_graph_state,
)
from agentalloy.storage.state_store import LEASED_KINDS, PhaseState

_FAKE_SLUG = "nrmeyers__agentalloy"


@pytest.fixture(autouse=True)
def _stable_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin repo_slug to a constant so tests assert on stream_id alone."""
    import agentalloy.signals.graph as graph

    monkeypatch.setattr(graph, "repo_slug", lambda _root: _FAKE_SLUG)


# ---------------------------------------------------------------------------
# T1 — PhaseGraphState carries every durable field; session kinds absent
# ---------------------------------------------------------------------------


def test_t1_initial_state_carries_durable_fields() -> None:
    state = initial_phase_graph_state()
    assert state["phase"] == "intake"
    assert state["lane"] == "sdd-full"
    assert state["paused"] is False
    assert state["paused_since"] is None
    assert state["contract_id"] is None
    assert state["cursor"] is None
    assert state["approved"] is False
    assert state["artifact_refs"] == []


def test_t1_session_scoped_kinds_are_absent() -> None:
    state = initial_phase_graph_state(phase="build", lane="sdd-fast")
    keys = set(state)
    for kind in ("announced", "composed", "banner-turns", "pause-reminded"):
        assert kind not in keys


def test_t1_to_phase_graph_state_derives_lane_and_pause() -> None:
    row = PhaseState(phase="design", mode="paused", paused_since="2026-08-08T00:00:00Z")
    state = to_phase_graph_state(row)
    assert state["phase"] == "design"
    assert state["paused"] is True
    assert state["paused_since"] == "2026-08-08T00:00:00Z"
    # session kinds never surface
    assert not any(k in state for k in ("announced", "composed", "banner-turns", "pause-reminded"))


def test_t1_to_phase_graph_state_defaults_on_missing_row() -> None:
    state = to_phase_graph_state(None)
    assert state["phase"] == "intake"
    assert state["lane"] == "sdd-full"
    assert state["paused"] is False


# ---------------------------------------------------------------------------
# T2 — thread_id = (repo_slug, stream_id), not slug alone
# ---------------------------------------------------------------------------


def test_t2_distinct_streams_produce_distinct_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for sid in ("stream-a", "stream-b"):
        monkeypatch.setenv("AGENTALLOY_STREAM_ID", sid)
        assert make_thread_key(tmp_path) == ThreadKey(repo_slug=_FAKE_SLUG, stream_id=sid)
    a = make_thread_key(tmp_path) if False else None  # noqa: F841 (readability guard)
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "stream-a")
    key_a = make_thread_key(tmp_path)
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "stream-b")
    key_b = make_thread_key(tmp_path)
    assert key_a.stream_id != key_b.stream_id
    # same repo slug in both — the code index would share one, the phase graph must not
    assert key_a.repo_slug == key_b.repo_slug == _FAKE_SLUG
    assert key_a.as_tuple() != key_b.as_tuple()


def test_t2_as_tuple_roundtrip() -> None:
    key = ThreadKey(repo_slug=_FAKE_SLUG, stream_id="s")
    assert key.as_tuple() == (_FAKE_SLUG, "s")


# ---------------------------------------------------------------------------
# T4 — resolution order: explicit binding wins, then worktree path / env
# ---------------------------------------------------------------------------


def test_t4_explicit_binding_file_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agentalloy" / ".stream").write_text("bound-anchor\n")
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "env-should-lose")
    assert make_thread_key(tmp_path).stream_id == "bound-anchor"


def test_t4_env_fallback_when_no_binding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTALLOY_STREAM_ID", raising=False)
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "env-fallback")
    assert make_thread_key(tmp_path).stream_id == "env-fallback"


# ---------------------------------------------------------------------------
# Lease-aware key derivation (task 02)
# ---------------------------------------------------------------------------


def test_leased_kinds_mirror_store() -> None:
    assert leased_graph_kinds() == LEASED_KINDS
    assert "phase" in LEASED_KINDS
    assert "approved" in LEASED_KINDS


def test_lease_scoped_per_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Two streams in the *same* repo must not share a checkpoint key, so their
    # leases (on the same kind) are isolated per stream.
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "s-1")
    k1 = make_thread_key(tmp_path)
    monkeypatch.setenv("AGENTALLOY_STREAM_ID", "s-2")
    k2 = make_thread_key(tmp_path)
    assert k1 != k2
