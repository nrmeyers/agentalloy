"""Contract provenance on proxy telemetry rows (build contract proxy-contract-provenance).

AC3.1: Tier-2 contract-scoped composes populate contract_path/contract_tags on
the consolidated proxy_request row. AC3.2: free-text rows keep them null/empty.
AC3.3: covered end-to-end through the real apply path (TestClient + mock
upstream + real telemetry store), not only at the merge unit."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from agentalloy.api.compose_models import (
    ComposedResult,
    ComposeRequest,
    LatencyBreakdown,
    compose_request_from_contract,
)
from agentalloy.api.proxy_apply import (
    _merge_compose_telemetry,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.contracts import parse_contract
from agentalloy.storage.telemetry_store import open_telemetry_store
from tests.test_proxy_passthrough_native import (
    _SIGNAL,
    _anthropic_body,
    _composed_signal,
    _make_app_with_store,
    _orchestrator,
    _token,
)

_CONTRACT_MD = """---
phase: build
task_slug: provenance-probe
domain_tags: [redis, streams]
---

Implement the consumer-group worker loop.
"""


def _seed_contract(tmp_path: Path) -> Path:
    path = tmp_path / ".agentalloy" / "contracts" / "build" / "provenance-probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONTRACT_MD)
    return path


def _result(task: str = "t") -> ComposedResult:
    return ComposedResult(
        task=task,
        phase="build",
        output="X",
        domain_fragments=["f1"],
        source_skills=["s1"],
        system_fragments=[],
        system_skills_applied=False,
        assembly_tier=1,
        latency_ms=LatencyBreakdown(retrieval_ms=1, assembly_ms=0, total_ms=1),
    )


# -------- merge unit (AC3.1 / AC3.2 at the fold) --------


def test_merge_populates_contract_fields_from_tier2_request(tmp_path: Path) -> None:
    contract = parse_contract(_seed_contract(tmp_path))
    req = compose_request_from_contract(contract, legs="domain", k=2)
    tel = _merge_compose_telemetry(SignalResult(should_compose=True), None, _result(), req)
    assert tel.contract_path == str(contract.path)
    assert tel.contract_tags == ["redis", "streams"]


def test_merge_leaves_contract_fields_null_without_request() -> None:
    tel = _merge_compose_telemetry(SignalResult(should_compose=True), None, _result(), None)
    assert tel.contract_path is None
    assert tel.contract_tags == []


def test_merge_ignores_request_when_tier2_compose_failed(tmp_path: Path) -> None:
    # The request was built but the compose leg threw (tier2 result is None):
    # no skills were injected, so the row must not claim contract provenance.
    contract = parse_contract(_seed_contract(tmp_path))
    req = compose_request_from_contract(contract, legs="domain", k=2)
    tel = _merge_compose_telemetry(SignalResult(should_compose=True), None, None, req)
    assert tel.contract_path is None
    assert tel.contract_tags == []


def test_merge_free_text_request_has_no_contract_fields() -> None:
    # Free-flow requests are plain ComposeRequests — even if passed, they carry
    # no contract fields, so the row stays null by construction.
    req = ComposeRequest(task="t", phase="build", legs="domain")
    tel = _merge_compose_telemetry(SignalResult(should_compose=True), None, _result(), req)
    assert tel.contract_path is None
    assert tel.contract_tags == []


# -------- hermetic e2e through the real apply path (AC3.3) --------


def _cursor_signal(tmp_path: Path, contract_path: Path) -> SignalResult:
    return SignalResult(
        should_compose=True,
        phase="build",
        announce=True,
        workflow_prose="OPERATE LIKE THIS",
        workflow_skill_id="wf-build",
        repo=str(tmp_path),
        session_key="sess-1",
        session_source="header",
        task="t",
        announce_cursor=True,
        current_contract=str(contract_path),
    )


def test_e2e_contract_scoped_row_carries_contract_provenance(tmp_path: Path) -> None:
    contract_path = _seed_contract(tmp_path)
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"))
        signal = _cursor_signal(tmp_path, contract_path)
        with patch(_SIGNAL, return_value=signal), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = store.query_traces(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "proxy_composed"
        assert row.contract_path == str(contract_path)
        assert list(row.contract_tags) == ["redis", "streams"]


def test_e2e_free_text_row_has_null_contract_provenance(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    with closing(open_telemetry_store(tmp_path / "tele.duck")) as store:
        app = _make_app_with_store(captured, store, orchestrator=_orchestrator("WF"))
        # Tier-1 announce only — the free-text shape (no cursor, no contract).
        with patch(_SIGNAL, return_value=_composed_signal(tmp_path)), TestClient(app) as client:
            resp = client.post(f"/proj/{_token(tmp_path)}/v1/messages", json=_anthropic_body())
        assert resp.status_code == 200
        rows = store.query_traces(limit=10)
        assert len(rows) == 1
        assert rows[0].status == "proxy_composed"
        assert rows[0].contract_path is None
        assert list(rows[0].contract_tags) == []
