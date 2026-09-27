"""v2 service integration suite — a live ``agentalloy serve`` over HTTP.

Selected with ``-m integration`` (excluded from the default run). Targets
``$AGENTALLOY_URL`` (default http://127.0.0.1:48961, the pre-PR port); the
approval-lock tests also need ``$AGENTALLOY_APPROVER_TOKEN`` (the same token the service runs with)
and are skipped without it, and ``test_compose_*`` needs a model on the
service's model port (``AGENTALLOY_INTEGRATION_MODEL=1``).

A selected run against an unreachable service FAILS rather than skips: the
gate must never go green on an empty or disconnected suite (the v1 suite was
deleted in #655 and its nightly kept collecting 0 tests). Run on the dev
server via ``scripts/pre-pr-eval.sh``, which starts an isolated service.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("AGENTALLOY_URL", "http://127.0.0.1:48961")
APPROVER = os.environ.get("AGENTALLOY_APPROVER_TOKEN", "")
WITH_MODEL = os.environ.get("AGENTALLOY_INTEGRATION_MODEL") == "1"

needs_approver = pytest.mark.skipif(not APPROVER, reason="AGENTALLOY_APPROVER_TOKEN not set")
needs_model = pytest.mark.skipif(not WITH_MODEL, reason="AGENTALLOY_INTEGRATION_MODEL != 1")


@pytest.fixture(scope="module")
def cli() -> Any:
    with httpx.Client(base_url=URL, timeout=httpx.Timeout(30.0, read=3600.0)) as c:
        try:
            c.get("/health")
        except httpx.HTTPError as exc:
            pytest.fail(f"agentalloy service not reachable at {URL}: {exc}")
        yield c


def _project() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def _tool(
    cli: httpx.Client, project: str, name: str, args: dict[str, Any], approver: bool
) -> dict[str, Any]:
    headers = {"X-AgentAlloy-Approver": APPROVER} if approver else {}
    resp = cli.post(
        "/tool", json={"name": name, "args": json.dumps(args), "project": project}, headers=headers
    )
    resp.raise_for_status()
    body = resp.json()
    assert body["ok"], body
    return json.loads(body["result"])


def _exit(cli: httpx.Client, project: str, phase: str) -> None:
    rec = _tool(
        cli,
        project,
        "artifact_record",
        {"phase": phase, "name": f"{phase}-exit", "body": f"{phase} done"},
        False,
    )
    assert rec.get("status") != "rejected", rec


def test_health(cli: httpx.Client) -> None:
    resp = cli.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_status_for_a_fresh_project_is_at_intake(cli: httpx.Client) -> None:
    body = cli.get("/status", params={"project": _project()}).json()
    assert body["phase"] == "intake"
    assert body["phase_set"] is False
    assert "api_version" in body and "capabilities" in body


def test_advance_needs_an_exit_artifact(cli: httpx.Client) -> None:
    project = _project()
    adv = _tool(cli, project, "phase_advance", {"target": "spec", "approved": True}, False)
    assert adv["status"] == "rejected"
    assert "exit artifact" in adv["reason"]


def test_ungated_advance_moves_the_project(cli: httpx.Client) -> None:
    project = _project()
    _exit(cli, project, "intake")
    adv = _tool(cli, project, "phase_advance", {"target": "spec"}, False)
    assert adv["status"] == "ok", adv
    assert cli.get("/status", params={"project": project}).json()["phase"] == "spec"


def test_phases_are_walked_one_at_a_time(cli: httpx.Client) -> None:
    project = _project()
    _exit(cli, project, "intake")
    adv = _tool(cli, project, "phase_advance", {"target": "design", "approved": True}, False)
    assert adv["status"] == "rejected"
    assert "one phase at a time" in adv["reason"]


@needs_approver
def test_gated_advance_is_locked_to_the_approver(cli: httpx.Client) -> None:
    assert cli.get("/status").json()["approval_locked"] is True
    project = _project()
    _exit(cli, project, "intake")
    assert _tool(cli, project, "phase_advance", {"target": "spec"}, False)["status"] == "ok"
    _exit(cli, project, "spec")

    denied = _tool(cli, project, "phase_advance", {"target": "design", "approved": True}, False)
    assert denied["status"] == "rejected"
    assert "locked to the orchestrator" in denied["reason"]

    allowed = _tool(cli, project, "phase_advance", {"target": "design", "approved": True}, True)
    assert allowed["status"] == "ok", allowed
    assert cli.get("/status", params={"project": project}).json()["phase"] == "design"


def test_projects_are_isolated(cli: httpx.Client) -> None:
    a, b = _project(), _project()
    _exit(cli, a, "intake")
    assert _tool(cli, a, "phase_advance", {"target": "spec"}, False)["status"] == "ok"
    assert cli.get("/status", params={"project": b}).json()["phase"] == "intake"


@needs_model
def test_compose_returns_the_handoff_contract(cli: httpx.Client) -> None:
    project = _project()
    resp = cli.post(
        "/compose",
        json={
            "prompt": "Add HMAC signature verification to our webhook receiver.",
            "project": project,
            "session_key": project,
            "new_session": True,
        },
    )
    resp.raise_for_status()
    body = resp.json()
    assert body["phase"] == "intake"
    assert body["stop_reason"] != "error", body
    assert body["context_type"] in (0, 1, 2)
    assert isinstance(body["source_skills"], list)
    assert isinstance(body["fragments"], list)
    if body["source_skills"]:
        assert body["skill"].strip()
    # The deterministic fact block stamps the stored phase, never the model's.
    if body["context"]:
        assert "Phase: intake" in body["context"]
