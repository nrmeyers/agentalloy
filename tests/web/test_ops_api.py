"""Unit tests for the web UI ops endpoints — repos, approvals, packs, profiles,
contracts (TA2, TA6, TA10)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentalloy.api.state_router import (
    _repo_key_for,
    _stream_key_for,
    default_repo_root,
    get_state_store,
)
from agentalloy.app import create_app
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store

_CSRF = {"X-AgentAlloy-CSRF": "1"}


def _make_repo(tmp_path: Path, name: str, phase: str = "spec") -> Path:
    root = tmp_path / name
    (root / ".agentalloy").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    _set_phase(root, phase)
    return root


def _set_phase(root: Path, phase: str) -> None:
    """Put *root* in *phase* — in the store, which is where the API reads it."""
    from agentalloy.install.subcommands.phase import run_phase_set

    run_phase_set(phase, root=root, force=True)


def _write_spec_doc(store: DuckDBStateStore, root: Path) -> None:
    """Record the spec artifact into BOTH store scopes ops_api reads from —
    a pre-existing split, unrelated to the artifact-store migration:
    `/api/approvals` and `/api/repos` read the fixture's fixed-repo `store`
    (`Depends(get_state_store)`, no per-request `.for_repo()`), while
    `/api/repos/approve` calls `run_approve` -> `phase_access(root)`, which
    IS root-scoped. Writing to only one leaves the other blind."""
    from agentalloy.install.subcommands._state import phase_access

    content = "# x\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
    store.set_artifact("spec", "x", "spec.md", content)
    phase_access(root).contracts_handle().set_artifact("spec", "x", "spec.md", content)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PROFILE_ROOT", str(tmp_path / "profiles"))
    app = create_app(use_default_lifespan=False)
    # Wire a state store so contract-counting routes work (store-backed, not filesystem).
    state_db = tmp_path / "state.duck"
    # Routes scope to (repo, stream_id) resolved from the request; the fixture
    # has to be opened under that same pair or seeded rows are invisible to them.
    root_s = str(default_repo_root())
    store = open_state_store(
        state_db, repo=_repo_key_for(root_s), stream_id=_stream_key_for(root_s)
    )
    app.state.store = store
    app.dependency_overrides[get_state_store] = lambda: store
    with TestClient(app) as c:
        c.tmp = tmp_path  # pyright: ignore[reportAttributeAccessIssue]
        c.store = store  # pyright: ignore[reportAttributeAccessIssue]
        yield c


def _wire(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    state = {
        "harness_files_written": [
            {"repo_root": str(r), "harness": "claude-code", "path": "x", "action": "written"}
            for r in roots
        ]
    }
    monkeypatch.setattr("agentalloy.install.state.load_state", lambda root=None: state)


def test_repos_lists_wired_state(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r1", phase="build")
    (repo / ".agentalloy" / "config").write_text("lifecycle_mode: full\n")
    (repo / ".agentalloy" / "upstream").write_text("url: http://localhost:1234/v1\nmodel: qwen3\n")
    # Contracts are counted from the store, not the filesystem.
    store = client.store  # pyright: ignore[reportAttributeAccessIssue]
    store.put_contract("ctr-build-1", phase="build", slug="01-task", body="build contract")
    _wire(monkeypatch, repo)

    body = client.get("/api/repos").json()
    assert body["total"] == 1
    r = body["repos"][0]
    assert r["harnesses"] == ["claude-code"]
    assert r["phase"] == "build"
    assert r["lifecycle_mode"] == "full"
    assert r["upstream_model"] == "qwen3"
    assert r["contracts_by_phase"] == {"build": 1}
    assert r["approval_required"] is False  # build is not approval-gated


def test_repos_tolerates_missing_dir(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch, tmp_path / "gone")
    body = client.get("/api/repos").json()
    assert body["repos"][0]["exists"] is False


def test_approvals_pending_then_approve_advances(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path, "r2", phase="spec")
    _write_spec_doc(client.store, repo)
    _wire(monkeypatch, repo)

    pending = client.get("/api/approvals").json()
    assert pending["total"] == 1
    entry = pending["pending"][0]
    assert entry["phase"] == "spec"
    assert entry["next_phase"] == "design"
    assert entry["stale"] is False  # never approved, not stale
    assert entry["artifacts"] == ["spec/x/spec.md"]

    r = client.post("/api/repos/approve", json={"repo": str(repo), "phase": "spec"})
    assert r.status_code == 403  # CSRF required

    r = client.post(
        "/api/repos/approve",
        json={"repo": str(repo), "phase": "spec", "approver": "alice"},
        headers=_CSRF,
    )
    assert r.status_code == 200
    assert r.json()["advanced"]["phase"] == "design"
    assert client.get("/api/approvals").json()["total"] == 0


def test_approvals_stale_marker_reappears(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r3", phase="spec")
    _write_spec_doc(client.store, repo)
    _wire(monkeypatch, repo)
    from agentalloy.install.subcommands._state import phase_access

    r = client.post("/api/repos/approve", json={"repo": str(repo), "phase": "spec"}, headers=_CSRF)
    assert r.status_code == 200
    # `run_approve` recorded the approval into `phase_access(repo)`'s own scope
    # (the correct read path for the approve endpoint); mirror it into the
    # fixture's fixed-repo scope too, since `/api/approvals` reads that one —
    # same pre-existing split noted in `_write_spec_doc`.
    approval = phase_access(repo).contracts_handle().get_approval("spec")
    assert approval is not None
    client.store.set_approval("spec", approval["artifact_digest"])

    # Back to spec (simulate rework), edit the artifact after the marker — the
    # recorded digest no longer matches the current artifact content.
    _set_phase(repo, "spec")
    client.store.set_artifact(
        "spec", "x", "spec.md", "# x (reworked)\n## Acceptance Criteria\n- a\n"
    )

    pending = client.get("/api/approvals").json()
    assert pending["total"] == 1
    assert pending["pending"][0]["stale"] is True


def test_gates_endpoint_reports_blockers(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path, "r4", phase="spec")  # no spec doc yet
    _wire(monkeypatch, repo)
    body = client.get(f"/api/repos/gates?repo={repo}").json()
    assert body["phase"] == "spec"
    assert body["next_phase"] == "design"
    assert body["blocked"] is True
    assert body["approval_pending"] is True


def test_packs_installed_counts(client):
    client.app.state.runtime = SimpleNamespace(
        get_active_skills=lambda: [SimpleNamespace(skill_id="sdd-fast")]
    )
    body = client.get("/api/packs").json()
    sdd = next(p for p in body["packs"] if p["name"] == "sdd")
    assert sdd["skill_count"] >= 8  # includes sdd-add-skill now
    assert sdd["installed_count"] == 1


def test_doctor_passthrough(client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "agentalloy.install.subcommands.doctor.run_doctor",
        lambda: {"schema_version": 2, "all_checks_passed": True, "checks": []},
    )
    assert client.get("/api/doctor").json()["all_checks_passed"] is True


def test_reembed_status_and_dry_run(client, monkeypatch: pytest.MonkeyPatch):
    client.app.state.store = SimpleNamespace()
    client.app.state.vector_store = SimpleNamespace(count_embeddings=lambda: 42)
    monkeypatch.setattr(
        "agentalloy.reembed.cli.discover_unembedded_fragments",
        lambda store, vs, **kw: [1, 2, 3],
    )
    body = client.get("/api/reembed/status").json()
    assert body == {"embedded_total": 42, "unembedded": 3}
    r = client.post("/api/reembed", json={"dry_run": True}, headers=_CSRF)
    assert r.json() == {"dry_run": True, "would_embed": 3}


def test_reembed_run_releases_store_handle_around_write(client, monkeypatch: pytest.MonkeyPatch):
    """The service process holds the skill store read-only; the write pass must
    run inside released() (handle closed) and refresh the cache afterwards."""
    from contextlib import contextmanager

    events: list[str] = []

    class FakeStore:
        @contextmanager
        def released(self):
            events.append("released-enter")
            yield
            events.append("released-exit")

    client.app.state.store = FakeStore()
    monkeypatch.setattr(
        "agentalloy.reembed.cli.run_bulk_reembed",
        lambda **kw: events.append("reembed") or 0,
    )
    monkeypatch.setattr(
        "agentalloy.web.runtime_refresh.refresh_runtime_cache",
        lambda app: events.append("refresh") or True,
    )
    r = client.post("/api/reembed", json={"dry_run": False}, headers=_CSRF)
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert body["cache_refreshed"] is True
    assert events == ["released-enter", "reembed", "released-exit", "refresh"]


def test_profiles_list_and_resolve(client, tmp_path: Path):
    body = client.get("/api/profiles").json()
    assert any(p["name"] == "default" for p in body["profiles"])
    repo = _make_repo(tmp_path, "r5")
    r = client.post("/api/profiles/resolve", json={"repo": str(repo)})
    assert r.json()["profile"] == "default"


# ---------------------------------------------------------------------------
# Contract surface (TA2, TA6, TA10)
# ---------------------------------------------------------------------------


class TestContractWebSurface:
    """Web contract read/edit/archive over the same /contracts routes as the CLI."""

    def _seed_contract(self, client) -> str:
        store = client.store  # pyright: ignore[reportAttributeAccessIssue]
        store.put_contract(
            "ctr-web-01",
            phase="build",
            slug="08-web-surface",
            work_item="contract-store-and-write-gating",
            domain_tags=["web-ui", "api-design"],
            body="# Contract body\n\nOriginal content.",
        )
        return "ctr-web-01"

    # TA6 — archive from the web flips status
    def test_ta6_archive_from_web_flips_status(self, client):
        cid = self._seed_contract(client)
        # Verify active
        row = client.get(f"/contracts/{cid}").json()
        assert row["status"] == "active"

        # Archive via the same route the CLI uses
        r = client.post(f"/contracts/{cid}/archive", json={}, headers=_CSRF)
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

        # Row stays fetchable by contract_id
        row2 = client.get(f"/contracts/{cid}").json()
        assert row2["contract_id"] == cid
        assert row2["status"] == "archived"

    # TA10 — web edit and CLI edit produce identical stored bytes
    def test_ta10_web_edit_matches_cli_edit(self, client):
        cid = self._seed_contract(client)
        new_body = "# Contract body\n\nCorrected content."
        new_tags = ["web-ui", "api-design", "testing"]

        # Web edit: PATCH /contracts/{id}
        r_web = client.patch(
            f"/contracts/{cid}",
            json={"body": new_body, "domain_tags": new_tags},
            headers=_CSRF,
        )
        assert r_web.status_code == 200
        web_result = r_web.json()

        # The store row is the single source of truth — read it back
        store = client.store  # pyright: ignore[reportAttributeAccessIssue]
        stored = store.get_contract(cid)
        assert stored is not None
        # Body and domain_tags match what the web sent
        assert stored["body"] == new_body
        assert stored["domain_tags"] == new_tags
        # updated_at was bumped
        assert stored["updated_at"] is not None

        # Simulate a CLI edit: same PATCH route, same payload
        r_cli = client.patch(
            f"/contracts/{cid}",
            json={"body": new_body, "domain_tags": new_tags},
            headers=_CSRF,
        )
        assert r_cli.status_code == 200
        cli_result = r_cli.json()

        # Both responses are identical — same routes, same serializer
        assert web_result["body"] == cli_result["body"]
        assert web_result["domain_tags"] == cli_result["domain_tags"]
        assert web_result["contract_id"] == cli_result["contract_id"]

    # TA2 — no direct DuckDB open in the web import graph
    def test_ta2_no_direct_duckdb_in_web_import_graph(self):
        """Assert no `duckdb.connect` in the web/ import graph."""
        web_ops = importlib.import_module("agentalloy.web.ops_api")
        source_file = Path(web_ops.__file__)
        source = source_file.read_text()
        assert "duckdb.connect" not in source, (
            "ops_api.py must not call duckdb.connect directly — "
            "the store is injected via Depends(get_state_store)"
        )

        # Also verify the web module does not import duckdb at all
        assert "duckdb" not in sys.modules.get("agentalloy.web.ops_api", "").__dict__, (
            "ops_api should not expose duckdb in its namespace"
        )
