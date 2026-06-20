"""Hermetic end-to-end SDD lifecycle harness.

Drives the Claude Code hook surface (`/v1/hook/...`) through the *real* signal
pipeline — trigger -> gate -> compose -> inject -> phase-write -> telemetry — over
a full intake->spec->design->build->qa->ship walk. The only seams neutralized are
the non-deterministic LM verdicts:

  * `signals.classifier._classify_intent` -> a scriptable oracle (controls which
    prompts read as completion/approval signals);
  * the embedder -> `StubLMClient` (deterministic 768-dim vectors, no network).

Everything else runs real: `check_transition_trigger` (incl. its keyword
fallback), `decide_transition`, the shipped `_packs/sdd` exit gates, the compose
orchestrator, and the DuckDB telemetry writer. This reproduces — as assertions —
the four failures a live laptop run exhibited while all 2200 mocked-seam tests
stayed green:

  1. contract write records no telemetry trace,
  2. natural-language "now the design" doesn't advance spec->design,
  3. only the intake contract is ever composed,
  4. a spec written to the wrong path stalls with no actionable guidance.

Non-integration: runs in the default CI suite with no live embed/reranker server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentalloy.api.compose_router import get_orchestrator
from agentalloy.api.skill_router import get_skill_store
from agentalloy.app import create_app
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.runtime_state import load_runtime_cache
from agentalloy.signals.predicates import PredicateResult
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import open_or_create
from agentalloy.telemetry.writer import DuckDBTelemetryWriter
from tests.support import StubLMClient

EMBED_MODEL = "stub-embed"
TAGS = ["fastapi", "sqlite", "api", "python"]


# ---------------------------------------------------------------------------
# Project scaffold — a temp repo the hook reads phase/artifacts from
# ---------------------------------------------------------------------------


class ProjectDir:
    """A temp project root with a `.agentalloy/phase` file and helpers to lay
    down the deliverable artifacts each SDD gate checks for."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
        self.set_phase("intake")

    def set_phase(self, phase: str) -> None:
        (self.root / ".agentalloy" / "phase").write_text(f"phase: {phase}\n")

    def read_phase(self) -> str:
        return (self.root / ".agentalloy" / "phase").read_text().strip()

    def write_contract(self, slug: str, phase: str, tags: list[str]) -> Path:
        d = self.root / ".agentalloy" / "contracts" / phase
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{slug}.md"
        p.write_text(
            f"---\nphase: {phase}\ntask_slug: {slug}\ndomain_tags: [{', '.join(tags)}]\n---\n\n"
            f"# {slug}\n\nBuild a FastAPI service that stores URLs with tags in SQLite, "
            f"authenticates with a bearer token, and lists/filters saved links.\n"
        )
        return p

    def write_spec(self, slug: str, *, at_root: bool = False) -> Path:
        body = "# Spec\n\n## Acceptance Criteria\n\n- POST /links works\n\n## Out of Scope\n\n- frontend\n"
        if at_root:
            p = self.root / f"{slug}-spec.md"  # the laptop's wrong path
        else:
            d = self.root / "docs" / "spec"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{slug}.md"
        p.write_text(body)
        return p

    def write_design(self, slug: str) -> Path:
        # Design fans out into a per-slug folder: approach.md + tasks.md are the
        # two gated files (plus component files as the work needs).
        d = self.root / "docs" / "design" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "approach.md").write_text(
            "# Design\n\n## Approach\n\nmodule layout: main.py, db.py, auth.py\n"
        )
        (d / "tasks.md").write_text(
            "# Tasks\n\n## Tasks\n\n- T1: schema (satisfies AC-1)\n- T2: API (satisfies AC-2)\n"
        )
        (d / "test-plan.md").write_text(
            "# Test Plan\n\n## Test Cases\n\n"
            "- TC1: POST /links creates a link -> 201 (proves AC-1, task T1)\n"
            "- TC2: GET /links lists links (proves AC-2, task T2)\n"
        )
        return d

    def write_build(self) -> None:
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "src" / "app.py").write_text("def app():\n    return 1\n")
        (self.root / "tests").mkdir(parents=True, exist_ok=True)
        (self.root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n")

    def write_qa(self, slug: str) -> Path:
        d = self.root / "docs" / "qa"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{slug}.md"
        p.write_text(
            "# QA\n\n"
            "## Checks\n\nsuite green, lint clean, types clean\n\n"
            "## Review\n\nAC-1/AC-2 met; non-goals respected; no Critical findings\n"
        )
        return p


# ---------------------------------------------------------------------------
# Scriptable intent oracle — the single neutralized LM verdict
# ---------------------------------------------------------------------------


class _IntentOracle:
    """Stand-in for `_classify_intent`. Returns MET for a completion/approval
    intent when the prompt contains one of `hits`; NOT_MET otherwise. Keeps the
    real `check_transition_trigger` branching and keyword fallback intact."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def __call__(self, text: str, intent: str, lm_client: Any, model: str) -> PredicateResult:
        if intent in ("completion", "approval") and any(
            h.lower() in text.lower() for h in self.hits
        ):
            return PredicateResult.MET
        return PredicateResult.NOT_MET


@pytest.fixture
def intent_oracle(monkeypatch: pytest.MonkeyPatch) -> _IntentOracle:
    oracle = _IntentOracle()
    monkeypatch.setattr("agentalloy.signals.classifier._classify_intent", oracle)
    # The hook builds its own embed client for the trigger; keep it off the network.
    monkeypatch.setattr(
        "agentalloy.embed_provider.get_embed_client", lambda _cfg=None: StubLMClient()
    )
    return oracle


# ---------------------------------------------------------------------------
# App + real corpus + real telemetry writer, hermetic
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(corpus_dir: Path):
    lb = LadybugStore(str(corpus_dir / "ladybug"))
    lb.open()
    vs = open_or_create(corpus_dir / "skills.duck")
    runtime = load_runtime_cache(lb)
    orch = ComposeOrchestrator(
        runtime, StubLMClient(), vs, DuckDBTelemetryWriter(vs), embedding_model=EMBED_MODEL
    )
    app = create_app(use_default_lifespan=False)
    app.dependency_overrides[get_orchestrator] = lambda: orch
    app.dependency_overrides[get_skill_store] = lambda: lb  # PreToolUse system-skill source
    client = TestClient(app)
    try:
        yield client, vs
    finally:
        client.close()
        vs.close()
        lb.close()


@pytest.fixture
def project(tmp_path: Path) -> ProjectDir:
    return ProjectDir(tmp_path / "proj")


def _ups(client: TestClient, project: ProjectDir, prompt: str) -> dict[str, Any]:
    resp = client.post(
        "/v1/hook/user-prompt-submit", json={"prompt": prompt, "cwd": str(project.root)}
    )
    assert resp.status_code == 200
    return resp.json()


def _ptu(client: TestClient, project: ProjectDir, path: Path) -> dict[str, Any]:
    resp = client.post(
        "/v1/hook/post-tool-use",
        json={
            "tool_name": "Write",
            "tool_input": {"file_path": str(path)},
            "cwd": str(project.root),
        },
    )
    assert resp.status_code == 200
    return resp.json()


def _pre(client: TestClient, project: ProjectDir) -> dict[str, Any]:
    resp = client.post(
        "/v1/hook/pre-tool-use",
        json={"tool_name": "Write", "cwd": str(project.root)},
    )
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# The full lifecycle walk
# ---------------------------------------------------------------------------


def test_full_lifecycle_walk(app_client, project: ProjectDir, intent_oracle: _IntentOracle) -> None:
    """intake -> spec -> design -> build -> qa -> ship, one prompt per phase, each
    advancing only once its real exit artifact exists."""
    client, _vs = app_client
    slug = "linkvault2"
    # Each step's prompt contains the next phase's name; the oracle reads any of
    # these as a completion signal. (intake bypasses the trigger entirely.)
    intent_oracle.hits = ["design", "build", "qa", "ship"]

    # intake -> spec: a contract exists.
    project.write_contract(slug, "intake", TAGS)
    r = _ups(client, project, "the contract captures it; let's go")
    assert r["transition"] is True and r["to_phase"] == "spec"
    assert project.read_phase() == "phase: spec"

    # spec -> design: docs/spec/<slug>.md with required sections.
    project.write_spec(slug)
    r = _ups(client, project, "Looks right. Now the design: module layout, models, auth.")
    assert r["transition"] is True and r["to_phase"] == "design"
    assert project.read_phase() == "phase: design"
    # The next phase's workflow skill is injected as the scaffold (not missing).
    assert "[agentalloy-workflow]" in r["composed_block"]

    # design -> build.
    project.write_design(slug)
    r = _ups(client, project, "design approved — go build it")
    assert r["transition"] is True and r["to_phase"] == "build"

    # build -> qa: src/** and tests/**.
    project.write_build()
    r = _ups(client, project, "implementation done; run qa")
    assert r["transition"] is True and r["to_phase"] == "qa"

    # qa -> ship: docs/qa/<slug>.md with Checks + Review.
    project.write_qa(slug)
    r = _ups(client, project, "tests pass — ship it")
    assert r["transition"] is True and r["to_phase"] == "ship"
    assert project.read_phase() == "phase: ship"

    # ship is terminal: no further transition.
    r = _ups(client, project, "shipped and announced")
    assert r["transition"] is False
    assert project.read_phase() == "phase: ship"


# ---------------------------------------------------------------------------
# Laptop failure #2 — natural-language transition (and its negative control)
# ---------------------------------------------------------------------------


def test_natural_language_advances_spec_to_design(
    app_client, project: ProjectDir, intent_oracle: _IntentOracle
) -> None:
    """The exact laptop case: a natural-language design request advances spec->design
    via the intent-primary trigger (no rigid keyword needed)."""
    client, _vs = app_client
    project.set_phase("spec")
    project.write_spec("linkvault")
    intent_oracle.hits = ["the design"]
    r = _ups(
        client, project, "The spec looks right. Now the design: module layout, the models, auth."
    )
    assert r["transition"] is True and r["to_phase"] == "design"


def test_no_transition_without_intent_or_keyword(
    app_client, project: ProjectDir, intent_oracle: _IntentOracle
) -> None:
    """Negative control: the spec deliverable is present (guard would pass), but
    with no completion intent and no keyword the phase must NOT advance — proving
    the trigger gates the transition, not just the artifact."""
    client, _vs = app_client
    project.set_phase("spec")
    project.write_spec("linkvault")
    intent_oracle.hits = []  # nothing reads as completion
    r = _ups(client, project, "what about rate limiting on the list endpoint?")
    assert r.get("transition") in (None, False)
    assert r["should_compose"] is False
    assert project.read_phase() == "phase: spec"


# ---------------------------------------------------------------------------
# Laptop failures #1 & #3 — contract write composes domain skills + telemetry
# ---------------------------------------------------------------------------


def test_contract_write_composes_and_records_telemetry(app_client, project: ProjectDir) -> None:
    """A contract write triggers domain compose and records a telemetry trace —
    directly reproducing 'No compose traces recorded yet'."""
    client, vs = app_client
    assert vs.count_traces() == 0
    project.set_phase("build")
    contract = project.write_contract("linkvault2", "build", TAGS)
    r = _ptu(client, project, contract)
    assert r["status"] == "composed"
    assert r["composed_block"]
    assert vs.count_traces() >= 1


def test_contract_with_empty_domain_tags_still_records_telemetry(
    app_client, project: ProjectDir
) -> None:
    """The shipped contract template ships `domain_tags: []`. An empty-tags
    contract must still compose from the body text and record a trace — guards the
    v2.4.0 fix that stopped rejecting empty domain_tags at parse."""
    client, vs = app_client
    assert vs.count_traces() == 0
    project.set_phase("build")
    contract = project.write_contract("linkvault2", "build", [])  # domain_tags: []
    r = _ptu(client, project, contract)
    assert r["status"] == "composed"
    assert vs.count_traces() >= 1


def test_write_outside_contracts_is_no_action(app_client, project: ProjectDir) -> None:
    """A write that isn't a contract composes nothing and records nothing."""
    client, vs = app_client
    note = project.root / "notes.md"
    note.write_text("scratch\n")
    r = _ptu(client, project, note)
    assert r["status"] == "no_action"
    assert vs.count_traces() == 0


# ---------------------------------------------------------------------------
# Laptop failure #4 — wrong-path deliverable yields a near-miss advisory
# ---------------------------------------------------------------------------


def test_wrong_path_spec_yields_near_miss_advisory(
    app_client, project: ProjectDir, intent_oracle: _IntentOracle
) -> None:
    """Spec written to the repo root (not docs/spec/): no transition, and the
    advisory in the response names the misplaced file and where it belongs.
    Also the first test to assert an advisory reaches the HTTP response body."""
    client, _vs = app_client
    project.set_phase("spec")
    project.write_spec("linkvault2", at_root=True)  # -> ./linkvault2-spec.md
    intent_oracle.hits = ["the design"]
    r = _ups(client, project, "Looks right. Now the design.")
    assert r["transition"] is False
    block = r["composed_block"]
    assert "linkvault2-spec.md" in block
    assert "docs/spec" in block
    assert project.read_phase() == "phase: spec"


# ---------------------------------------------------------------------------
# Stage 0 — system skills inject via the hook (PreToolUse)
# ---------------------------------------------------------------------------


def _system_ids(resp: dict[str, Any]) -> str:
    return "\n".join(resp.get("system_skills") or [])


def test_always_apply_system_skill_injects_at_pretooluse(app_client, project: ProjectDir) -> None:
    """The harness bug: system skills never injected through the hook because the
    PreToolUse path read a never-populated `applies_when` gate. With the corpus
    scope-based path, an always_apply system skill injects at every phase."""
    client, _vs = app_client
    for phase in ("intake", "spec", "design", "build", "qa", "ship"):
        project.set_phase(phase)
        assert "sys-governance-always" in _system_ids(_pre(client, project)), phase


def test_phase_scoped_system_skill_injects_only_in_its_phase(
    app_client, project: ProjectDir
) -> None:
    """A `phase_scope: [build]` system skill injects at build alongside the
    always-apply one; at spec, only the always-apply one is present."""
    client, _vs = app_client

    project.set_phase("build")
    at_build = _system_ids(_pre(client, project))
    assert "sys-governance-always" in at_build
    assert "sys-governance-build-phase" in at_build

    project.set_phase("spec")
    at_spec = _system_ids(_pre(client, project))
    assert "sys-governance-always" in at_spec
    assert "sys-governance-build-phase" not in at_spec
