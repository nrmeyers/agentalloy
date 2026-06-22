"""ComposeOrchestrator — wires retrieval and raw-fragment assembly.

Per v5.4: the runtime path holds no generative LLM. ``/compose`` retrieves
domain + system fragments and returns the concatenated raw fragment text
plus provenance. The inference model on the iGPU stitches this into its
own prompt; no second LLM call happens here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from agentalloy.api.compose_models import (
    DEFAULT_MAX_TOKENS_BY_PHASE,
    ComposedResult,
    ComposeRequest,
    EmptyResult,
    ErrorAvailable,
    ErrorCode,
    LatencyBreakdown,
)
from agentalloy.embed_provider import EmbedClient
from agentalloy.lm_client import (
    LMClientError,
    LMModelNotLoaded,
)
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import (
    RetrievalResult,
    retrieve_domain_candidates,
)
from agentalloy.retrieval.embedding_errors import (
    EmbeddingError,
    EmbeddingErrorResult,
)
from agentalloy.retrieval.system import SystemRetrievalResult, retrieve_system_fragments
from agentalloy.runtime_state import RuntimeCache
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore
from agentalloy.telemetry import TelemetryRecord, TelemetryWriter

logger = logging.getLogger(__name__)

# Output is no longer LLM-synthesized; "assembly_tier" is preserved in the
# response shape for backwards compatibility but reports 0 to signal that no
# generative tier was used.
ASSEMBLY_TIER = 0


@dataclass(frozen=True)
class _StageErrorBase(Exception):
    code: ErrorCode
    message: str
    available: ErrorAvailable | None = None


class RetrievalStageError(_StageErrorBase):
    """Retrieval failed — maps to HTTP 503 stage=retrieval."""


class AssemblyStageError(_StageErrorBase):
    """Reserved for backwards compatibility. v5.4 removes the LLM assembly
    stage; this class remains so callers that still ``except`` it keep
    compiling, but it is no longer raised by the runtime path."""


class ComposeOrchestrator:
    """Single entrypoint for POST /compose."""

    def __init__(
        self,
        source: RuntimeCache | LadybugStore,
        lm: EmbedClient,
        vector_store: VectorStore,
        telemetry: TelemetryWriter,
        *,
        embedding_model: str,
    ) -> None:
        self._source: RuntimeCache | LadybugStore = source
        self._lm = lm
        self._vector_store = vector_store
        self._telemetry = telemetry
        self._embedding_model = embedding_model

    @property
    def lm(self) -> EmbedClient:
        """Test hook: allows tests to monkeypatch the underlying embed client."""
        return self._lm

    @lm.setter
    def lm(self, client: EmbedClient) -> None:
        self._lm = client

    async def compose(self, req: ComposeRequest) -> ComposedResult | EmptyResult:
        start_ns = time.perf_counter_ns()
        retrieval, system = await asyncio.gather(
            self.retrieve(req),
            self.retrieve_system(req),
        )
        system_fragment_ids = [f.fragment_id for f in system.candidates]
        system_applied = bool(system.candidates)
        retrieval_error_code: str | None = None
        # Degraded = the dense leg didn't really run: an embedding failure
        # (500 / circuit-open) fell back to BM25, or the bounded query was empty.
        # Either way it's a silent quality drop unless we record it on the trace.
        dense_leg_degraded = (
            isinstance(retrieval, EmbeddingErrorResult) or retrieval.dense_leg_degraded
        )

        # Stage B (LM fragment re-rank) outcome — present only on a successful
        # RetrievalResult; the BM25-only/error fallback never runs Stage B.
        lm_assist_outcome = (
            retrieval.lm_assist_outcome if isinstance(retrieval, RetrievalResult) else "disabled"
        )
        from agentalloy.retrieval.lm_assist import load_config as _lm_config

        _lm_cfg = _lm_config()
        lm_assist_model = _lm_cfg.model if _lm_cfg.enabled else None

        if isinstance(retrieval, EmbeddingErrorResult):
            retrieval_error_code = retrieval.error.code.value
            logger.warning(
                "embedding failed (code=%s), using %s fallback",
                retrieval_error_code,
                "BM25-only" if retrieval.candidates else "empty-result",
            )

        if not retrieval.candidates:
            elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
            self._telemetry.write(
                TelemetryRecord(
                    composition_id=str(uuid.uuid4()),
                    timestamp=datetime.now(UTC),
                    phase=req.phase,
                    task_prompt=req.task,
                    result_type="compose_empty",
                    domain_fragment_ids=[],
                    system_fragment_ids=system_fragment_ids,
                    source_skill_ids=[],
                    latency_retrieval_ms=retrieval.retrieval_ms,
                    latency_total_ms=elapsed_ms,
                    error_payload=retrieval_error_code,
                    lm_assist_outcome=lm_assist_outcome,
                    lm_assist_model=lm_assist_model,
                    requesting_agent=req.requesting_agent,
                    dense_leg_degraded=dense_leg_degraded,
                )
            )
            return EmptyResult(
                task=req.task,
                phase=req.phase,
                system_fragments=system_fragment_ids,
                system_skills_applied=system_applied,
                recommended_max_tokens=DEFAULT_MAX_TOKENS_BY_PHASE[req.phase],
                dense_leg_degraded=dense_leg_degraded,
            )

        output = _format_fragments(system.candidates, retrieval.candidates)
        elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
        domain_fragment_ids = [f.fragment_id for f in retrieval.candidates]
        source_skills = list(dict.fromkeys(f.skill_id for f in retrieval.candidates))
        workflow_skill_ids = list(
            dict.fromkeys(f.skill_id for f in retrieval.candidates if f.skill_class == "workflow")
        )
        reranked = isinstance(retrieval, RetrievalResult) and retrieval.reranked

        # Token-savings telemetry.  Heuristic: len(text) // 4 (chars-to-tokens
        # approximation; no tokenizer dependency, same granularity as the
        # benchmark's token ratio metrics in eval/run_poc.py).
        tokens_returned = len(output) // 4
        # Flat-injection counterfactual: sum of raw_prose for every source skill.
        # Only available when the source is a RuntimeCache (version detail is in
        # memory).  With a bare LadybugStore we skip the counterfactual (0) to
        # avoid a per-request DB query.
        tokens_flat_equivalent = 0
        # ``_source`` may not be set on test doubles that bypass __init__.
        # Bind to a typed local so pyright can narrow through isinstance.
        _src = getattr(self, "_source", None)
        if isinstance(_src, RuntimeCache):
            for skill_id in source_skills:
                skill = _src.get_active_skill_by_id(skill_id)
                if skill is not None:
                    detail = _src.get_version_detail(skill.active_version_id)
                    if detail is not None:
                        tokens_flat_equivalent += len(detail.raw_prose) // 4

        self._telemetry.write(
            TelemetryRecord(
                composition_id=str(uuid.uuid4()),
                timestamp=datetime.now(UTC),
                phase=req.phase,
                task_prompt=req.task,
                result_type="compose",
                assembly_tier=ASSEMBLY_TIER,
                domain_fragment_ids=domain_fragment_ids,
                system_fragment_ids=system_fragment_ids,
                source_skill_ids=source_skills,
                latency_retrieval_ms=retrieval.retrieval_ms,
                latency_assembly_ms=0,
                latency_total_ms=elapsed_ms,
                error_payload=retrieval_error_code,
                workflow_skill_ids=workflow_skill_ids,
                reranked=reranked,
                tokens_returned=tokens_returned,
                tokens_flat_equivalent=tokens_flat_equivalent,
                lm_assist_outcome=lm_assist_outcome,
                lm_assist_model=lm_assist_model,
                requesting_agent=req.requesting_agent,
                dense_leg_degraded=dense_leg_degraded,
            )
        )
        return ComposedResult(
            task=req.task,
            phase=req.phase,
            output=output,
            domain_fragments=domain_fragment_ids,
            source_skills=source_skills,
            system_fragments=system_fragment_ids,
            system_skills_applied=system_applied,
            assembly_tier=ASSEMBLY_TIER,
            latency_ms=LatencyBreakdown(
                retrieval_ms=retrieval.retrieval_ms,
                assembly_ms=0,
                # Full wall-time (matches the telemetry record's latency_total_ms),
                # not retrieval-only — which excluded system retrieval + assembly.
                total_ms=elapsed_ms,
            ),
            recommended_max_tokens=DEFAULT_MAX_TOKENS_BY_PHASE[req.phase],
            dense_leg_degraded=dense_leg_degraded,
        )

    async def retrieve(self, req: ComposeRequest) -> RetrievalResult | EmbeddingErrorResult:
        try:
            return await asyncio.to_thread(
                retrieve_domain_candidates,
                self._source,
                self._lm,
                self._vector_store,
                task=req.task,
                phase=req.phase,
                domain_tags=req.domain_tags,
                k=req.resolved_k(),
                embedding_model=self._embedding_model,
                contract_tags=req.resolved_contract_tags,
            )
        except EmbeddingError as e:
            # EmbeddingError is raised by safe_embed() when the circuit breaker
            # is open or the embedding call fails. Return EmbeddingErrorResult
            # so compose() can handle it gracefully.
            return EmbeddingErrorResult(
                error=e,
                bm25_only=False,
            )
        except LMModelNotLoaded as e:
            raise RetrievalStageError("embedding_model_unavailable", str(e), available=None) from e
        except LMClientError as e:
            raise RetrievalStageError("embedding_failed", str(e), available=None) from e
        except Exception as e:
            logger.exception("retrieval stage unexpected failure")
            raise RetrievalStageError("store_unavailable", str(e), available=None) from e

    async def retrieve_system(self, req: ComposeRequest) -> SystemRetrievalResult:
        try:
            return await asyncio.to_thread(self._retrieve_system_sync, req.phase)
        except Exception as e:
            logger.exception("system retrieval stage unexpected failure")
            raise RetrievalStageError("store_unavailable", str(e), available=None) from e

    def _retrieve_system_sync(self, phase: str | None) -> SystemRetrievalResult:
        from agentalloy.applicability import filter_applicable_system_skills

        if isinstance(self._source, RuntimeCache):
            start_ns = time.perf_counter_ns()
            skills = self._source.get_active_skills(skill_class="system")
            applicable = filter_applicable_system_skills(skills, phase=phase, category=None)
            applicable_sorted = sorted(applicable, key=lambda s: s.skill_id)
            candidates: list[ActiveFragment] = []
            for skill in applicable_sorted:
                candidates.extend(self._source.get_active_fragments_for_skill(skill.skill_id))
            return SystemRetrievalResult(
                candidates=candidates,
                applied_skill_ids=[s.skill_id for s in applicable_sorted],
                retrieval_ms=int((time.perf_counter_ns() - start_ns) // 1_000_000),
            )
        return retrieve_system_fragments(self._source, phase=phase, category=None)


def _format_fragments(system: list[ActiveFragment], domain: list[ActiveFragment]) -> str:
    """Render fragments as a single string the inference model can consume.

    Groups by skill, separates with horizontal rules. Order: system fragments
    (governance/applicability) first, then domain fragments in retrieval order.
    """
    sections: list[str] = []
    if system:
        sections.append(_format_group("System fragments", system))
    if domain:
        sections.append(_format_group("Domain fragments", domain))
    return "\n\n".join(sections).strip()


def _format_group(title: str, fragments: list[ActiveFragment]) -> str:
    chunks: list[str] = [f"# {title}"]
    by_skill: dict[str, list[ActiveFragment]] = {}
    order: list[str] = []
    for f in fragments:
        if f.skill_id not in by_skill:
            order.append(f.skill_id)
            by_skill[f.skill_id] = []
        by_skill[f.skill_id].append(f)
    for skill_id in order:
        chunks.append(f"\n## skill: {skill_id}\n")
        for f in by_skill[skill_id]:
            chunks.append(f"### {f.fragment_type} — {f.fragment_id}\n")
            chunks.append(f.content.strip())
            chunks.append("")
    return "\n".join(chunks).strip()
