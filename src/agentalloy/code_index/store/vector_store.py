"""LanceDB-backed per-repo code vector store (``vectors.lance``).

Replaces codebase-indexer's DuckDB brute-force vector scan and its standalone
Tantivy sidecar with one Lance dataset: vector ANN + native BM25, cloned from
``agentalloy.storage.fragment_store`` conventions.

The dataset is derived from the graph store (source of truth) — a full
rebuild is an atomic table overwrite (a new MVCC version), so a kill
mid-rebuild leaves the last committed version intact and concurrent readers
are unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import lancedb
import pyarrow as pa

from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    CodeSearchHit,
    CodeVectorRow,
    EmbeddingDimMismatch,
    l2_normalize,
)

logger = logging.getLogger(__name__)

# Below this row count an IVF_PQ index is not worth building (and IVF training
# needs enough rows); exact brute-force is sub-millisecond at this scale anyway.
_ANN_MIN_ROWS = 2048

CODE_VECTORS_SCHEMA = pa.schema(
    [
        pa.field("qualified_name", pa.string(), nullable=False),
        pa.field("embedding", pa.list_(pa.float32(), EMBEDDING_DIM), nullable=False),
        pa.field("symbol_type", pa.string(), nullable=False),
        pa.field("file_path", pa.string(), nullable=False),
        pa.field("start_line", pa.int64(), nullable=True),
        pa.field("end_line", pa.int64(), nullable=True),
        pa.field("indexed_at", pa.int64(), nullable=False),
        pa.field("text", pa.string(), nullable=False),
    ]
)


def _q(val: str) -> str:
    """Single-quote a string literal for a Lance SQL filter (escape quotes)."""
    return "'" + val.replace("'", "''") + "'"


def _in_list(vals: Sequence[str]) -> str:
    return "(" + ", ".join(_q(v) for v in vals) + ")"


def _opt_int(v: object) -> int | None:
    return int(v) if isinstance(v, int) else None


class LanceCodeVectorStore:
    """CodeVectorStore backed by a LanceDB dataset (one per repo slug)."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        # config points at ``.../repos/{slug}/vectors.lance``; lancedb.connect
        # wants the parent dir and a table name whose .lance dir is that path.
        self._db = lancedb.connect(str(p.parent))
        self._table_name = p.stem  # "vectors"
        self._table = self._db.create_table(
            self._table_name, schema=CODE_VECTORS_SCHEMA, exist_ok=True
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

    def _row(self, r: CodeVectorRow) -> dict[str, object]:
        return {
            "qualified_name": r.qualified_name,
            "embedding": l2_normalize(r.embedding),
            "symbol_type": r.symbol_type,
            "file_path": r.file_path,
            "start_line": r.start_line,
            "end_line": r.end_line,
            "indexed_at": int(r.indexed_at),
            "text": r.text or "",
        }

    @staticmethod
    def _check_dims(batch: Sequence[CodeVectorRow]) -> None:
        for r in batch:
            if len(r.embedding) != EMBEDDING_DIM:
                # Message MUST contain an upgrade.py marker substring ("dimension").
                raise EmbeddingDimMismatch(
                    f"qualified_name={r.qualified_name}: embedding has {len(r.embedding)} "
                    f"dimensions, expected {EMBEDDING_DIM} (nomic-embed-text-v1.5)"
                )

    # -- writes --------------------------------------------------------------

    def upsert(self, rows: Iterable[CodeVectorRow]) -> int:
        """Upsert rows (keyed on ``qualified_name``). Normalizes at write time.

        ``merge_insert`` replaces a re-embedded symbol's prior row in one
        commit — no separate delete needed. Returns input rows written.
        """
        batch = list(rows)
        if not batch:
            return 0
        self._check_dims(batch)
        payload = [self._row(r) for r in batch]
        (
            self._table.merge_insert("qualified_name")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(payload)
        )
        return len(payload)

    def bulk_replace(self, rows: Iterable[CodeVectorRow]) -> int:
        """Atomically replace the ENTIRE dataset with ``rows``.

        Builds a new table version via overwrite in one commit; a kill before
        this returns leaves the prior version intact, and concurrent readers on
        the old version are unaffected. Used by full reindex.
        """
        batch = list(rows)
        self._check_dims(batch)
        payload = [self._row(r) for r in batch]
        self._table = self._db.create_table(
            self._table_name, schema=CODE_VECTORS_SCHEMA, mode="overwrite"
        )
        if payload:
            self._table.add(payload)
        self._has_vector_index = False
        return len(payload)

    def delete(self, qualified_names: Sequence[str]) -> int:
        """Remove rows by qualified name. Returns rows deleted."""
        qns = list(qualified_names)
        if not qns:
            return 0
        pred = f"qualified_name IN {_in_list(qns)}"
        before = self._table.count_rows(pred)
        self._table.delete(pred)
        return before

    # -- reads ---------------------------------------------------------------

    def search_similar(self, query_vec: Sequence[float], *, k: int = 10) -> list[CodeSearchHit]:
        """Top-k cosine similarity (``score`` = 1 - cosine distance).

        Row-count dispatch mirrors ``fragment_store``: below ``_ANN_MIN_ROWS``
        no ANN index exists and Lance brute-forces an exact scan; once
        ``optimize()`` has built the IVF_PQ index the search rides ANN.
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
        rows = search.limit(k).to_list()
        return [
            CodeSearchHit(
                qualified_name=str(r["qualified_name"]),
                file_path=str(r["file_path"]),
                start_line=_opt_int(r["start_line"]),
                end_line=_opt_int(r["end_line"]),
                score=1.0 - float(r["_distance"]),
            )
            for r in rows
        ]

    def search_bm25(self, query: str, *, k: int = 10) -> list[tuple[str, float]]:
        """Native BM25 (Tantivy) over the ``text`` column. Returns [] if no
        FTS index has been built yet (BM25 leg degrades gracefully)."""
        if not query.strip():
            return []
        try:
            rows = self._table.search(query, query_type="fts").limit(k).to_list()
        except Exception:  # noqa: BLE001 — FTS index absent/unavailable; degrade to []
            return []
        return [(str(r["qualified_name"]), float(r["_score"])) for r in rows]

    # -- counts / probes -----------------------------------------------------

    def count(self) -> int:
        return int(self._table.count_rows())

    def embedding_dim(self) -> int | None:
        """Row-count gated: int when populated, None when empty (hard contract,
        same as ``LanceFragmentStore.embedding_dim``)."""
        return None if self.count() == 0 else EMBEDDING_DIM

    # -- maintenance ---------------------------------------------------------

    def rebuild_fts_index(self) -> None:
        """(Re)build the native BM25 index over ``text``."""
        try:
            self._table.create_fts_index("text", replace=True)
        except Exception:
            logger.warning("rebuild_fts_index failed; BM25 leg returns [] until next rebuild")

    def optimize(self) -> None:
        """Compact + (re)build indices at the end of an index pass: BM25
        always, the ANN vector index only once the dataset is large enough."""
        self.rebuild_fts_index()
        if self.count() >= _ANN_MIN_ROWS:
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

    def close(self) -> None:
        # LanceDB connections are file handles; nothing to flush. Drop refs.
        self._table = None  # type: ignore[assignment]
        self._db = None  # type: ignore[assignment]
