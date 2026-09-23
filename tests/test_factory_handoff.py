"""Software-factory handoff (TheForge): contract extras + the approver lock.

- contract_add accepts route / scope_avoids / success_criteria / body /
  work_item / source_ref; slim upserts never wipe them; legacy contracts
  serialize byte-identically.
- With AGENTALLOY_APPROVER_TOKEN set, recording an approval or resetting the
  lifecycle over /tool requires X-AgentAlloy-Approver — a headless coding
  agent (which never holds the token) cannot approve its own plan.
"""

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy import server
from agentalloy.server import app
from agentalloy.state_store import StateStore

PROJECT = "demo-deadbeef"
TOKEN = "t" * 40


@pytest.fixture
def state(tmp_path: Path) -> Generator[StateStore, None, None]:
    store = StateStore(str(tmp_path / "state.duck"))
    server.state_store = store
    yield store
    server.state_store = None
    store.close()


def _tool(client: TestClient, name: str, args: dict, headers: dict | None = None) -> dict:
    r = client.post(
        "/tool",
        json={"name": name, "args": json.dumps(args), "project": PROJECT},
        headers=headers or {},
    ).json()
    assert r["ok"], r
    return json.loads(r["result"])


def test_contract_add_source_ref_round_trips(state: StateStore) -> None:
    client = TestClient(app)
    source_ref = {"system": "forge", "taskId": "forge:abc", "obsFocusOutcomeId": "FO1"}
    _tool(
        client,
        "contract_add",
        {
            "slug": "reduce-onboarding-drop",
            "domain_tags": ["react"],
            "touches": "web/src/onboarding/**",
            "route": "spec",
            "scope_avoids": ["src/billing/**"],
            "success_criteria": ["Activation rate >= 40% (EM confirming)"],
            "body": "What the user actually wants: ...",
            "work_item": "forge:abc",
            "source_ref": source_ref,
        },
    )
    detail = _tool(client, "contract_detail", {"slug": "reduce-onboarding-drop"})
    assert detail["source_ref"] == source_ref
    assert detail["success_criteria"] == ["Activation rate >= 40% (EM confirming)"]
    assert detail["scope_avoids"] == ["src/billing/**"]
    assert detail["route"] == "spec"

    # A later slim upsert (the steering loop's shape) keeps the handoff fields.
    _tool(
        client,
        "contract_add",
        {"slug": "reduce-onboarding-drop", "domain_tags": ["ts"], "touches": "x"},
    )
    detail = _tool(client, "contract_detail", {"slug": "reduce-onboarding-drop"})
    assert detail["domain_tags"] == ["ts"]
    assert detail["source_ref"] == source_ref


def test_legacy_contract_shape_unchanged(state: StateStore) -> None:
    client = TestClient(app)
    _tool(client, "contract_add", {"slug": "slim", "domain_tags": ["py"], "touches": "src/"})
    assert _tool(client, "contract_detail", {"slug": "slim"}) == {
        "slug": "slim",
        "domain_tags": ["py"],
        "touches": "src/",
    }


def _to_spec_with_artifact(client: TestClient, headers: dict) -> None:
    # intake → spec is not approval-gated; spec → design is.
    _tool(
        client,
        "artifact_record",
        {"phase": "intake", "name": "intake-exit", "body": "route: spec, contract written"},
    )
    assert _tool(client, "phase_advance", {"target": "spec"}, headers)["status"] == "ok"
    _tool(
        client,
        "artifact_record",
        {
            "phase": "spec",
            "name": "spec-exit",
            "body": "## Acceptance Criteria\n- a\n## Out of Scope\n- b",
        },
    )


def test_approval_locked_without_token(state: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_APPROVER_TOKEN", TOKEN)
    client = TestClient(app)
    _to_spec_with_artifact(client, {})
    r = _tool(client, "phase_advance", {"target": "design", "approved": True})
    assert r["status"] == "rejected"
    assert "orchestrator" in r["reason"]
    wrong = _tool(
        client,
        "phase_advance",
        {"target": "design", "approved": True},
        {"X-AgentAlloy-Approver": "nope"},
    )
    assert wrong["status"] == "rejected"
    assert _tool(client, "phase_reset", {})["status"] == "rejected"
    assert state.scoped(PROJECT).get_current_phase() == "spec"


def test_approval_with_token(state: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_APPROVER_TOKEN", TOKEN)
    client = TestClient(app)
    hdr = {"X-AgentAlloy-Approver": TOKEN}
    _to_spec_with_artifact(client, hdr)
    r = _tool(client, "phase_advance", {"target": "design", "approved": True}, hdr)
    assert r["status"] == "ok"
    assert state.scoped(PROJECT).get_current_phase() == "design"
    assert _tool(client, "phase_reset", {}, hdr)["status"] == "reset"


def test_lock_off_when_token_unset(state: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTALLOY_APPROVER_TOKEN", raising=False)
    client = TestClient(app)
    _to_spec_with_artifact(client, {})
    assert _tool(client, "phase_advance", {"target": "design", "approved": True})["status"] == "ok"


def test_status_reports_approval_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.delenv("AGENTALLOY_APPROVER_TOKEN", raising=False)
    assert client.get("/status").json()["approval_locked"] is False
    monkeypatch.setenv("AGENTALLOY_APPROVER_TOKEN", TOKEN)
    assert client.get("/status").json()["approval_locked"] is True
