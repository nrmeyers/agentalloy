"""End-to-end: the proxy's ``evaluate_signal`` auto-advance walks the full
``sdd-full`` lane intake → spec → design → plan → build → qa → ship.

Where ``tests/lifecycle/test_sdd_full_workflow.py`` drives the CLI decision
points (``run_phase_set`` / ``run_approve``), this drives the *proxy* path —
real ``evaluate_signal`` turns with the upstream stubbed out — so it exercises
the pieces only the proxy touches:

* ``_write_phase_atomic`` (leased store write + phase-start-ref stamp),
* the proxy's cursor seeding on phase entry,
* the ``check_transition_trigger`` → ``_route_step`` gate pipeline, and
* ``_auto_create_next_contract`` (next-phase contract carry-forward).

Contract/artifact markers are injected through the same extractors the proxy
runs on an upstream response (``contract_extractor`` / ``artifact_extractor``),
so no mock ever stands in for a gate or a store write. The transition trigger
is forced deterministically with ``AGENTALLOY_FORCE_CHECK=1`` (the documented
manual override), so the walk needs no embedding/reranker model.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agentalloy.api import artifact_extractor, contract_extractor
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.api.state_router import scoped_state_store
from agentalloy.signals.gates import (  # pyright: ignore[reportPrivateUsage]
    _APPROVAL_STORE_NAME_GLOB,
)
from agentalloy.signals.predicates import (  # pyright: ignore[reportPrivateUsage]
    _artifact_digest,
    _resolve_workitem_slug_for,
)
from agentalloy.storage.state_store import process_store

# One work item spans the lifecycle; a single build task fans out of plan.
WORK_SLUG = "feat-cache-invalidation"
BUILD_SLUG = "01-implement-cache"
SESSION = "sess-sdd-full-e2e"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A wired git repo: initial commit + ``.agentalloy`` so the proxy treats
    it as managed, with the transition trigger forced for every turn."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# fixture\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    (tmp_path / ".agentalloy").mkdir(exist_ok=True)
    monkeypatch.setenv("AGENTALLOY_FORCE_CHECK", "1")
    return tmp_path


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "e2e",
        "GIT_AUTHOR_EMAIL": "e2e@example.com",
        "GIT_COMMITTER_NAME": "e2e",
        "GIT_COMMITTER_EMAIL": "e2e@example.com",
    }
    out = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    )
    return out.stdout.strip()


def _store(project: Path):
    return scoped_state_store(process_store(), project)


def _phase(project: Path) -> str | None:
    ps = _store(project).read_phase()
    return ps.phase if ps else None


def _cursor(project: Path) -> str | None:
    return _store(project).read("cursor")


def _turn(project: Path, prompt: str) -> None:
    """One agent turn through the real ``evaluate_signal`` gate pipeline."""
    request = ProxyRequest(
        model="test-model",
        messages=[ProxyMessage(role="user", content=prompt)],
        tools=[{"name": "Read", "description": "read a file", "input_schema": {}}],
    )
    asyncio.run(evaluate_signal(request, project, session_id=SESSION))


def _emit_contract(project: Path, attrs: str, body: str) -> None:
    text = f"<!-- agentalloy:contract {attrs} -->\n{body}\n<!-- /agentalloy:contract -->"
    contract_extractor.extract_and_store(text, project_root=project)


def _emit_artifact(project: Path, phase: str, slug: str, name: str, body: str) -> None:
    text = f"<!-- agentalloy:artifact name={name} -->\n{body}\n<!-- /agentalloy:artifact -->"
    artifact_extractor.extract_and_store(text, phase=phase, slug=slug, store=_store(project))


def _record_approval(project: Path, phase: str) -> None:
    """Record a digest-matching approval exactly as ``run_approve`` does, but
    without advancing the phase — the advance must come from ``evaluate_signal``."""
    handle = _store(project)
    slug = _resolve_workitem_slug_for(handle, project, phase)
    rows = handle.list_artifacts(phase, slug=slug, name_glob=_APPROVAL_STORE_NAME_GLOB[phase])
    handle.set_approval(phase, _artifact_digest(rows), approver="e2e")


def _commit_all(project: Path, message: str) -> None:
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", message)


class TestProxySddFullWorkflow:
    def test_full_lifecycle_via_evaluate_signal(self, project: Path) -> None:
        store = _store(project)

        # ---- intake (lazy-seeded by the proxy on the first managed turn) ----
        _turn(project, "start the work")
        assert _phase(project) == "intake"

        # intake → spec is gated on a spec contract.
        _emit_contract(project, f"phase=spec slug={WORK_SLUG}", "# Spec contract\n")
        _turn(project, "intake done, ready for spec")
        assert _phase(project) == "spec"
        # Proxy cursor seeding: phase entry points at the spec work item.
        assert _cursor(project) == f"active/spec/{WORK_SLUG}.md"

        # ---- spec → design -------------------------------------------------
        _emit_artifact(
            project,
            "spec",
            WORK_SLUG,
            "spec.md",
            "# Spec\n\n## Acceptance Criteria\n- cache invalidates on write\n\n## Out of Scope\n- persistence\n",
        )
        _record_approval(project, "spec")
        _turn(project, "spec approved, move to design")
        assert _phase(project) == "design"

        # ---- design → plan -------------------------------------------------
        _emit_artifact(
            project,
            "design",
            WORK_SLUG,
            "approach.md",
            "# Approach\n\n## Approach\nVersion-stamped cache entries.\n",
        )
        _record_approval(project, "design")
        _turn(project, "design approved, move to plan")
        assert _phase(project) == "plan"

        # ---- plan → build --------------------------------------------------
        _emit_artifact(
            project,
            "plan",
            WORK_SLUG,
            "tasks.md",
            "# Tasks\n\n## Tasks\n- implement cache\n",
        )
        _emit_artifact(
            project,
            "plan",
            WORK_SLUG,
            "test-plan.md",
            "# Test plan\n\n## Test Cases\n- write-through invalidates\n",
        )
        _emit_contract(
            project,
            f"phase=build slug={BUILD_SLUG} route=build tags=backend touches=src/",
            "# Build contract\n",
        )
        _record_approval(project, "plan")
        _turn(project, "plan approved, start build")
        assert _phase(project) == "build"
        # Proxy cursor seeding: build entry points at the build work item.
        assert _cursor(project) == f"active/build/{BUILD_SLUG}.md"

        # ---- build → qa ----------------------------------------------------
        # Nothing committed yet → the build gate must hold.
        _turn(project, "build progress")
        assert _phase(project) == "build"

        (project / "src").mkdir(exist_ok=True)
        (project / "tests").mkdir(exist_ok=True)
        (project / "src" / "cache.py").write_text("CACHE: dict[str, str] = {}\n")
        (project / "tests" / "test_cache.py").write_text(
            "def test_cache() -> None:\n    assert True\n"
        )
        _commit_all(project, "implement cache")

        _turn(project, "build done, ready for qa")
        assert _phase(project) == "qa"

        # ---- qa → ship -----------------------------------------------------
        # The qa report must be a real (non-empty) store artifact. When the qa
        # phase auto-inherits a contract from build (``_auto_create_next_contract``),
        # its slug is the build contract's; otherwise qa is repo-global. Either
        # way we record under the resolved slug, and drop a lesson so the
        # ``lessons_recorded`` gate is satisfied when it IS enforced.
        qa_rows = store.list_contracts(phase="qa", status="active")
        qa_slug = qa_rows[0]["slug"] if qa_rows else WORK_SLUG
        _emit_artifact(
            project,
            "qa",
            qa_slug,
            "report.md",
            "# QA report\n\nAll cache invalidation cases pass; coverage on src/cache.py is 100%.\n",
        )
        # NB: the lesson is stored under the bare name "solution" (never
        # ".artifact") so it satisfies ``lessons_recorded`` without being swept
        # into the qa report's ``*.artifact`` size floor.
        _emit_artifact(
            project,
            "qa",
            qa_slug,
            "solution",
            "# Lesson\n\nAlways invalidate on write.\n",
        )
        _turn(project, "qa passed, ship it")
        assert _phase(project) == "ship"

        # ---- terminal state ------------------------------------------------
        spec_rows = store.list_artifacts("spec", slug=WORK_SLUG)
        assert any(r["name"] == "spec.artifact" for r in spec_rows)
        qa_arts = store.list_artifacts("qa")
        assert any(r["name"] == "report.artifact" for r in qa_arts)
        assert store.get_approval("spec") is not None
        assert store.get_approval("design") is not None
        assert store.get_approval("plan") is not None

        # Contract ledger: phase-scoped ids mean every phase's contract survives
        # the carry-forward — none is upserted over by the next phase's row.
        for carried_phase in ("spec", "design", "plan"):
            rows = store.list_contracts(phase=carried_phase, status="active")
            assert [c["slug"] for c in rows] == [WORK_SLUG], carried_phase
        assert [c["slug"] for c in store.list_contracts(phase="build", status="active")] == [
            BUILD_SLUG
        ]
        # qa inherits the build contract's slug via _auto_create_next_contract.
        assert [c["slug"] for c in store.list_contracts(phase="qa", status="active")] == [
            BUILD_SLUG
        ]


def test_auto_create_next_contract_fires_through_evaluate_signal(project: Path) -> None:
    """spec → design must auto-create a design contract carrying the slug forward.

    This is the proxy-only behavior the lifecycle walk relies on for work-item
    continuity (it is what makes design/plan slug-scoped instead of repo-global).
    Regression guard: ``_resolve_current_contract`` must unwrap the seeded
    cursor's ``active/{phase}/{id}.md`` marker form to the bare store key, or
    ``_auto_create_next_contract``'s ``get_contract`` lookup misses and no
    next-phase contract is ever carried forward.
    """
    _turn(project, "start the work")
    assert _phase(project) == "intake"

    _emit_contract(project, f"phase=spec slug={WORK_SLUG}", "# Spec contract\n")
    _turn(project, "intake done, ready for spec")
    assert _phase(project) == "spec"

    _emit_artifact(
        project,
        "spec",
        WORK_SLUG,
        "spec.md",
        "# Spec\n\n## Acceptance Criteria\n- x\n\n## Out of Scope\n- y\n",
    )
    _record_approval(project, "spec")
    _turn(project, "spec approved, move to design")
    assert _phase(project) == "design"

    # The design contract should have been auto-created from the spec contract…
    design_rows = _store(project).list_contracts(phase="design", status="active")
    assert [c["slug"] for c in design_rows] == [WORK_SLUG]
    # …and the spec contract must survive: phase-scoped contract ids mean the
    # carry-forward adds a sibling row instead of upserting over the spec row.
    spec_rows = _store(project).list_contracts(phase="spec", status="active")
    assert [c["slug"] for c in spec_rows] == [WORK_SLUG]
