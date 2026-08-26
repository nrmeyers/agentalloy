"""End-to-end: the ``sdd-full`` lane walks intake → spec → design → plan →
build → qa → ship through the real machinery.

Unlike the per-gate unit tests (which evaluate a single predicate against a
hand-built store), this drives the *actual* decision points the CLI/proxy use —
``run_phase_set`` / ``run_approve`` / the contract & artifact extractors /
``DuckDBStateStore`` — against a fresh git repo, and asserts at every step that:

* each forward transition is **gated** — blocked until the phase's exit
  contracts, artifacts, approvals, and (for build) committed code exist;
* every contract, artifact, and approval lands in the **state store** — the
  single source of truth, with no disk mirror re-introduced;
* the build gate checks **real git commits** inside the contracted scope.

It exists to prove the store-only refactor (``refactor/store-only-state``)
didn't strand the full lifecycle: a walk from intake to ship must still be
possible end to end with contracts and gates enforced.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agentalloy.api import artifact_extractor, contract_extractor
from agentalloy.install.subcommands._state import phase_access
from agentalloy.install.subcommands.approve import run_approve
from agentalloy.install.subcommands.phase import run_phase_get, run_phase_set

# One work item spans the whole lifecycle: a single slug carried from the spec
# contract through spec/design/plan artifacts.
WORK_SLUG = "feat-cache-invalidation"
BUILD_SLUG = "01-implement-cache"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A git repo with one commit, so the build gate has a base ref to diff."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# fixture\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _git(root: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "e2e",
        "GIT_AUTHOR_EMAIL": "e2e@example.com",
        "GIT_COMMITTER_NAME": "e2e",
        "GIT_COMMITTER_EMAIL": "e2e@example.com",
    }
    merged = {**os.environ, **env}
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=merged,
    )
    return out.stdout.strip()


def _store(project: Path):
    """The scoped DuckDB handle for *project* — same bucket run_phase_set uses."""
    return phase_access(project).contracts_handle()


def _emit_contract(project: Path, attrs: str, body: str) -> None:
    """Simulate the agent emitting a contract marker; route through the extractor."""
    text = f"<!-- agentalloy:contract {attrs} -->\n{body}\n<!-- /agentalloy:contract -->"
    contract_extractor.extract_and_store(text, project_root=project)


def _emit_artifact(project: Path, phase: str, slug: str, name: str, body: str) -> None:
    """Simulate the agent recording a lifecycle artifact via a marker."""
    text = f"<!-- agentalloy:artifact name={name} -->\n{body}\n<!-- /agentalloy:artifact -->"
    artifact_extractor.extract_and_store(text, phase=phase, slug=slug, store=_store(project))


def _current(project: Path) -> str | None:
    return run_phase_get(root=project).get("phase")


def _active_contracts(project: Path, phase: str) -> list[dict]:
    return _store(project).list_contracts(phase=phase, status="active")


def _commit_all(project: Path, message: str) -> None:
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", message)


class TestSddFullWorkflow:
    """Walk intake → ship; every transition must be gated until satisfied."""

    def test_full_lifecycle(self, project: Path) -> None:
        store = _store(project)

        # ---- intake (entry) ------------------------------------------------
        result = run_phase_set("intake", root=project)
        assert result["blocked"] is False
        assert _current(project) == "intake"

        # Forward-skip is never allowed: intake → design jumps over spec.
        skip = run_phase_set("design", root=project)
        assert skip["blocked"] is True
        assert skip["reason"] == "forward_skip"
        assert _current(project) == "intake"

        # intake → spec is gated on a spec contract existing.
        blocked = run_phase_set("spec", root=project)
        assert blocked["blocked"] is True
        assert blocked["reason"] == "not_met"
        assert _current(project) == "intake"

        _emit_contract(project, f"phase=spec slug={WORK_SLUG}", "# Spec contract\n")
        assert [c["slug"] for c in _active_contracts(project, "spec")] == [WORK_SLUG]

        result = run_phase_set("spec", root=project)
        assert result["blocked"] is False
        assert _current(project) == "spec"

        # ---- spec → design -------------------------------------------------
        # Gated on the exit artifact *and* human approval. With no artifact yet,
        # approve must refuse.
        refused = run_approve("spec", root=project)
        assert refused["ok"] is False

        _emit_artifact(
            project,
            "spec",
            WORK_SLUG,
            "spec.md",
            "# Spec\n\n## Acceptance Criteria\n- cache invalidates on write\n\n## Out of Scope\n- persistence\n",
        )
        # Artifact present but not yet approved → the approval gate must hold.
        blocked = run_phase_set("design", root=project)
        assert blocked["blocked"] is True
        assert blocked["reason"] == "approval"
        assert _current(project) == "spec"

        approved = run_approve("spec", root=project)
        assert approved["ok"] is True
        assert approved["advanced"]["blocked"] is False
        assert _current(project) == "design"
        assert store.get_approval("spec") is not None

        # ---- design → plan -------------------------------------------------
        _emit_artifact(
            project,
            "design",
            WORK_SLUG,
            "approach.md",
            "# Approach\n\n## Approach\nVersion-stamped cache entries.\n",
        )
        approved = run_approve("design", root=project)
        assert approved["ok"] is True
        assert _current(project) == "plan"

        # ---- plan → build --------------------------------------------------
        _emit_artifact(
            project,
            "plan",
            WORK_SLUG,
            "tasks.md",
            "# Tasks\n\n## Tasks\n- implement cache\n- wire invalidation\n",
        )
        _emit_artifact(
            project,
            "plan",
            WORK_SLUG,
            "test-plan.md",
            "# Test plan\n\n## Test Cases\n- write-through invalidates\n",
        )
        # The build contract carries the scope the build gate will enforce.
        _emit_contract(
            project,
            f"phase=build slug={BUILD_SLUG} route=build tags=backend touches=src/",
            "# Build contract\n",
        )
        build_contracts = _active_contracts(project, "build")
        assert [c["slug"] for c in build_contracts] == [BUILD_SLUG]
        assert build_contracts[0]["scope_touches"] == ["src/"]

        approved = run_approve("plan", root=project)
        assert approved["ok"] is True
        assert _current(project) == "build"

        # ---- build → qa ----------------------------------------------------
        # Nothing committed yet: the build gate must block on scope + tests.
        blocked = run_phase_set("qa", root=project)
        assert blocked["blocked"] is True
        assert _current(project) == "build"

        (project / "src").mkdir(exist_ok=True)
        (project / "tests").mkdir(exist_ok=True)
        (project / "src" / "cache.py").write_text("CACHE: dict[str, str] = {}\n")
        (project / "tests" / "test_cache.py").write_text(
            "def test_cache() -> None:\n    assert True\n"
        )
        _commit_all(project, "implement cache")

        result = run_phase_set("qa", root=project)
        assert result["blocked"] is False
        assert _current(project) == "qa"

        # ---- qa → ship -----------------------------------------------------
        # Gated on a real (non-empty) qa record in the store.
        blocked = run_phase_set("ship", root=project)
        assert blocked["blocked"] is True
        assert _current(project) == "qa"

        _emit_artifact(
            project,
            "qa",
            WORK_SLUG,
            "report.md",
            "# QA report\n\nAll cache invalidation cases pass; coverage on src/cache.py is 100%.\n",
        )
        result = run_phase_set("ship", root=project)
        assert result["blocked"] is False
        assert _current(project) == "ship"

        # ---- terminal state ------------------------------------------------
        # The whole lifecycle's contracts and artifacts are store-backed rows.
        spec_rows = store.list_artifacts("spec", slug=WORK_SLUG)
        assert any(r["name"] == "spec.artifact" for r in spec_rows)
        qa_rows = store.list_artifacts("qa")
        assert any(r["name"] == "report.artifact" for r in qa_rows)
        assert store.list_contracts(phase="spec", status="active")

        # Store-only invariant (the point of this branch): nothing in the
        # lifecycle spilled to a disk mirror. Artifacts and contracts live in
        # the store, not under docs/ or .agentalloy/contracts/.
        assert not (project / "docs").exists()
        assert not (project / ".agentalloy" / "contracts").exists()

    def test_build_gate_requires_scope_touch_not_just_tests(self, project: Path) -> None:
        """Committing only tests does not satisfy the build scope gate.

        The build contract scopes work to ``src/``; a tests-only commit must not
        count as touching the contracted scope (regression guard for the
        ``scope_touched_in_diff`` test-path exclusion).
        """
        run_phase_set("intake", root=project)
        _emit_contract(project, f"phase=spec slug={WORK_SLUG}", "# Spec contract\n")
        run_phase_set("spec", root=project)
        _emit_artifact(
            project,
            "spec",
            WORK_SLUG,
            "spec.md",
            "# Spec\n\n## Acceptance Criteria\n- x\n\n## Out of Scope\n- y\n",
        )
        run_approve("spec", root=project)
        _emit_artifact(
            project, "design", WORK_SLUG, "approach.md", "# Approach\n\n## Approach\nA.\n"
        )
        run_approve("design", root=project)
        _emit_artifact(project, "plan", WORK_SLUG, "tasks.md", "# Tasks\n\n## Tasks\n- t\n")
        _emit_artifact(project, "plan", WORK_SLUG, "test-plan.md", "# TP\n\n## Test Cases\n- c\n")
        _emit_contract(
            project,
            f"phase=build slug={BUILD_SLUG} route=build tags=backend touches=src/",
            "# Build contract\n",
        )
        run_approve("plan", root=project)
        assert _current(project) == "build"

        # Commit ONLY a test file — scope (src/) is untouched.
        (project / "tests").mkdir(exist_ok=True)
        (project / "tests" / "test_only.py").write_text("def test_x() -> None:\n    pass\n")
        _commit_all(project, "tests only")

        blocked = run_phase_set("qa", root=project)
        assert blocked["blocked"] is True
        assert blocked["reason"] == "not_met"
        assert _current(project) == "build"
