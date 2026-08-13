"""Compose ``debug`` flag: per-stage retrieval detail mirrored into the response."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from agentalloy.api.compose_models import ComposeRequest
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.reads.models import ActiveFragment, ActiveSkill
from agentalloy.retrieval.domain import RetrievalResult
from agentalloy.retrieval.system import SystemRetrievalResult
from agentalloy.runtime_state import RuntimeCache, VersionDetail
from agentalloy.storage.protocols import FragmentStore


def _orchestrator() -> tuple[ComposeOrchestrator, ActiveFragment]:
    skill = ActiveSkill(
        skill_id="sk-dbg",
        canonical_name="debug skill",
        category="engineering",
        skill_class="domain",
        domain_tags=["python"],
        always_apply=False,
        phase_scope=None,
        category_scope=None,
        active_version_id="sk-dbg-v1",
        tier=None,
    )
    vd = VersionDetail(
        version_id="sk-dbg-v1",
        version_number=1,
        authored_at=datetime.now(UTC),
        author="t",
        change_summary="t",
        raw_prose="p" * 200,
    )
    fragment = ActiveFragment(
        fragment_id="f-dbg-1",
        fragment_type="execution",
        sequence=1,
        content="x" * 80,
        skill_id="sk-dbg",
        version_id="sk-dbg-v1",
        skill_class="domain",
        category="engineering",
        domain_tags=["python"],
    )
    cache = RuntimeCache(
        skills={"sk-dbg": skill}, fragments=[fragment], version_details={"sk-dbg-v1": vd}
    )
    orch = ComposeOrchestrator(
        source=cache,
        lm=MagicMock(),
        vector_store=MagicMock(spec=FragmentStore),
        telemetry=MagicMock(),
        embedding_model="fake-embed",
    )

    async def _fake_retrieve(req: ComposeRequest) -> RetrievalResult:
        return RetrievalResult(
            candidates=[fragment],
            retrieval_ms=1,
            eligible_count=7,
            scores_by_id={"f-dbg-1": 1.0, "f-other": 0.5},
            skills_ranked=["sk-dbg", "sk-other"],
            bm25_source="union",
            reranked=True,
            lm_assist_outcome="hit",
            lm_assist_kept_ids=["f-dbg-1"],
            lm_assist_dropped_ids=["f-other"],
            lm_assist_scores={"f-dbg-1": 0.91},
        )

    async def _fake_system(req: ComposeRequest) -> SystemRetrievalResult:
        return SystemRetrievalResult(candidates=[], applied_skill_ids=[], retrieval_ms=0)

    orch.retrieve = _fake_retrieve  # type: ignore[method-assign]
    orch.retrieve_system = _fake_system  # type: ignore[method-assign]
    return orch, fragment


async def test_debug_flag_mirrors_retrieval_internals() -> None:
    orch, _ = _orchestrator()
    result = await orch.compose(ComposeRequest(task="t", phase="build", debug=True))
    dbg = result.debug
    assert dbg is not None
    assert dbg.eligible_count == 7
    assert dbg.scores_by_id["f-dbg-1"] == 1.0
    assert dbg.skills_ranked == ["sk-dbg", "sk-other"]
    assert dbg.bm25_source == "union"
    assert dbg.reranked is True
    assert dbg.lm_assist_outcome == "hit"
    assert dbg.lm_assist_kept_ids == ["f-dbg-1"]
    assert dbg.lm_assist_scores == {"f-dbg-1": 0.91}


async def test_debug_defaults_off_and_absent_from_payload() -> None:
    orch, _ = _orchestrator()
    result = await orch.compose(ComposeRequest(task="t", phase="build"))
    assert result.debug is None
    # Absent (None) rather than an empty object, so non-debug payloads are
    # byte-identical to pre-flag responses when serialized without None fields.
    assert result.model_dump(exclude_none=True).get("debug") is None
