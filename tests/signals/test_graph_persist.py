"""Tests for graph checkpoint persistence on the store (task 05).

Covers test-plan.md T13/T14 (resume keeps prior state; pause survives) and the
stream-isolation check (distinct streams get distinct checkpoint rows).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.signals.graph import (
    ThreadKey,
    load_graph_state,
    save_graph_state,
)
from agentalloy.storage.state_store import DuckDBStateStore


@pytest.fixture
def store(tmp_path: Path):
    db = tmp_path / "state.duck"
    with DuckDBStateStore(db).open() as s:
        s.migrate()
        yield s


def _key() -> ThreadKey:
    return ThreadKey(repo_slug="nrmeyers__agentalloy", stream_id="s-a")


def test_t13_save_then_load_roundtrip_resumes_prior_state(store: DuckDBStateStore) -> None:
    state = {
        "phase": "design",
        "lane": "sdd-full",
        "paused": False,
        "paused_since": None,
        "contract_id": "02-cache",
        "cursor": "02-cache",
        "approved": False,
        "artifact_refs": [],
    }
    save_graph_state(store, _key(), state)  # type: ignore[arg-type]
    loaded = load_graph_state(store, _key())
    assert loaded is not None
    assert loaded["phase"] == "design"
    assert loaded["cursor"] == "02-cache"
    assert loaded["lane"] == "sdd-full"


def test_t14_paused_flag_survives_save_load(store: DuckDBStateStore) -> None:
    save_graph_state(
        store,
        _key(),
        {
            "phase": "build",
            "lane": "sdd-full",
            "paused": True,
            "paused_since": 1785782958,
            "contract_id": None,
            "cursor": None,
            "approved": False,
            "artifact_refs": [],
        },
    )  # type: ignore[arg-type]
    loaded = load_graph_state(store, _key())
    assert loaded is not None
    assert loaded["paused"] is True
    assert loaded["paused_since"] == 1785782958
    assert loaded["phase"] == "build"


def test_fresh_stream_loads_none(store: DuckDBStateStore) -> None:
    assert load_graph_state(store, _key()) is None


def test_distinct_streams_do_not_share_checkpoint_row(store: DuckDBStateStore) -> None:
    a = ThreadKey("nrmeyers__agentalloy", "s-a")
    b = ThreadKey("nrmeyers__agentalloy", "s-b")
    save_graph_state(
        store,
        a,
        {
            "phase": "design",
            "lane": "sdd-full",
            "paused": False,
            "paused_since": None,
            "contract_id": None,
            "cursor": None,
            "approved": False,
            "artifact_refs": [],
        },
    )  # type: ignore[arg-type]
    assert load_graph_state(store, a) is not None
    assert load_graph_state(store, b) is None  # isolation (#548)
