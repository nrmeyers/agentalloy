"""Content-type tolerance for the JSON-body state endpoints.

Agents hand-roll these HTTP calls: ``curl --data`` and Python
``urllib``/``requests`` (without ``json=``) tag a JSON body as
``application/x-www-form-urlencoded``. The service must accept the valid JSON
anyway, and a genuinely unparseable body must return an actionable 422 that
names the transport fix instead of a bare validation dump. Field-level
validation errors keep FastAPI's default response shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.json_body import install_json_body_tolerances
from agentalloy.api.state_router import (
    _repo_key_for,
    _stream_key_for,
    default_repo_root,
    get_state_store,
)
from agentalloy.api.state_router import router as state_router
from agentalloy.storage.state_store import open_state_store

_ARTIFACT_PATH = "/state/artifact"


@pytest.fixture
def tolerant_client(tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient with the state router + json-body tolerances installed."""
    root_s = str(default_repo_root())
    store = open_state_store(
        tmp_path / "state.duck", repo=_repo_key_for(root_s), stream_id=_stream_key_for(root_s)
    )
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_state_store] = lambda: store
    install_json_body_tolerances(app)
    try:
        yield TestClient(app)
    finally:
        store.close()


def _artifact_body() -> bytes:
    return json.dumps(
        {
            "phase": "plan",
            "slug": "ct-probe",
            "name": "probe.artifact",
            "content": "# Probe\n\ncontent-type tolerance",
        }
    ).encode()


def _artifact_url() -> str:
    return f"{_ARTIFACT_PATH}?repo_root={default_repo_root()}"


class TestJsonBodyTolerance:
    """Valid JSON bodies are accepted regardless of the client's content-type tag."""

    def test_json_content_type_still_works(self, tolerant_client: TestClient) -> None:
        resp = tolerant_client.put(
            _artifact_url(), content=_artifact_body(), headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 200

    def test_form_urlencoded_tag_on_json_body_is_accepted(
        self, tolerant_client: TestClient
    ) -> None:
        # curl --data's default tag — the exact failure mode from the field.
        resp = tolerant_client.put(
            _artifact_url(),
            content=_artifact_body(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "probe.artifact"

    def test_missing_content_type_is_accepted(self, tolerant_client: TestClient) -> None:
        resp = tolerant_client.put(_artifact_url(), content=_artifact_body())
        assert resp.status_code == 200


class TestActionableBody422:
    """Unparseable bodies return the transport fix, not a validation dump."""

    def test_genuine_form_fields_get_actionable_message(self, tolerant_client: TestClient) -> None:
        resp = tolerant_client.put(
            _artifact_url(),
            content=b"phase=plan&slug=ct-probe&name=probe.artifact",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "application/json" in detail
        assert "artifact put" in detail

    def test_garbage_body_get_actionable_message(self, tolerant_client: TestClient) -> None:
        resp = tolerant_client.put(_artifact_url(), content=b"not json at all")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "Content-Type: application/json" in detail

    def test_field_validation_422_keeps_default_shape(self, tolerant_client: TestClient) -> None:
        # Missing required fields: loc is ("body", <field>), type "missing" —
        # not a parse failure, so the default list-of-errors shape is kept.
        resp = tolerant_client.put(
            _artifact_url(), content=b"{}", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert any(err.get("type") == "missing" for err in detail)
