"""DuckDB-backed vector store for fragment embeddings + composition telemetry.

Single file per scope (``skills.duck``) holding both tables. Uses DuckDB's
built-in ``array_cosine_distance`` over ``FLOAT[EMBEDDING_DIM]`` columns — not
the experimental VSS extension. Linear scan is <10ms at current corpus scale.

L2-normalization is enforced at write time so ``array_cosine_distance``
reduces to an inner product at query time. Callers pass raw embeddings;
the store normalizes before insert.

BM25 full-text search is available via ``search_bm25``, which uses DuckDB's
native FTS extension over the ``prose`` column. The FTS index is built once
on first open via ``open_or_create``.

Public API:
    - ``open_or_create(path) -> VectorStore``
    - ``VectorStore.insert_embeddings(items)``
    - ``VectorStore.search_similar(query_vec, *, category=None, fragment_type=None, k=10)``
    - ``VectorStore.search_bm25(query, *, categories=None, k=10)``
    - ``VectorStore.record_composition_trace(trace)``
    - ``l2_normalize(vec) -> list[float]`` — shared helper

Schema and semantics track v5.3 Agentic Coding Architecture §2.4 / §2.5.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from agentalloy.storage.card_index import CARD_FRAGMENT_TYPE

EMBEDDING_DIM = 768
"""Vector dimensionality. Tied to ``nomic-embed-text-v1.5`` (768-dim default).
Changing the model requires a schema migration and full corpus reindex —
DuckDB's ``FLOAT[768]`` column type is dimension-fixed."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FragmentEmbedding:
    """A fragment's embedding vector plus the denormalized columns that make
    filtered vector search cheap (no cross-engine join on the hot path)."""

    fragment_id: str
    embedding: Sequence[float]  # raw; normalized on insert
    skill_id: str
    category: str
    fragment_type: str
    embedded_at: int  # unix epoch seconds
    embedding_model: str
    prose: str = ""  # raw fragment text; indexed for BM25
    # Authored phase eligibility (Skill.phase_scope, SDD lifecycle vocabulary —
    # e.g. build/design/qa). None = no authored scope.
    phase_scope: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SimilarityHit:
    fragment_id: str
    skill_id: str
    distance: float  # cosine distance in [0, 2]; 0 = identical direction


@dataclass(frozen=True)
class BM25Hit:
    fragment_id: str
    score: float  # BM25 score; higher = more relevant


@dataclass(frozen=True)
class CompositionTrace:
    """One row in ``composition_traces``. Optional fields carry None into the
    DB column as SQL NULL. Schema per summary doc §2.4.2."""

    trace_id: str
    request_ts: int
    phase: str
    task_prompt: str
    status: str
    correlation_id: str | None = None
    category: str | None = None
    selected_fragment_ids: list[str] = field(default_factory=lambda: [])
    source_skill_ids: list[str] = field(default_factory=lambda: [])
    system_skill_ids: list[str] = field(default_factory=lambda: [])
    assembly_tier: str | None = None
    assembly_model: str | None = None
    retrieval_latency_ms: int | None = None
    assembly_latency_ms: int | None = None
    total_latency_ms: int | None = None
    error_code: str | None = None
    response_size_chars: int | None = None
    prompt_version: str | None = None
    workflow_skill_ids: list[str] = field(default_factory=lambda: [])
    contract_path: str | None = None
    contract_tags: list[str] = field(default_factory=lambda: [])
    bm25_source: str = "rule-extracted"  # "rule-extracted" | "contract" | "union"
    # Signal-layer fields
    event_type: str = "compose"  # "compose" | "phase_eval" | "phase_transition" | "system_skill_applied" | "contract_retrieval"
    pre_filter_matched: str | None = None
    gates_met: list[str] = field(default_factory=lambda: list[str]())
    gates_unmet: list[str] = field(default_factory=lambda: list[str]())
    qwen_calls: int = 0
    # True when a cross-encoder rerank reordered the candidate pool (Stage A).
    reranked: bool = False
    # Token-savings telemetry: estimated tokens in the composed output and in
    # the flat-injection counterfactual (all source-skill raw_prose concatenated).
    # Both use the len(text) // 4 heuristic (no tokenizer dependency).
    tokens_returned: int = 0
    tokens_flat_equivalent: int = 0
    # Stage B (LM fragment re-rank) outcome: "disabled" | "hit" | "timeout" | "error".
    lm_assist_outcome: str = "disabled"
    # The LM_ASSIST_MODEL tag in effect for this composition (telemetry only).
    lm_assist_model: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def l2_normalize(vec: Sequence[float]) -> list[float]:
    """Return the L2-normalized form of ``vec`` (unit Euclidean norm).

    Raises ``ValueError`` if ``vec`` is the zero vector (no defined direction).
    """
    norm_sq = sum(x * x for x in vec)
    if norm_sq == 0.0:
        raise ValueError("cannot L2-normalize the zero vector")
    norm = math.sqrt(norm_sq)
    return [x / norm for x in vec]


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------


_SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS fragment_embeddings (
    fragment_id VARCHAR PRIMARY KEY,
    embedding FLOAT[{EMBEDDING_DIM}] NOT NULL,
    skill_id VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    fragment_type VARCHAR NOT NULL,
    embedded_at BIGINT NOT NULL,
    embedding_model VARCHAR NOT NULL,
    prose VARCHAR NOT NULL DEFAULT '',
    phase_scope VARCHAR[]
);

CREATE INDEX IF NOT EXISTS idx_frag_emb_skill ON fragment_embeddings(skill_id);
CREATE INDEX IF NOT EXISTS idx_frag_emb_category ON fragment_embeddings(category);
CREATE INDEX IF NOT EXISTS idx_frag_emb_type ON fragment_embeddings(fragment_type);

CREATE TABLE IF NOT EXISTS composition_traces (
    trace_id VARCHAR PRIMARY KEY,
    correlation_id VARCHAR,
    request_ts BIGINT NOT NULL,
    phase VARCHAR NOT NULL,
    category VARCHAR,
    task_prompt VARCHAR NOT NULL,
    selected_fragment_ids VARCHAR[],
    source_skill_ids VARCHAR[],
    system_skill_ids VARCHAR[],
    assembly_tier VARCHAR,
    assembly_model VARCHAR,
    retrieval_latency_ms INTEGER,
    assembly_latency_ms INTEGER,
    total_latency_ms INTEGER,
    status VARCHAR NOT NULL,
    error_code VARCHAR,
    response_size_chars INTEGER,
    prompt_version VARCHAR,
    workflow_skill_ids VARCHAR[],
    event_type VARCHAR NOT NULL DEFAULT 'compose',
    pre_filter_matched VARCHAR,
    gates_met VARCHAR[],
    gates_unmet VARCHAR[],
    qwen_calls INTEGER NOT NULL DEFAULT 0,
    contract_path VARCHAR,
    contract_tags VARCHAR[],
    bm25_source VARCHAR NOT NULL DEFAULT 'rule-extracted',
    reranked BOOLEAN NOT NULL DEFAULT FALSE,
    tokens_returned INTEGER NOT NULL DEFAULT 0,
    tokens_flat_equivalent INTEGER NOT NULL DEFAULT 0,
    lm_assist_outcome VARCHAR NOT NULL DEFAULT 'disabled',
    lm_assist_model VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_traces_ts ON composition_traces(request_ts);
CREATE INDEX IF NOT EXISTS idx_traces_phase ON composition_traces(phase);
CREATE INDEX IF NOT EXISTS idx_traces_status ON composition_traces(status);

CREATE TABLE IF NOT EXISTS prompt_loads (
    ts BIGINT NOT NULL,
    prompt_name VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    trace_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_prompt_loads_ts ON prompt_loads(ts);

-- Stage 0: auditable key/value record of how the corpus index was built.
-- The re-embed pass writes ``card_index`` here so the indexed representation
-- (plain / prefix / cards / both) is recoverable from the corpus alone.
CREATE TABLE IF NOT EXISTS corpus_meta (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL,
    updated_at BIGINT NOT NULL
);
"""

_FTS_SETUP_SQL = """
INSTALL fts;
LOAD fts;
"""

_FTS_INDEX_EXISTS_SQL = """
SELECT COUNT(*) FROM information_schema.tables
WHERE table_name = 'fts_main_fragment_embeddings_config'
"""

_FTS_CREATE_SQL = "PRAGMA create_fts_index('fragment_embeddings', 'fragment_id', 'prose');"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _trace_where(
    *,
    phase: str | None,
    status: str | None,
    since: int | None,
    until: int | None,
) -> tuple[str, list[object]]:
    """Build a parameterised WHERE clause for composition_traces queries."""
    clauses: list[str] = []
    params: list[object] = []
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if since is not None:
        clauses.append("request_ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("request_ts <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


class VectorStoreError(Exception):
    """Base for vector-store errors."""


class EmbeddingDimMismatch(VectorStoreError):
    """Raised when an embedding's length doesn't match ``EMBEDDING_DIM``."""


class VectorStore:
    """Thin wrapper over a DuckDB connection with the Skill API's schema.

    Not thread-safe — use one connection per process. DuckDB allows multiple
    reader processes against the same file but writer is exclusive.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- transactions --------------------------------------------------------

    def begin_transaction(self) -> None:
        """Begin a DuckDB transaction for batch operations."""
        self._conn.execute("BEGIN TRANSACTION")

    def commit_transaction(self) -> None:
        """Commit the current transaction."""
        self._conn.execute("COMMIT")

    def rollback_transaction(self) -> None:
        """Rollback the current transaction, undoing all writes since begin_transaction."""
        self._conn.execute("ROLLBACK")

    # -- embeddings ----------------------------------------------------------

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int:
        """Batch insert. Normalizes at write time. Returns count inserted.

        Upsert semantics: ``fragment_id`` is the primary key, so re-inserting
        an existing id raises a DuckDB constraint error. Use ``delete_skill``
        before re-inserting if replacing a skill's fragments.
        """
        batch = list(items)
        if not batch:
            return 0
        for f in batch:
            if len(f.embedding) != EMBEDDING_DIM:
                raise EmbeddingDimMismatch(
                    f"fragment_id={f.fragment_id}: embedding has {len(f.embedding)} "
                    f"dimensions, expected {EMBEDDING_DIM}"
                )
        rows = [
            (
                f.fragment_id,
                l2_normalize(f.embedding),
                f.skill_id,
                f.category,
                f.fragment_type,
                f.embedded_at,
                f.embedding_model,
                f.prose,
                list(f.phase_scope) if f.phase_scope else None,
            )
            for f in batch
        ]
        self._conn.executemany(
            """
            INSERT INTO fragment_embeddings
                (fragment_id, embedding, skill_id, category, fragment_type,
                 embedded_at, embedding_model, prose, phase_scope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def backfill_phase_scope(self, scope_by_skill: dict[str, list[str] | None]) -> int:
        """Set ``phase_scope`` for every fragment of each skill. Returns rows updated.

        Used by ``python -m agentalloy.migrate`` to populate the column on
        corpora built before it existed. Embeddings are untouched.
        """
        updated = 0
        for skill_id, scope in scope_by_skill.items():
            cur = self._conn.execute(
                "UPDATE fragment_embeddings SET phase_scope = ? WHERE skill_id = ?",
                [scope if scope else None, skill_id],
            )
            row = cur.fetchone()
            updated += int(row[0]) if row else 0
        return updated

    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        fragment_types: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        k: int = 10,
    ) -> list[SimilarityHit]:
        """Top-k cosine distance, with optional denormalized-column filters.

        ``query_vec`` is L2-normalized internally before comparison so cosine
        distance reduces to inner product regardless of what the caller passes.

        ``deprecated_skill_ids`` excludes fragments belonging to deprecated
        skills from the result set. The Cypher-path reads (active.py) already
        filter ``deprecated = false``; this parameter closes the same gap for
        the DuckDB vector leg so RRF fusion does not surface deprecated content.
        """
        if len(query_vec) != EMBEDDING_DIM:
            raise EmbeddingDimMismatch(
                f"query vector has {len(query_vec)} dimensions, expected {EMBEDDING_DIM}"
            )
        q = l2_normalize(query_vec)

        where_clauses: list[str] = []
        params: list[object] = [q]
        if categories and phases:
            # Union eligibility: the category map OR the skill's authored
            # phase_scope may admit a fragment. NULL phase_scope falls back
            # to the category map alone.
            where_clauses.append(
                "(category = ANY(?) OR (phase_scope IS NOT NULL AND list_has_any(phase_scope, ?)))"
            )
            params.append(categories)
            params.append(phases)
        elif categories:
            where_clauses.append("category = ANY(?)")
            params.append(categories)
        elif phases:
            where_clauses.append("(phase_scope IS NOT NULL AND list_has_any(phase_scope, ?))")
            params.append(phases)
        if fragment_types:
            where_clauses.append("fragment_type = ANY(?)")
            params.append(fragment_types)
        if deprecated_skill_ids:
            where_clauses.append("skill_id != ALL(?)")
            params.append(deprecated_skill_ids)
        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(k)

        sql = f"""
            SELECT
                fragment_id,
                skill_id,
                array_cosine_distance(
                    embedding,
                    CAST(? AS FLOAT[{EMBEDDING_DIM}])
                ) AS distance
            FROM fragment_embeddings
            {where}
            ORDER BY distance
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SimilarityHit(
                fragment_id=str(row[0]),
                skill_id=str(row[1]),
                distance=float(row[2]),
            )
            for row in rows
        ]

    def search_bm25(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        k: int = 10,
    ) -> list[BM25Hit]:
        """BM25 full-text search over the prose column.

        Returns up to ``k`` results ordered by descending BM25 score.
        Only fragments with a non-null score (i.e. at least one query token
        matched) are returned. Returns empty list if the FTS index is not
        available or query is empty.

        ``deprecated_skill_ids`` excludes fragments from deprecated skills,
        mirroring the Cypher-path filter in ``reads/active.py``.
        """
        if not query.strip():
            return []

        try:
            where_clauses: list[str] = ["score IS NOT NULL"]
            params: list[object] = [query]
            if categories and phases:
                where_clauses.append(
                    "(category = ANY(?) "
                    "OR (phase_scope IS NOT NULL AND list_has_any(phase_scope, ?)))"
                )
                params.append(categories)
                params.append(phases)
            elif categories:
                where_clauses.append("category = ANY(?)")
                params.append(categories)
            elif phases:
                where_clauses.append("(phase_scope IS NOT NULL AND list_has_any(phase_scope, ?))")
                params.append(phases)
            if deprecated_skill_ids:
                where_clauses.append("skill_id != ALL(?)")
                params.append(deprecated_skill_ids)
            params.append(k)

            where = " AND ".join(where_clauses)
            sql = f"""
                SELECT score, fragment_id FROM (
                    SELECT *,
                        fts_main_fragment_embeddings.match_bm25(
                            fragment_id, ?, fields := 'prose'
                        ) AS score
                    FROM fragment_embeddings
                )
                WHERE {where}
                ORDER BY score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 — FTS unavailable or index not built
            return []

        return [BM25Hit(fragment_id=str(row[1]), score=float(row[0])) for row in rows]

    def rebuild_fts_index(self) -> None:
        """Rebuild the FTS index on the prose column.

        Workaround for a DuckDB FTS extension bug (present in 1.5.2 and
        1.5.3): drop_fts_index deletes the stopwords catalog entry, and
        create_fts_index then fails with "subject stopwords has been
        deleted" during commit.

        Fix: drop the entire FTS schema CASCADE (which clears catalog
        entries cleanly), then call create_fts_index fresh.

        Callers should still treat a final failure as non-fatal: vector
        search keeps working; the BM25 leg silently returns empty until
        the next successful rebuild.
        """
        import contextlib

        self._conn.execute("CHECKPOINT;")
        with contextlib.suppress(Exception):
            self._conn.execute("DROP SCHEMA IF EXISTS fts_main_fragment_embeddings CASCADE")
        with contextlib.suppress(Exception):
            self._conn.execute(_FTS_CREATE_SQL)

    def count_embeddings(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM fragment_embeddings").fetchone()
        return int(row[0]) if row else 0

    # -- corpus metadata -----------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a corpus_meta key/value pair (with an updated_at stamp).

        Used to record the Stage 0 ``card_index`` mode so the corpus's indexed
        representation is auditable without re-deriving it from the rows.
        """
        import time as _time

        self._conn.execute(
            """
            INSERT INTO corpus_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                            updated_at = excluded.updated_at
            """,
            [key, value, int(_time.time())],
        )

    def get_meta(self, key: str) -> str | None:
        """Return the corpus_meta value for ``key``, or None if unset/absent.

        Soft-fails to None when the table is missing (corpus predates Stage 0).
        """
        try:
            row = self._conn.execute(
                "SELECT value FROM corpus_meta WHERE key = ?", [key]
            ).fetchone()
        except Exception:  # noqa: BLE001 — table absent on pre-Stage-0 corpora
            return None
        return str(row[0]) if row else None

    def count_cards(self) -> int:
        """Number of synthetic card documents currently in the index."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM fragment_embeddings WHERE fragment_type = ?",
            [CARD_FRAGMENT_TYPE],
        ).fetchone()
        return int(row[0]) if row else 0

    def delete_cards(self, skill_id: str | None = None) -> int:
        """Remove synthetic card documents. Returns rows deleted.

        ``skill_id=None`` drops every card (full-scope rebuild). Passing a
        ``skill_id`` scopes the delete to that one skill's card — required when
        re-embedding a single skill (``--skill-id``), so other skills' cards
        survive a scoped pass instead of being wiped and never reinserted.
        """
        if skill_id is None:
            cur = self._conn.execute(
                "DELETE FROM fragment_embeddings WHERE fragment_type = ?", [CARD_FRAGMENT_TYPE]
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM fragment_embeddings WHERE fragment_type = ? AND skill_id = ?",
                [CARD_FRAGMENT_TYPE, skill_id],
            )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def embedding_dim(self) -> int | None:
        """Return the dimension of stored embeddings, or None if the corpus is empty.

        Used by install-pack to hard-block dim-mismatched packs before they
        corrupt the vector store. Reads one non-null embedding and measures
        its length — DuckDB doesn't enforce a fixed length on FLOAT[] columns,
        so the corpus's "dim" is whatever was first written.
        """
        row = self._conn.execute(
            "SELECT len(embedding) FROM fragment_embeddings WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def fragment_ids_present(self, fragment_ids: Sequence[str]) -> set[str]:
        """Return the subset of ``fragment_ids`` that already have embeddings.
        Useful for idempotent re-embed runs (skip what's already done)."""
        if not fragment_ids:
            return set()
        rows = self._conn.execute(
            "SELECT fragment_id FROM fragment_embeddings WHERE fragment_id = ANY(?)",
            [list(fragment_ids)],
        ).fetchall()
        return {str(row[0]) for row in rows}

    def delete_skill(self, skill_id: str) -> int:
        """Remove all fragment embeddings for a skill. Returns rows deleted."""
        cur = self._conn.execute("DELETE FROM fragment_embeddings WHERE skill_id = ?", [skill_id])
        row = cur.fetchone()
        return int(row[0]) if row else 0

    # -- telemetry -----------------------------------------------------------

    def record_composition_trace(self, trace: CompositionTrace) -> None:
        """Insert a composition trace row. Callers should wrap in try/except
        so telemetry failures never propagate to the caller of /compose."""
        self._conn.execute(
            """
            INSERT INTO composition_traces (
                trace_id, correlation_id, request_ts, phase, category,
                task_prompt, selected_fragment_ids, source_skill_ids,
                system_skill_ids, assembly_tier, assembly_model,
                retrieval_latency_ms, assembly_latency_ms, total_latency_ms,
                status, error_code, response_size_chars,
                prompt_version, workflow_skill_ids,
                event_type, pre_filter_matched, gates_met, gates_unmet, qwen_calls,
                contract_path, contract_tags, bm25_source, reranked,
                tokens_returned, tokens_flat_equivalent,
                lm_assist_outcome, lm_assist_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trace.trace_id,
                trace.correlation_id,
                trace.request_ts,
                trace.phase,
                trace.category,
                trace.task_prompt,
                trace.selected_fragment_ids,
                trace.source_skill_ids,
                trace.system_skill_ids,
                trace.assembly_tier,
                trace.assembly_model,
                trace.retrieval_latency_ms,
                trace.assembly_latency_ms,
                trace.total_latency_ms,
                trace.status,
                trace.error_code,
                trace.response_size_chars,
                trace.prompt_version,
                trace.workflow_skill_ids,
                trace.event_type,
                trace.pre_filter_matched,
                trace.gates_met,
                trace.gates_unmet,
                trace.qwen_calls,
                trace.contract_path,
                trace.contract_tags,
                trace.bm25_source,
                trace.reranked,
                trace.tokens_returned,
                trace.tokens_flat_equivalent,
                trace.lm_assist_outcome,
                trace.lm_assist_model,
            ],
        )

    def count_traces(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM composition_traces").fetchone()
        return int(row[0]) if row else 0

    def query_traces(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CompositionTrace]:
        """Return composition traces newest-first with optional filters."""
        where, params = _trace_where(phase=phase, status=status, since=since, until=until)
        sql = f"""
            SELECT trace_id, correlation_id, request_ts, phase, category,
                   task_prompt, selected_fragment_ids, source_skill_ids,
                   system_skill_ids, assembly_tier, assembly_model,
                   retrieval_latency_ms, assembly_latency_ms, total_latency_ms,
                   status, error_code, response_size_chars, prompt_version,
                   workflow_skill_ids, event_type, pre_filter_matched,
                   gates_met, gates_unmet, qwen_calls,
                   contract_path, contract_tags, bm25_source, reranked,
                   tokens_returned, tokens_flat_equivalent,
                   lm_assist_outcome, lm_assist_model
            FROM composition_traces
            {where}
            ORDER BY request_ts DESC
            LIMIT ? OFFSET ?
        """
        rows = self._conn.execute(sql, params + [limit, offset]).fetchall()
        return [
            CompositionTrace(
                trace_id=str(r[0]),
                correlation_id=r[1],
                request_ts=int(r[2]),
                phase=str(r[3]),
                category=r[4],
                task_prompt=str(r[5]),
                selected_fragment_ids=list(r[6] or []),
                source_skill_ids=list(r[7] or []),
                system_skill_ids=list(r[8] or []),
                assembly_tier=r[9],
                assembly_model=r[10],
                retrieval_latency_ms=r[11],
                assembly_latency_ms=r[12],
                total_latency_ms=r[13],
                status=str(r[14]),
                error_code=r[15],
                response_size_chars=r[16],
                prompt_version=r[17],
                workflow_skill_ids=list(r[18] or []),
                event_type=r[19] or "compose",
                pre_filter_matched=r[20],
                gates_met=list(r[21] or []),
                gates_unmet=list(r[22] or []),
                qwen_calls=int(r[23]) if r[23] else 0,
                contract_path=r[24],
                contract_tags=list(r[25] or []),
                bm25_source=r[26] or "rule-extracted",
                reranked=bool(r[27]),
                tokens_returned=int(r[28]) if r[28] is not None else 0,
                tokens_flat_equivalent=int(r[29]) if r[29] is not None else 0,
                lm_assist_outcome=str(r[30]) if r[30] is not None else "disabled",
                lm_assist_model=r[31],
            )
            for r in rows
        ]

    def count_traces_filtered(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> int:
        where, params = _trace_where(phase=phase, status=status, since=since, until=until)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM composition_traces {where}", params
        ).fetchone()
        return int(row[0]) if row else 0

    def aggregate_savings(self) -> dict[str, object]:
        """Aggregate token-savings telemetry across all compose traces.

        Returns a dict with overall totals and a per-phase breakdown.
        Rows with tokens_flat_equivalent == 0 are excluded from the savings %
        calculation to avoid division-by-zero on legacy/empty rows.
        """
        overall = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total_composes,
                COALESCE(SUM(tokens_returned), 0) AS sum_returned,
                COALESCE(SUM(tokens_flat_equivalent), 0) AS sum_flat
            FROM composition_traces
            WHERE status = 'compose'
            """
        ).fetchone()
        total_composes = int(overall[0]) if overall else 0
        sum_returned = int(overall[1]) if overall else 0
        sum_flat = int(overall[2]) if overall else 0
        tokens_saved = max(0, sum_flat - sum_returned)
        savings_pct = round(tokens_saved / sum_flat * 100, 1) if sum_flat > 0 else 0.0

        phase_rows = self._conn.execute(
            """
            SELECT
                phase,
                COUNT(*) AS composes,
                COALESCE(SUM(tokens_returned), 0) AS returned,
                COALESCE(SUM(tokens_flat_equivalent), 0) AS flat
            FROM composition_traces
            WHERE status = 'compose'
            GROUP BY phase
            ORDER BY composes DESC
            """
        ).fetchall()
        per_phase: list[dict[str, object]] = []
        for row in phase_rows:
            ph_flat = int(row[3])
            ph_returned = int(row[2])
            ph_saved = max(0, ph_flat - ph_returned)
            ph_pct = round(ph_saved / ph_flat * 100, 1) if ph_flat > 0 else 0.0
            per_phase.append(
                {
                    "phase": str(row[0]),
                    "composes": int(row[1]),
                    "tokens_returned": ph_returned,
                    "tokens_flat_equivalent": ph_flat,
                    "tokens_saved": ph_saved,
                    "savings_pct": ph_pct,
                }
            )

        return {
            "total_composes": total_composes,
            "tokens_returned": sum_returned,
            "tokens_flat_equivalent": sum_flat,
            "tokens_saved": tokens_saved,
            "savings_pct": savings_pct,
            "per_phase": per_phase,
        }

    def clear_telemetry(self) -> dict[str, int]:
        """Delete all rows from composition_traces and prompt_loads.

        Does NOT touch fragment_embeddings (the corpus).
        Returns counts of deleted rows.
        """
        traces = self.count_traces()
        self._conn.execute("DELETE FROM composition_traces")
        loads_row = self._conn.execute("SELECT COUNT(*) FROM prompt_loads").fetchone()
        loads = int(loads_row[0]) if loads_row else 0
        self._conn.execute("DELETE FROM prompt_loads")
        return {"traces_deleted": traces, "prompt_loads_deleted": loads}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _fts_index_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    row = conn.execute(_FTS_INDEX_EXISTS_SQL).fetchone()
    return bool(row and row[0] > 0)


# Columns added to ``composition_traces`` after the initial DDL shipped.
# Listed as (column_name, type, default_clause) — default_clause is appended
# verbatim after the type if non-empty. New columns added here are picked up
# by existing installs on next ``open_or_create()``.
_COMPOSITION_TRACES_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("event_type", "VARCHAR", "DEFAULT 'compose'"),
    ("pre_filter_matched", "VARCHAR", ""),
    ("gates_met", "VARCHAR[]", ""),
    ("gates_unmet", "VARCHAR[]", ""),
    ("qwen_calls", "INTEGER", "DEFAULT 0"),
    ("contract_path", "VARCHAR", ""),
    ("contract_tags", "VARCHAR[]", ""),
    ("bm25_source", "VARCHAR", "DEFAULT 'rule-extracted'"),
    ("reranked", "BOOLEAN", "DEFAULT FALSE"),
    ("tokens_returned", "INTEGER", "DEFAULT 0"),
    ("tokens_flat_equivalent", "INTEGER", "DEFAULT 0"),
    ("lm_assist_outcome", "VARCHAR", "DEFAULT 'disabled'"),
    ("lm_assist_model", "VARCHAR", ""),
)


# Same pattern for ``fragment_embeddings``: additive columns picked up by
# existing installs on next open. phase_scope is backfilled from the graph
# by ``python -m agentalloy.migrate`` (NULL rows fall back to the category map).
_FRAGMENT_EMBEDDINGS_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("phase_scope", "VARCHAR[]", ""),
)


def _apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply additive schema migrations.

    Existing installs predate columns added in later phases. ``CREATE TABLE
    IF NOT EXISTS`` does not back-fill missing columns, so each subsequent
    INSERT would raise. This walks the live schema and issues ``ALTER TABLE
    ADD COLUMN`` for any missing column. Idempotent and soft-fail.
    """
    import contextlib

    try:
        frag_rows = conn.execute("PRAGMA table_info('fragment_embeddings')").fetchall()
        frag_existing = {str(row[1]) for row in frag_rows}
        for col, col_type, default_clause in _FRAGMENT_EMBEDDINGS_MIGRATIONS:
            if col in frag_existing:
                continue
            stmt = f"ALTER TABLE fragment_embeddings ADD COLUMN {col} {col_type}"
            if default_clause:
                stmt = f"{stmt} {default_clause}"
            with contextlib.suppress(Exception):
                conn.execute(stmt)
    except Exception:
        pass

    try:
        rows = conn.execute("PRAGMA table_info('composition_traces')").fetchall()
    except Exception:
        return
    existing = {str(row[1]) for row in rows}

    for col, col_type, default_clause in _COMPOSITION_TRACES_MIGRATIONS:
        if col in existing:
            continue
        stmt = f"ALTER TABLE composition_traces ADD COLUMN {col} {col_type}"
        if default_clause:
            stmt = f"{stmt} {default_clause}"
        with contextlib.suppress(Exception):
            conn.execute(stmt)


def open_or_create(path: str | Path) -> VectorStore:
    """Open (or create) the DuckDB vector store at ``path``.

    Creates parent directories if missing. Idempotent: applies schema DDL on
    every open, then runs additive migrations so existing installs pick up
    columns added in later phases. Builds the BM25 FTS index on first open
    (or when missing). Use as a context manager to guarantee connection close.

    T7B: Startup dimension guard — raises ``EmbeddingDimMismatch`` if an
    existing corpus was built at a different dimension than ``EMBEDDING_DIM``.
    Fail-fast here prevents silent mid-request crashes when a user upgrades
    their embedding model (e.g. qwen3-embedding 1024-dim → nomic-embed-text-v1.5 768-dim).
    """
    assert isinstance(path, (str, Path)), "path must be str or Path"  # P10-R5
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p))
    # D2: construct vs immediately so any failure below can release the DuckDB
    # file lock before the exception propagates (callers may catch and reopen).
    vs = VectorStore(conn)
    try:
        conn.execute(_SCHEMA_DDL)
        _apply_migrations(conn)

        try:
            conn.execute(_FTS_SETUP_SQL)
            if not _fts_index_exists(conn):
                conn.execute(_FTS_CREATE_SQL)
        except Exception:  # noqa: BLE001 — FTS extension unavailable; BM25 leg silently degrades
            pass

        stored_dim = vs.embedding_dim()  # int | None — None means corpus is empty
        assert stored_dim is None or stored_dim > 0, (
            "stored embedding dim must be positive"
        )  # P10-R5
        if stored_dim is not None and stored_dim != EMBEDDING_DIM:
            raise EmbeddingDimMismatch(
                f"Corpus was built with {stored_dim}-dim embeddings but the runtime "
                f"expects {EMBEDDING_DIM}-dim (nomic-embed-text-v1.5). "
                f"Run `agentalloy reembed --force` to rebuild with the correct model. "
                f"WARNING: --force deletes all existing embeddings; re-run install-packs afterward."
            )
    except BaseException:
        vs.close()  # release DuckDB file lock before raising — callers may catch and reopen
        raise
    return vs


def append_trace(db_path: Path, trace: CompositionTrace) -> None:
    """Convenience: open the store at db_path, insert trace, close. Soft-fail.

    D3: re-raises ``EmbeddingDimMismatch`` before the broad soft-fail catch —
    a corpus dimension mismatch is a hard configuration error that must surface,
    not be silently swallowed in the telemetry path.
    """
    try:
        with open_or_create(db_path) as store:
            store.record_composition_trace(trace)
    except EmbeddingDimMismatch:
        raise  # D3: hard corpus error — propagate, do not soft-fail
    except Exception:
        pass
