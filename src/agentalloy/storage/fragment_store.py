"""LanceDB-backed fragment store: vector ANN (retrieval) + exact cosine (dedup)
+ native BM25 (Tantivy).

Replaces the DuckDB-vss linear scan and the DuckDB-fts BM25 (with its
``DROP SCHEMA ... CASCADE`` workaround). The dataset is a *derived index*
rebuilt from the SQL-canonical source (``agentalloy.duck``); the denormalized
filter columns therefore cannot drift (decision D8).

Concurrency / zero-downtime (decisions D3, D4): Lance is MVCC and file-based —
no exclusive writer lock. The serving process and a ``reembed`` process can both
open the dataset at once. A full rebuild is an atomic table *overwrite* (a new
version); readers pinned to the prior version are unaffected and pick up the new
one on ``checkout_latest`` / reopen. A kill mid-rebuild leaves the last committed
version intact (atomic).

Dedup fidelity (decision D2): ``search_similar`` defaults to an EXACT cosine
scan (``bypass_vector_index``); the 0.92 / 0.80 thresholds depend on true
cosine. Retrieval callers may pass ``exact=False`` to use the ANN index (D1),
built by ``optimize()`` for scale headroom.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import lancedb
import pyarrow as pa

from agentalloy.storage.card_index import CARD_FRAGMENT_TYPE
from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    BM25Hit,
    EmbeddingDimMismatch,
    FragmentEmbedding,
    SimilarityHit,
    l2_normalize,
)

logger = logging.getLogger(__name__)

# Below this row count an IVF_PQ index is not worth building (and IVF training
# needs enough rows); exact brute-force is sub-millisecond at this scale anyway.
_ANN_MIN_ROWS = 2048

FRAGMENTS_SCHEMA = pa.schema(
    [
        pa.field("fragment_id", pa.string(), nullable=False),
        pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM), nullable=False),
        pa.field("skill_id", pa.string(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("fragment_type", pa.string(), nullable=False),
        pa.field("embedded_at", pa.int64(), nullable=False),
        pa.field("embedding_model", pa.string(), nullable=False),
        pa.field("prose", pa.string(), nullable=False),
        pa.field("phase_scope", pa.list_(pa.string()), nullable=True),
    ]
)


def _q(val: str) -> str:
    """Single-quote a string literal for a Lance SQL filter (escape quotes)."""
    return "'" + val.replace("'", "''") + "'"


def _in_list(vals: Sequence[str]) -> str:
    return "(" + ", ".join(_q(v) for v in vals) + ")"


def _arr_list(vals: Sequence[str]) -> str:
    return "[" + ", ".join(_q(v) for v in vals) + "]"


def _build_filter(
    *,
    categories: Sequence[str] | None,
    phases: Sequence[str] | None,
    fragment_types: Sequence[str] | None,
    deprecated_skill_ids: Sequence[str] | None,
) -> str | None:
    """Build a Lance SQL filter mirroring the v5.3 DuckDB predicates.

    Union eligibility (category map OR authored phase_scope) matches
    ``vector_store.search_similar``: a NULL phase_scope falls back to the
    category map alone (``array_has_any`` is false on null).
    """
    clauses: list[str] = []
    if categories and phases:
        clauses.append(
            f"(category IN {_in_list(categories)} "
            f"OR array_has_any(phase_scope, {_arr_list(phases)}))"
        )
    elif categories:
        clauses.append(f"category IN {_in_list(categories)}")
    elif phases:
        clauses.append(f"array_has_any(phase_scope, {_arr_list(phases)})")
    if fragment_types:
        clauses.append(f"fragment_type IN {_in_list(fragment_types)}")
    if deprecated_skill_ids:
        clauses.append(f"skill_id NOT IN {_in_list(deprecated_skill_ids)}")
    return " AND ".join(clauses) if clauses else None


class LanceFragmentStore:
    """FragmentStore backed by a LanceDB ``fragments`` dataset."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        # config points at ``.../corpus/fragments.lance``; lancedb.connect wants
        # the parent dir and a table name whose .lance dir is that path.
        self._db = lancedb.connect(str(p.parent))
        self._table_name = p.stem  # "fragments"
        # Open-or-create in one call: ``exist_ok=True`` opens the existing table
        # (e.g. a copied/seeded dataset) and creates it from the schema only when
        # absent — avoids relying on a catalog listing that doesn't always see a
        # table copied in out-of-band (the per-test ``corpus_dir`` copytree case).
        self._table = self._db.create_table(
            self._table_name, schema=FRAGMENTS_SCHEMA, exist_ok=True
        )
        self._has_vector_index = self._vector_index_present()

    # -- internal ------------------------------------------------------------

    def _vector_index_present(self) -> bool:
        try:
            return any(
                "embedding" in (getattr(ix, "columns", None) or [])
                for ix in self._table.list_indices()
            )
        except Exception:
            return False

    def _row(self, f: FragmentEmbedding) -> dict[str, object]:
        return {
            "fragment_id": f.fragment_id,
            "embedding": l2_normalize(f.embedding),
            "skill_id": f.skill_id,
            "category": f.category,
            "fragment_type": f.fragment_type,
            "embedded_at": int(f.embedded_at),
            "embedding_model": f.embedding_model,
            "prose": f.prose or "",
            "phase_scope": list(f.phase_scope) if f.phase_scope else None,
        }

    @staticmethod
    def _check_dims(batch: Sequence[FragmentEmbedding]) -> None:
        for f in batch:
            if len(f.embedding) != EMBEDDING_DIM:
                # Message MUST contain an upgrade.py marker substring ("dimension").
                raise EmbeddingDimMismatch(
                    f"fragment_id={f.fragment_id}: embedding has {len(f.embedding)} "
                    f"dimensions, expected {EMBEDDING_DIM} (nomic-embed-text-v1.5)"
                )

    # -- writes --------------------------------------------------------------

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int:
        """Upsert fragments (keyed on ``fragment_id``). Normalizes at write time.

        Uses ``merge_insert`` so re-embedding a skill's fragments replaces the
        prior rows in one commit (no separate delete needed). Returns the number
        of input rows written.
        """
        batch = list(items)
        if not batch:
            return 0
        self._check_dims(batch)
        rows = [self._row(f) for f in batch]
        (
            self._table.merge_insert("fragment_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(rows)
        )
        return len(rows)

    def bulk_replace(self, items: Iterable[FragmentEmbedding]) -> int:
        """Atomically replace the ENTIRE dataset with ``items`` (decision D3).

        Builds a new table version via overwrite in one commit; a kill before
        this returns leaves the prior version intact, and concurrent readers on
        the old version are unaffected (D4). Used by ``reembed --force`` and the
        importer for a full, all-or-nothing rebuild.
        """
        batch = list(items)
        self._check_dims(batch)
        rows = [self._row(f) for f in batch]
        self._table = self._db.create_table(
            self._table_name, schema=FRAGMENTS_SCHEMA, mode="overwrite"
        )
        if rows:
            self._table.add(rows)
        self._has_vector_index = False
        return len(rows)

    def backfill_phase_scope(self, scope_by_skill: dict[str, list[str] | None]) -> int:
        """Set ``phase_scope`` for every fragment of each skill. Returns skills updated."""
        updated = 0
        for skill_id, scope in scope_by_skill.items():
            self._table.update(
                where=f"skill_id = {_q(skill_id)}",
                values={"phase_scope": list(scope) if scope else None},
            )
            updated += 1
        return updated

    def delete_cards(self, skill_id: str | None = None) -> int:
        """Remove synthetic card documents. Returns rows deleted."""
        pred = f"fragment_type = {_q(CARD_FRAGMENT_TYPE)}"
        if skill_id is not None:
            pred += f" AND skill_id = {_q(skill_id)}"
        before = self._table.count_rows(pred)
        self._table.delete(pred)
        return before

    def delete_skill(self, skill_id: str) -> int:
        """Remove all fragments for a skill. Returns rows deleted."""
        pred = f"skill_id = {_q(skill_id)}"
        before = self._table.count_rows(pred)
        self._table.delete(pred)
        return before

    def delete_all(self) -> int:
        """Wipe the dataset (public replacement for the v5.3 private ``_conn`` DELETE)."""
        n = self._table.count_rows()
        self._table = self._db.create_table(
            self._table_name, schema=FRAGMENTS_SCHEMA, mode="overwrite"
        )
        self._has_vector_index = False
        return n

    # -- reads ---------------------------------------------------------------

    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        fragment_types: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        k: int = 10,
        exact: bool = True,
    ) -> list[SimilarityHit]:
        """Top-k cosine distance with optional filters.

        ``exact=True`` (default) forces a brute-force scan — the dedup contract
        (D2): the 0.92 / 0.80 thresholds need true cosine. Retrieval callers may
        pass ``exact=False`` to use the ANN index (D1) at scale.
        """
        if len(query_vec) != EMBEDDING_DIM:
            raise EmbeddingDimMismatch(
                f"query vector has {len(query_vec)} dimensions, expected {EMBEDDING_DIM}"
            )
        if self._table.count_rows() == 0:
            return []
        q = l2_normalize(query_vec)
        # ``distance_type`` lives on the vector-query subclass; LanceDB's stubs
        # type ``.search()`` as the base builder, so pyright can't see it.
        search = self._table.search(q, vector_column_name="embedding").distance_type(  # pyright: ignore[reportAttributeAccessIssue]
            "cosine"
        )
        if exact and self._has_vector_index:
            with contextlib.suppress(Exception):
                search = search.bypass_vector_index()
        flt = _build_filter(
            categories=categories,
            phases=phases,
            fragment_types=fragment_types,
            deprecated_skill_ids=deprecated_skill_ids,
        )
        if flt:
            search = search.where(flt, prefilter=True)
        rows = search.limit(k).to_list()
        return [
            SimilarityHit(
                fragment_id=str(r["fragment_id"]),
                skill_id=str(r["skill_id"]),
                distance=float(r["_distance"]),
            )
            for r in rows
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
        """Native BM25 (Tantivy) over the prose column. Returns [] if no FTS index."""
        if not query.strip():
            return []
        try:
            search = self._table.search(query, query_type="fts")
            flt = _build_filter(
                categories=categories,
                phases=phases,
                fragment_types=None,
                deprecated_skill_ids=deprecated_skill_ids,
            )
            if flt:
                search = search.where(flt, prefilter=True)
            rows = search.limit(k).to_list()
        except Exception:  # noqa: BLE001 — FTS index absent/unavailable; BM25 leg degrades to []
            return []
        return [BM25Hit(fragment_id=str(r["fragment_id"]), score=float(r["_score"])) for r in rows]

    # -- counts / probes -----------------------------------------------------

    def count_embeddings(self) -> int:
        return int(self._table.count_rows())

    def count_cards(self) -> int:
        return int(self._table.count_rows(f"fragment_type = {_q(CARD_FRAGMENT_TYPE)}"))

    def embedding_dim(self) -> int | None:
        """Row-count gated: int when populated, None when empty (hard contract).

        Do NOT read the FixedSizeList width — it is always 768 even on an empty
        dataset, which would flip the install-pack gate's empty-corpus branch.
        """
        return None if self.count_embeddings() == 0 else EMBEDDING_DIM

    def fragment_ids_present(self, fragment_ids: Sequence[str]) -> set[str]:
        ids = list(fragment_ids)
        if not ids:
            return set()
        rows = (
            self._table.search()
            .where(f"fragment_id IN {_in_list(ids)}")
            .select(["fragment_id"])
            .limit(len(ids))
            .to_list()
        )
        return {str(r["fragment_id"]) for r in rows}

    # -- maintenance ---------------------------------------------------------

    def rebuild_fts_index(self) -> None:
        """(Re)build the native BM25 index. No CASCADE workaround needed."""
        try:
            self._table.create_fts_index("prose", replace=True)
        except Exception:
            logger.warning("rebuild_fts_index failed; BM25 leg returns [] until next rebuild")

    def optimize(self) -> None:
        """Compact + (re)build indices (decision D5: automatic upkeep).

        Called at the end of a reembed pass: builds the ANN vector index when
        the corpus is large enough (D1), rebuilds BM25, then compacts files and
        prunes old versions.
        """
        self.rebuild_fts_index()
        if self.count_embeddings() >= _ANN_MIN_ROWS:
            try:
                self._table.create_index(
                    metric="cosine", vector_column_name="embedding", index_type="IVF_PQ"
                )
                self._has_vector_index = True
            except Exception:
                logger.warning("vector index build failed; retrieval falls back to exact scan")
        try:
            self._table.optimize()
        except Exception:
            logger.debug("table.optimize() unavailable or failed; skipping compaction")

    def checkout_latest(self) -> None:
        """Pin to the newest committed version (after an external reembed)."""
        try:
            self._table.checkout_latest()
        except Exception:
            self._table = self._db.open_table(self._table_name)

    def close(self) -> None:
        # LanceDB connections are file handles; nothing to flush. Drop refs.
        self._table = None  # type: ignore[assignment]
        self._db = None  # type: ignore[assignment]
