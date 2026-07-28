"""Task 04 `state-api-unwrap` — nothing outside the store handles the phase blob.

``phase`` is stored as JSON carrying mode, actor, and timestamps beside the
name.  Three read seams used to hand that row out verbatim — ``GET /state``,
``GET /state/resume``, and the generic ``GET /state/{kind}`` — and the client's
``get_state`` handed back the whole response envelope on top of it.  A caller
asking for the phase got JSON where it expected ``"build"``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.api.state_client import StateClient
from agentalloy.api.state_router import (
    _repo_key_for,
    default_repo_root,
    get_state_store,
)
from agentalloy.api.state_router import (
    router as state_router,
)
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store


@pytest.fixture()
def wired(tmp_path: Path):
    store = open_state_store(tmp_path / "state.duck", repo=_repo_key_for(str(default_repo_root())))
    app = FastAPI()
    app.include_router(state_router)
    app.dependency_overrides[get_state_store] = lambda: store
    try:
        yield store, TestClient(app)
    finally:
        store.close()


def _seed(store: DuckDBStateStore, repo_root: Path) -> DuckDBStateStore:
    """Give *repo_root* a phase blob with metadata, and return its store view."""
    view = store.for_repo(_repo_key_for(str(repo_root)))
    view.write_phase("build", actor="proxy", mode="workflow")
    return view


class TestPhaseRoute:
    def test_returns_the_bare_name(self, wired, tmp_path: Path) -> None:
        store, client = wired
        repo = tmp_path / "repo"
        _seed(store, repo)

        resp = client.get("/state/phase", params={"repo_root": str(repo)})

        assert resp.status_code == 200
        assert resp.json() == {"kind": "phase", "value": "build"}

    def test_unset_phase_is_a_null_value_not_an_error(self, wired, tmp_path: Path) -> None:
        _store, client = wired
        resp = client.get("/state/phase", params={"repo_root": str(tmp_path / "fresh")})
        assert resp.status_code == 200
        assert resp.json() == {"kind": "phase", "value": None}

    def test_the_route_wins_over_the_generic_kind_route(self, wired, tmp_path: Path) -> None:
        """Declaration order is load-bearing — pin it so a reorder is caught."""
        store, client = wired
        repo = tmp_path / "repo"
        _seed(store, repo)

        value = client.get("/state/phase", params={"repo_root": str(repo)}).json()["value"]

        assert value == "build"
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(value)  # a blob would parse; a bare name must not


class TestReadAll:
    def test_phase_is_unwrapped_in_the_map(self, wired, tmp_path: Path) -> None:
        store, client = wired
        repo = tmp_path / "repo"
        view = _seed(store, repo)
        view.write("cursor", "active/build/04-unwrap.md")

        state = client.get("/state", params={"repo_root": str(repo)}).json()["state"]

        assert state["phase"] == "build"
        assert state["cursor"] == "active/build/04-unwrap.md"

    def test_other_kinds_are_still_verbatim(self, wired, tmp_path: Path) -> None:
        """Only phase is a blob; unwrapping must not touch anything else."""
        store, client = wired
        repo = tmp_path / "repo"
        store.for_repo(_repo_key_for(str(repo))).write("cursor", '{"looks": "like json"}')

        state = client.get("/state", params={"repo_root": str(repo)}).json()["state"]

        assert state["cursor"] == '{"looks": "like json"}'


class TestResume:
    def test_phase_is_unwrapped(self, wired, tmp_path: Path) -> None:
        store, client = wired
        repo = tmp_path / "repo"
        _seed(store, repo)

        body: dict[str, Any] = client.get("/state/resume", params={"repo_root": str(repo)}).json()

        assert body["phase"] == "build"

    def test_no_phase_resumes_with_null(self, wired, tmp_path: Path) -> None:
        _store, client = wired
        body = client.get("/state/resume", params={"repo_root": str(tmp_path / "fresh")}).json()
        assert body["phase"] is None


class TestGenericKindRoute:
    def test_unknown_kind_is_still_a_404(self, wired, tmp_path: Path) -> None:
        _store, client = wired
        resp = client.get("/state/nonsense", params={"repo_root": str(tmp_path)})
        assert resp.status_code == 404

    def test_a_repo_scoped_kind_reads_verbatim(self, wired, tmp_path: Path) -> None:
        store, client = wired
        repo = tmp_path / "repo"
        store.for_repo(_repo_key_for(str(repo))).write("cursor", "active/build/04.md")

        resp = client.get("/state/cursor", params={"repo_root": str(repo)})

        assert resp.json() == {"kind": "cursor", "value": "active/build/04.md"}


class TestClientUnwrap:
    """``get_state`` returns the value, not the envelope around it."""

    def _client(self, wired, monkeypatch: pytest.MonkeyPatch, repo: Path) -> StateClient:
        _store, http = wired
        client = StateClient(base_url="http://state.test", repo_root=str(repo))

        def _urlopen(url: Any, timeout: float = 0.0):  # noqa: ARG001
            target = url if isinstance(url, str) else url.full_url
            return _Resp(http.get(target.replace("http://state.test", "")).content)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        return client

    def test_phase_comes_back_bare(
        self, wired, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, _http = wired
        repo = tmp_path / "repo"
        _seed(store, repo)

        assert self._client(wired, monkeypatch, repo).get_state("phase") == "build"

    def test_an_unset_kind_is_none_not_the_string_none(
        self, wired, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``{"value": null}`` must not become ``"None"`` — callers test truthiness."""
        repo = tmp_path / "fresh"
        assert self._client(wired, monkeypatch, repo).get_state("cursor") is None

    def test_a_non_envelope_body_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An older service answering a bare token is still usable, not discarded."""
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(b"build\n"))
        assert StateClient(base_url="http://state.test").get_state("phase") == "build\n"


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body
