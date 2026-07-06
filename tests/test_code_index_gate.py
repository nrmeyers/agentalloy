"""sys-code-index availability gate — proxy-side drop-by-skill_id filter.

The capability layer is deferred by design (docs/design/sdd-golden-skills.md),
so sys-code-index — the one runtime-conditional system skill — is gated at the
proxy compose boundary: :mod:`agentalloy.api.code_index_gate` probes the
indexed-repos registry (fail-closed) and the orchestrator drops the skill from
the system leg. Three layers under test:

1. the availability probe itself (settings toggle, registry presence,
   last_indexed_at, corruption → fail-closed False, never raises);
2. the orchestrator's ``exclude_system_skill_ids`` post-retrieval filter;
3. the shared proxy seam (:func:`apply_signal`) end to end — prose present
   only when the gate says available;
4. phase scoping of the real bundled YAML (absent at intake/spec even when
   the repo is indexed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentalloy.api import code_index_gate
from agentalloy.api.code_index_gate import (
    CODE_INDEX_SKILL_ID,
    code_index_available,
    system_skill_exclusions,
)
from agentalloy.api.compose_models import ComposeRequest
from agentalloy.api.proxy_apply import apply_signal
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.applicability import filter_applicable_system_skills
from agentalloy.config import Settings
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.reads.models import ActiveFragment, ActiveSkill
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.system import SystemRetrievalResult

# ---------------------------------------------------------------------------
# 1. Availability probe
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, *, enabled: bool) -> Settings:
    return Settings(code_index_enabled=enabled, code_index_data_dir=str(tmp_path / "ci-data"))


def _register(settings: Settings, repo_dir: Path, *, indexed: bool) -> None:
    """Enroll ``repo_dir`` in the registry the same way an index run would."""
    from agentalloy.code_index.slug import repo_slug
    from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore

    root = Path(settings.code_index_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    slug = repo_slug(repo_dir)
    store = CodeIndexJobsStore(root / "jobs.sqlite")
    try:
        store.upsert_repo(slug=slug, repo_path=str(repo_dir), data_dir=str(root / "repos" / slug))
        if indexed:
            store.mark_indexed(slug)
    finally:
        store.close()


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "myrepo"
    d.mkdir()
    return d


def test_enabled_and_indexed_is_available(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    _register(settings, repo_dir, indexed=True)
    assert code_index_available(str(repo_dir), settings) is True
    assert system_skill_exclusions(str(repo_dir), settings) == frozenset()


def test_module_disabled_is_unavailable(tmp_path: Path, repo_dir: Path) -> None:
    # Registry says indexed, but the module toggle is off — fail closed.
    enabled = _settings(tmp_path, enabled=True)
    _register(enabled, repo_dir, indexed=True)
    disabled = _settings(tmp_path, enabled=False)
    assert code_index_available(str(repo_dir), disabled) is False
    assert system_skill_exclusions(str(repo_dir), disabled) == frozenset({CODE_INDEX_SKILL_ID})


def test_repo_not_in_registry_is_unavailable(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    other = repo_dir.parent / "otherrepo"
    other.mkdir()
    _register(settings, other, indexed=True)
    assert code_index_available(str(repo_dir), settings) is False


def test_enrolled_but_never_indexed_is_unavailable(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    _register(settings, repo_dir, indexed=False)  # last_indexed_at stays NULL
    assert code_index_available(str(repo_dir), settings) is False


def test_no_registry_file_is_unavailable(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    assert code_index_available(str(repo_dir), settings) is False


def test_none_repo_is_unavailable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    assert code_index_available(None, settings) is False


def test_corrupt_registry_fails_closed_without_raising(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    root = Path(settings.code_index_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "jobs.sqlite").write_bytes(b"this is not a sqlite database")
    assert code_index_available(str(repo_dir), settings) is False


# ---------------------------------------------------------------------------
# 2. Orchestrator post-retrieval filter (exclude_system_skill_ids)
# ---------------------------------------------------------------------------


def _system_frag(fid: str, skill_id: str) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type="governance",
        sequence=1,
        content=f"SYSTEM {skill_id}",
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="system",
        category="tooling",
        domain_tags=[],
    )


class _StubOrchestrator(ComposeOrchestrator):
    """Real compose() over stubbed retrieval legs (test_compose_legs pattern)."""

    def __init__(self) -> None:
        from agentalloy.telemetry.writer import NullTelemetryWriter

        self._embedding_model = "fake"
        self._telemetry = NullTelemetryWriter()

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult:  # noqa: ARG002
        return RetrievalResult(candidates=[], eligible_count=0, retrieval_ms=1)

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:  # noqa: ARG002
        return SystemRetrievalResult(
            candidates=[
                _system_frag("sci-f1", CODE_INDEX_SKILL_ID),
                _system_frag("ci-f1", "sys-ci"),
            ],
            applied_skill_ids=[CODE_INDEX_SKILL_ID, "sys-ci"],
            retrieval_ms=1,
        )


async def test_compose_drops_excluded_system_skill() -> None:
    orch = _StubOrchestrator()
    req = ComposeRequest(task="entering build", phase="build", legs="system")
    result = await orch.compose(req, exclude_system_skill_ids=frozenset({CODE_INDEX_SKILL_ID}))
    assert result.result_type == "composed"  # type: ignore[attr-defined]
    assert f"SYSTEM {CODE_INDEX_SKILL_ID}" not in result.output  # type: ignore[attr-defined]
    assert "SYSTEM sys-ci" in result.output  # type: ignore[attr-defined]
    # Provenance must not claim a fragment that was never injected.
    assert "sci-f1" not in result.system_fragments  # type: ignore[attr-defined]
    assert "ci-f1" in result.system_fragments  # type: ignore[attr-defined]


async def test_compose_keeps_skill_without_exclusions() -> None:
    orch = _StubOrchestrator()
    req = ComposeRequest(task="entering build", phase="build", legs="system")
    result = await orch.compose(req)
    assert f"SYSTEM {CODE_INDEX_SKILL_ID}" in result.output  # type: ignore[attr-defined]
    assert "SYSTEM sys-ci" in result.output  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 3. Proxy seam: apply_signal threads the gate into the Tier 1 compose
# ---------------------------------------------------------------------------


def _signal(repo: str) -> SignalResult:
    return SignalResult(
        should_compose=True,
        phase="build",
        task="implement the cache",
        announce=True,
        repo=repo,
    )


async def _inject_via_proxy(repo: str) -> str:
    outcome = await apply_signal(
        signal=_signal(repo),
        orchestrator=_StubOrchestrator(),
        inject=lambda text: text,
        delivered=lambda injected: True,
    )
    assert outcome.injected is not None, "system prose must still compose"
    return str(outcome.injected)


async def test_proxy_injects_code_index_prose_when_available(
    monkeypatch: pytest.MonkeyPatch, repo_dir: Path
) -> None:
    monkeypatch.setattr(code_index_gate, "code_index_available", lambda repo, settings=None: True)
    text = await _inject_via_proxy(str(repo_dir))
    assert f"SYSTEM {CODE_INDEX_SKILL_ID}" in text
    assert "SYSTEM sys-ci" in text


async def test_proxy_drops_code_index_prose_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, repo_dir: Path
) -> None:
    monkeypatch.setattr(code_index_gate, "code_index_available", lambda repo, settings=None: False)
    text = await _inject_via_proxy(str(repo_dir))
    assert f"SYSTEM {CODE_INDEX_SKILL_ID}" not in text
    # The rest of the system leg is untouched — compose still succeeds.
    assert "SYSTEM sys-ci" in text


async def test_proxy_drops_prose_when_probe_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_dir: Path
) -> None:
    # Real probe against a corrupt registry: fail-closed drop, compose intact.
    settings = _settings(tmp_path, enabled=True)
    root = Path(settings.code_index_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "jobs.sqlite").write_bytes(b"garbage")
    monkeypatch.setenv("CODE_INDEX_ENABLED", "1")
    monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(root))
    text = await _inject_via_proxy(str(repo_dir))
    assert f"SYSTEM {CODE_INDEX_SKILL_ID}" not in text
    assert "SYSTEM sys-ci" in text


# ---------------------------------------------------------------------------
# 4. Phase scoping of the bundled YAML (design/build/qa only)
# ---------------------------------------------------------------------------

_SKILL_YAML = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentalloy"
    / "_packs"
    / "sys"
    / "sys-code-index.yaml"
)


def _bundled_active_skill() -> ActiveSkill:
    raw: dict[str, Any] = yaml.safe_load(_SKILL_YAML.read_text())
    return ActiveSkill(
        skill_id=raw["skill_id"],
        canonical_name=raw["canonical_name"],
        category=raw["category"],
        skill_class=raw["skill_class"],
        domain_tags=list(raw.get("domain_tags") or []),
        always_apply=bool(raw.get("always_apply")),
        phase_scope=raw.get("phase_scope"),
        category_scope=raw.get("category_scope"),
        active_version_id=f"{raw['skill_id']}-v1",
        tier=None,
    )


@pytest.mark.parametrize("phase", ["design", "build", "qa"])
def test_bundled_skill_applies_in_scoped_phases(phase: str) -> None:
    skill = _bundled_active_skill()
    assert filter_applicable_system_skills([skill], phase=phase, category=None) == [skill]


@pytest.mark.parametrize("phase", ["intake", "spec", "ship", None])
def test_bundled_skill_absent_outside_scope(phase: str | None) -> None:
    # Even an indexed repo never sees the prose at intake/spec/ship — the
    # applicability predicate excludes it before the availability gate runs.
    skill = _bundled_active_skill()
    assert filter_applicable_system_skills([skill], phase=phase, category=None) == []
