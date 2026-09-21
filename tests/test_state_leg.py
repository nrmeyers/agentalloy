"""State leg tests — the panel must quote the v2 service surface verbatim.

docs/bug-v2-steering-split.md (RC-2/AC-1): the panel used to advertise
retired v1 routes (?repo_root= scoping, /state/*, /contracts) that 404 on
the v2 service. These tests pin the v2 shape: scope = (service, project),
action hints = ready-to-send POST /tool bodies with JSON-string args.
"""

import json
from pathlib import Path

import pytest

from agentalloy.api.state_leg import build_state_leg, _tool_body
from agentalloy.registry import project_key


@pytest.fixture(autouse=True)
def _pinned_service_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
    monkeypatch.setenv("AGENTALLOY_SERVICE_PORT", "48950")


def _panel(phase: str, **kwargs) -> dict:
    leg = build_state_leg(phase, **kwargs)
    assert leg is not None
    return json.loads(leg)


def test_no_phase_returns_none() -> None:
    assert build_state_leg("") is None


def test_scope_uses_project_key_and_live_port(tmp_path: Path) -> None:
    state = _panel("spec", project_root=tmp_path)
    assert state["scope"] == {
        "service": "http://127.0.0.1:48950",
        "project": project_key(tmp_path),
    }


def test_scope_omitted_without_project_root() -> None:
    state = _panel("spec")
    assert "scope" not in state


def test_tool_body_double_encodes_args() -> None:
    body = json.loads(_tool_body("proj-x", "artifact_record", {"phase": "spec", "k": 2}))
    assert body["name"] == "artifact_record"
    assert body["project"] == "proj-x"
    assert isinstance(body["args"], str)  # JSON string — the /tool contract
    assert json.loads(body["args"]) == {"phase": "spec", "k": 2}


def _hint_body(hint: str) -> dict:
    """Pull the rendered POST /tool body out of an action hint."""
    marker = " with body "
    assert marker in hint
    return json.loads(hint.split(marker, 1)[1].splitlines()[0])


def test_record_artifact_hint_is_a_valid_tool_call(tmp_path: Path) -> None:
    state = _panel("spec", project_root=tmp_path)
    body = _hint_body(state["actions"]["record_artifact"])
    assert body["name"] == "artifact_record"
    assert body["project"] == project_key(tmp_path)
    args = json.loads(body["args"])
    assert args["phase"] == "spec"
    assert args["name"] == "spec-exit"


def test_approval_gated_advance_requires_approved(tmp_path: Path) -> None:
    # spec→design is approval-gated: the hint pins approved:true with the
    # explicit user-approval condition.
    state = _panel("spec", project_root=tmp_path)
    hint = state["actions"]["advance_phase"]
    body = _hint_body(hint)
    assert body["name"] == "phase_advance"
    assert json.loads(body["args"]) == {"target": "design", "approved": True}
    assert "ONLY once the user has explicitly approved" in hint


def test_ungated_advance_has_no_approved_flag(tmp_path: Path) -> None:
    # build→qa is not in APPROVAL_GATES: no approved field in the body.
    state = _panel("build", project_root=tmp_path)
    body = _hint_body(state["actions"]["advance_phase"])
    assert json.loads(body["args"]) == {"target": "qa"}
    assert "approved" not in body["args"]


def test_ship_is_terminal(tmp_path: Path) -> None:
    state = _panel("ship", project_root=tmp_path)
    assert "terminal" in state["actions"]["advance_phase"]


def test_blocked_replaces_advance(tmp_path: Path) -> None:
    state = _panel("spec", project_root=tmp_path, gates_unmet=["exit-artifact"])
    assert "blocked" in state["actions"]
    assert "advance_phase" not in state["actions"]
    assert state["gates"] == {
        "passing": [],
        "failing": ["exit-artifact"],
        "blocked": True,
    }


def test_query_sessions_reset_hints_use_v2_routes(tmp_path: Path) -> None:
    state = _panel("plan", project_root=tmp_path)
    actions = state["actions"]

    query = actions["query"]
    project = project_key(tmp_path)
    assert f"GET http://127.0.0.1:48950/status?project={project}" in query
    assert f"GET http://127.0.0.1:48950/gates?project={project}" in query
    for name in ("code_search", "symbols", "knowledge_why", "knowledge_related",
                 "artifact_body", "contract_detail", "get_skill_for"):
        assert f'"name":"{name}"' in query

    assert "GET http://127.0.0.1:48950/sessions" in actions["sessions"]
    assert "POST http://127.0.0.1:48950/sessions/<session_key>/stash" in actions["sessions"]

    reset_body = _hint_body(actions["reset"])
    assert reset_body["name"] == "phase_reset"
    assert json.loads(reset_body["args"]) == {}


def test_store_failure_is_soft() -> None:
    class ExplodingStore:
        def get_contract(self, _id: str) -> None:
            raise RuntimeError("store gone")

    state = _panel("spec", store=ExplodingStore(), contract_id="spec/x", project_root="/tmp/r")
    assert state["phase"] == "spec"
    assert "actions" in state


def test_module_source_has_no_v1_surface() -> None:
    import agentalloy.api.state_leg as state_leg

    src = Path(state_leg.__file__).read_text()
    for pattern in (
        "state/advance",
        "state/approve-phase",
        "state/artifact",
        "state/phase",
        "/contracts",
        "repo_root",
    ):
        assert pattern not in src, f"retired v1 pattern {pattern!r} in state_leg.py"
