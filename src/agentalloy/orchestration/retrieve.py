"""RetrieveOrchestrator — direct retrieve without assembly (NXS-769)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

from agentalloy.api.compose_models import Phase
from agentalloy.api.retrieve_models import (
    ActiveVersionMeta,
    RetrieveByIdResponse,
    RetrieveQueryHit,
    RetrieveQueryResponse,
)
from agentalloy.embed_provider import EmbedClient
from agentalloy.lm_client import (
    LMClientError,
    LMModelNotLoaded,
)
from agentalloy.orchestration.compose import RetrievalStageError
from agentalloy.reads import get_active_version_by_id
from agentalloy.reads.models import ActiveSkill
from agentalloy.retrieval.domain import retrieve_domain_candidates
from agentalloy.retrieval.embedding_errors import EmbeddingErrorResult
from agentalloy.runtime_state import RuntimeCache, VersionDetail
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore
from agentalloy.telemetry import TelemetryRecord, TelemetryWriter


class RetrieveOrchestrator:
    """Serves both by-id and semantic-query retrieve modes. No assembly."""

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

    async def by_id(self, skill_id: str) -> RetrieveByIdResponse | None:
        start_ns = time.perf_counter_ns()
        skill = await asyncio.to_thread(self._get_skill_by_id, skill_id)
        if skill is None:
            return None

        version_meta, raw_prose = await asyncio.to_thread(
            self._fetch_version_meta_and_prose, skill.active_version_id
        )
        response = RetrieveByIdResponse(
            skill_id=skill.skill_id,
            canonical_name=skill.canonical_name,
            category=skill.category,
            skill_class=skill.skill_class,
            active_version=version_meta,
            raw_prose=raw_prose,
        )
        elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
        self._telemetry.write(
            TelemetryRecord(
                composition_id=str(uuid.uuid4()),
                timestamp=datetime.now(UTC),
                phase=None,
                task_prompt="",
                result_type="retrieve_by_id",
                source_skill_ids=[skill.skill_id],
                latency_retrieval_ms=elapsed_ms,
                latency_total_ms=elapsed_ms,
            )
        )
        return response

    async def by_query(
        self,
        *,
        task: str,
        phase: Phase,
        domain_tags: list[str] | None,
        k: int,
    ) -> RetrieveQueryResponse:
        start_ns = time.perf_counter_ns()

        try:
            result = await asyncio.to_thread(
                retrieve_domain_candidates,
                self._source,
                self._lm,
                self._vector_store,
                task=task,
                phase=phase,
                domain_tags=domain_tags,
                k=k * 2,  # overfetch so dedup-to-skills has room to produce k
                embedding_model=self._embedding_model,
            )
        except LMModelNotLoaded as e:
            raise RetrievalStageError("embedding_model_unavailable", str(e), available=None) from e
        except LMClientError as e:
            raise RetrievalStageError("embedding_failed", str(e), available=None) from e
        except Exception as e:
            raise RetrievalStageError("store_unavailable", str(e), available=None) from e
        error_payload = result.error_code if isinstance(result, EmbeddingErrorResult) else None

        # Dedup fragments to best-scoring-per-skill. Scores already computed
        # during retrieval (see ``RetrievalResult.scores_by_id``).
        per_skill: dict[str, tuple[str, float]] = {}
        for f in result.candidates:
            score = result.scores_by_id.get(f.fragment_id, 0.0)
            prev = per_skill.get(f.skill_id)
            if prev is None or score > prev[1]:
                per_skill[f.skill_id] = (f.version_id, score)

        # Fetch raw prose + canonical_name per skill; cap to k.
        top_skills = sorted(per_skill.items(), key=lambda kv: kv[1][1], reverse=True)[:k]
        hits: list[RetrieveQueryHit] = []
        for skill_id, (version_id, score) in top_skills:
            _, raw_prose = await asyncio.to_thread(self._fetch_version_meta_and_prose, version_id)
            skill = await asyncio.to_thread(self._get_skill_by_id, skill_id)
            if skill is None:
                continue
            hits.append(
                RetrieveQueryHit(
                    skill_id=skill_id,
                    version_id=version_id,
                    canonical_name=skill.canonical_name,
                    raw_prose=raw_prose,
                    score=score,
                )
            )

        elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
        self._telemetry.write(
            TelemetryRecord(
                composition_id=str(uuid.uuid4()),
                timestamp=datetime.now(UTC),
                phase=phase,
                task_prompt=task,
                result_type="retrieve_query",
                source_skill_ids=[h.skill_id for h in hits],
                latency_retrieval_ms=elapsed_ms,
                latency_total_ms=elapsed_ms,
                error_payload=error_payload,
            )
        )
        return RetrieveQueryResponse(results=hits)

    # ---- private helpers ----

    def _get_skill_by_id(self, skill_id: str) -> ActiveSkill | None:
        """Dispatch to RuntimeCache or store-backed reader."""
        if isinstance(self._source, RuntimeCache):
            return self._source.get_active_skill_by_id(skill_id)
        from agentalloy.reads import get_active_skill_by_id

        return get_active_skill_by_id(self._source, skill_id)

    def _fetch_version_meta_and_prose(self, version_id: str) -> tuple[ActiveVersionMeta, str]:
        if isinstance(self._source, RuntimeCache):
            detail: VersionDetail | None = self._source.get_version_detail(version_id)
            if detail is None:
                raise RuntimeError(f"version {version_id!r} not found in runtime cache")
            meta = ActiveVersionMeta(
                version_id=detail.version_id,
                version_number=detail.version_number,
                authored_at=detail.authored_at,
                author=detail.author,
                change_summary=detail.change_summary,
            )
            return meta, detail.raw_prose

        data = get_active_version_by_id(self._source, version_id)
        meta = ActiveVersionMeta(
            version_id=data["version_id"],
            version_number=data["version_number"],
            authored_at=data["authored_at"],
            author=data["author"],
            change_summary=data["change_summary"],
        )
        return meta, data["raw_prose"]
