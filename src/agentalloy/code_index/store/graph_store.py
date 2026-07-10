"""DuckDB-backed per-repo symbol graph (``graph.duck``).

Replaces the kuzu/LadybugDB property graph from codebase-indexer with two
relational tables (``symbols`` / ``edges``) plus ``centrality`` and a
``repo_meta`` kv. ``file_path`` is denormalized onto ``symbols`` so the
callers/callees query surface is a single join through the CALLS edges — no
DEFINES hop.

No FK constraints: edge endpoints may dangle (calls into unresolved external
packages are expected and kept for completeness).

Concurrency: DuckDB is single-writer cross-process. Index jobs run inside the
service process (the service IS the code-index writer), serialized per slug by
``open.slug_write_lock``; out-of-process consumers open read-only or use the
HTTP API.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb

from agentalloy.storage.protocols import CallSite, CodeEdge, CodeSymbol, DecisionRow

logger = logging.getLogger(__name__)

# Idempotent CREATE IF NOT EXISTS; run once per writer open. The graph is
# derived data (rebuilt from source, never migrated) so there is no ALTER
# ladder — a schema change means "reindex".
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS symbols (
  qualified_name TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  file_path TEXT,
  start_line BIGINT,
  end_line BIGINT,
  docstring TEXT,
  decorators TEXT[],
  is_exported BOOLEAN,
  is_async BOOLEAN DEFAULT FALSE,
  is_generator BOOLEAN DEFAULT FALSE,
  source_code TEXT,
  contextual_prefix TEXT DEFAULT '',
  content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL,
  file_path TEXT DEFAULT '',
  line_start BIGINT DEFAULT 0,
  col_start BIGINT DEFAULT 0,
  resolved_via TEXT DEFAULT 'unknown',
  confidence DOUBLE DEFAULT 1.0,
  new_target TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(kind, src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(kind, dst);

CREATE TABLE IF NOT EXISTS centrality (
  qualified_name TEXT PRIMARY KEY,
  pagerank REAL NOT NULL,
  updated_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at BIGINT NOT NULL
);
"""

_SYMBOL_COLS = (
    "qualified_name, kind, name, file_path, start_line, end_line, docstring, decorators, "
    "is_exported, is_async, is_generator, source_code, contextual_prefix, content_hash"
)
_SYMBOL_PLACEHOLDERS = ", ".join("?" for _ in range(14))

_EDGE_COLS = (
    "src, dst, kind, file_path, line_start, col_start, resolved_via, confidence, new_target"
)
_EDGE_PLACEHOLDERS = ", ".join("?" for _ in range(9))


def _symbol_params(s: CodeSymbol) -> tuple[object, ...]:
    return (
        s.qualified_name,
        s.kind,
        s.name,
        s.file_path,
        s.start_line,
        s.end_line,
        s.docstring,
        list(s.decorators),
        s.is_exported,
        s.is_async,
        s.is_generator,
        s.source_code,
        s.contextual_prefix,
        s.content_hash,
    )


def _edge_params(e: CodeEdge) -> tuple[object, ...]:
    return (
        e.src,
        e.dst,
        e.kind,
        e.file_path,
        e.line_start,
        e.col_start,
        e.resolved_via,
        e.confidence,
        e.new_target,
    )


def _opt_int(v: Any) -> int | None:
    return int(v) if v is not None else None


def _opt_line(v: Any) -> int | None:
    """Edge line columns default to 0 for "unknown"; surface that as None."""
    return int(v) if v else None


class DuckDBCodeGraphStore:
    """CodeGraphStore backed by a per-repo DuckDB file."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self._db_path = str(db_path)
        self._read_only = read_only
        if not read_only:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection | None = duckdb.connect(
            self._db_path, read_only=read_only
        )

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("CodeGraphStore is closed")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("failed to close code-graph DuckDB connection", exc_info=True)
            self._conn = None

    # -- schema ----------------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema. Idempotent. Writer-mode only (RO can't create)."""
        if self._read_only:
            raise RuntimeError("cannot migrate a read-only CodeGraphStore")
        self.conn.execute(_SCHEMA_DDL)
        logger.debug("graph.duck schema ensured at %s", self._db_path)

    # -- writes ------------------------------------------------------------------

    def replace_all(
        self, symbols: Iterable[CodeSymbol], edges: Iterable[CodeEdge]
    ) -> tuple[int, int]:
        """Replace the entire graph (DELETE + INSERT in one transaction).

        The atomic-rebuild-by-file-rename path belongs to ``open.py``'s writer
        role; this keeps the store itself simple: a kill mid-call rolls back to
        the prior contents.
        """
        sym_rows = [_symbol_params(s) for s in symbols]
        edge_rows = [_edge_params(e) for e in edges]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("DELETE FROM edges")
            self.conn.execute("DELETE FROM symbols")
            if sym_rows:
                self.conn.executemany(
                    f"INSERT INTO symbols ({_SYMBOL_COLS}) VALUES ({_SYMBOL_PLACEHOLDERS})",
                    sym_rows,
                )
            if edge_rows:
                self.conn.executemany(
                    f"INSERT INTO edges ({_EDGE_COLS}) VALUES ({_EDGE_PLACEHOLDERS})",
                    edge_rows,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return (len(sym_rows), len(edge_rows))

    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int:
        rows = [_symbol_params(s) for s in symbols]
        if not rows:
            return 0
        self.conn.executemany(
            f"INSERT OR REPLACE INTO symbols ({_SYMBOL_COLS}) VALUES ({_SYMBOL_PLACEHOLDERS})",
            rows,
        )
        return len(rows)

    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int:
        """Append edges. Edges have no natural key — incremental reindex first
        clears a file's edges via :meth:`delete_for_files`, then re-inserts."""
        rows = [_edge_params(e) for e in edges]
        if not rows:
            return 0
        self.conn.executemany(
            f"INSERT INTO edges ({_EDGE_COLS}) VALUES ({_EDGE_PLACEHOLDERS})",
            rows,
        )
        return len(rows)

    def delete_for_files(self, file_paths: Sequence[str]) -> int:
        """Drop all symbols AND edges recorded against the given files
        (incremental-reindex support). Returns total rows removed."""
        paths = list(file_paths)
        if not paths:
            return 0
        placeholders = ", ".join("?" for _ in paths)
        self.conn.execute("BEGIN TRANSACTION")
        try:
            n_sym = self._scalar(
                f"SELECT count(*) FROM symbols WHERE file_path IN ({placeholders})", paths
            )
            n_edge = self._scalar(
                f"SELECT count(*) FROM edges WHERE file_path IN ({placeholders})", paths
            )
            self.conn.execute(f"DELETE FROM symbols WHERE file_path IN ({placeholders})", paths)
            self.conn.execute(f"DELETE FROM edges WHERE file_path IN ({placeholders})", paths)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return int(n_sym or 0) + int(n_edge or 0)

    # -- symbol lookup -------------------------------------------------------------

    def symbol(self, qualified_name: str) -> CodeSymbol | None:
        rows = self.conn.execute(
            f"SELECT {_SYMBOL_COLS} FROM symbols WHERE qualified_name = ?",
            [qualified_name],
        ).fetchall()
        if not rows:
            return None
        r = rows[0]
        return CodeSymbol(
            qualified_name=str(r[0]),
            kind=str(r[1]),
            name=str(r[2]),
            file_path=None if r[3] is None else str(r[3]),
            start_line=_opt_int(r[4]),
            end_line=_opt_int(r[5]),
            docstring=None if r[6] is None else str(r[6]),
            decorators=[str(d) for d in (r[7] or [])],
            is_exported=None if r[8] is None else bool(r[8]),
            is_async=bool(r[9]),
            is_generator=bool(r[10]),
            source_code=None if r[11] is None else str(r[11]),
            contextual_prefix=str(r[12] or ""),
            content_hash=None if r[13] is None else str(r[13]),
        )

    # -- relations -------------------------------------------------------------

    def callers(self, fqn: str) -> list[CallSite]:
        """Symbols that CALL ``fqn``. ``line`` is the call-site line in the
        caller's file (edge ``line_start``); file resolved via the denormalized
        ``symbols.file_path`` with the edge's own file as fallback."""
        rows = self.conn.execute(
            """
            SELECT e.src, COALESCE(s.file_path, NULLIF(e.file_path, '')), e.line_start
            FROM edges e
            LEFT JOIN symbols s ON s.qualified_name = e.src
            WHERE e.kind = 'CALLS' AND e.dst = ?
            ORDER BY e.src, e.line_start
            """,
            [fqn],
        ).fetchall()
        return [
            CallSite(
                qualified_name=str(r[0]),
                file_path=None if r[1] is None else str(r[1]),
                line=_opt_line(r[2]),
            )
            for r in rows
        ]

    def callees(self, fqn: str) -> list[CallSite]:
        """Symbols ``fqn`` CALLS. ``line`` is the callee's definition line."""
        rows = self.conn.execute(
            """
            SELECT e.dst, s.file_path, s.start_line
            FROM edges e
            LEFT JOIN symbols s ON s.qualified_name = e.dst
            WHERE e.kind = 'CALLS' AND e.src = ?
            ORDER BY e.dst, e.line_start
            """,
            [fqn],
        ).fetchall()
        return [
            CallSite(
                qualified_name=str(r[0]),
                file_path=None if r[1] is None else str(r[1]),
                line=_opt_int(r[2]),
            )
            for r in rows
        ]

    # -- decisions (Knowledge module) ------------------------------------------

    def symbols_by_name(self, name: str) -> list[tuple[str, str]]:
        """Code symbols with the given short ``name`` (``MarkdownDoc`` excluded).

        The store's only other symbol getter is exact-PK :meth:`symbol`; this is
        the by-name lookup the decision-linkage tier-2 resolver needs. ``name`` is
        unindexed, so this is a scan — acceptable at per-repo scale."""
        rows = self.conn.execute(
            "SELECT qualified_name, kind FROM symbols "
            "WHERE name = ? AND kind != 'MarkdownDoc' ORDER BY qualified_name",
            [name],
        ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def governing_decisions(self, fqn: str) -> list[DecisionRow]:
        """Decisions that GOVERN ``fqn`` — the ``callers()`` shape with the
        ``GOVERNS`` edge kind. Reads ``e.src`` (the decision chunk) and hydrates
        its heading (``symbols.name``) and body (``symbols.source_code``). One hop,
        not transitive: a decision about ``fqn`` does not govern its callees."""
        rows = self.conn.execute(
            """
            SELECT e.src, s.file_path, s.start_line, s.name, s.source_code
            FROM edges e
            LEFT JOIN symbols s ON s.qualified_name = e.src
            WHERE e.kind = 'GOVERNS' AND e.dst = ?
            ORDER BY e.src
            """,
            [fqn],
        ).fetchall()
        return [
            DecisionRow(
                qualified_name=str(r[0]),
                file_path=None if r[1] is None else str(r[1]),
                start_line=_opt_int(r[2]),
                heading="" if r[3] is None else str(r[3]),
                snippet=None if r[4] is None else str(r[4]),
            )
            for r in rows
        ]

    def decisions_for_files(self, file_paths: Sequence[str]) -> list[DecisionRow]:
        """Decisions governing any symbol defined in ``file_paths`` — the
        file-scoped analogue of :meth:`governing_decisions`. One indexed join
        (`edges GOVERNS ⋈ symbols dst ON dst.file_path ∈ files`), `DISTINCT` so a
        decision governing several touched files appears once."""
        paths = list(file_paths)
        if not paths:
            return []
        placeholders = ", ".join("?" for _ in paths)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT e.src, d.file_path, d.start_line, d.name, d.source_code
            FROM edges e
            JOIN symbols code ON code.qualified_name = e.dst
                             AND code.file_path IN ({placeholders})
            LEFT JOIN symbols d ON d.qualified_name = e.src
            WHERE e.kind = 'GOVERNS'
            ORDER BY e.src
            """,
            paths,
        ).fetchall()
        return [
            DecisionRow(
                qualified_name=str(r[0]),
                file_path=None if r[1] is None else str(r[1]),
                start_line=_opt_int(r[2]),
                heading="" if r[3] is None else str(r[3]),
                snippet=None if r[4] is None else str(r[4]),
            )
            for r in rows
        ]

    def delete_govern_edges_for_doc(self, doc_path: str) -> int:
        """Drop every ``GOVERNS`` edge rooted at ``doc_path`` (edges carry
        ``file_path`` = the decision doc). Doc-granular, so re-derivation matches
        the file-granularity of :meth:`delete_for_files`. Returns rows removed."""
        n = self._scalar(
            "SELECT count(*) FROM edges WHERE kind = 'GOVERNS' AND file_path = ?",
            [doc_path],
        )
        self.conn.execute(
            "DELETE FROM edges WHERE kind = 'GOVERNS' AND file_path = ?",
            [doc_path],
        )
        return int(n or 0)

    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]:
        """All symbols that (transitively) call ``fqn`` within ``max_depth`` hops.

        Depth-capped recursive CTE: rows carry their depth, so a call cycle
        cannot recurse forever — the ``depth < max_depth`` predicate bounds the
        expansion. The seed symbol is excluded from the result even when it
        participates in a cycle back to itself.
        """
        if max_depth < 1:
            return []
        rows = self.conn.execute(
            """
            WITH RECURSIVE up(qn, depth) AS (
                SELECT src, 1 FROM edges WHERE kind = 'CALLS' AND dst = ?
                UNION
                SELECT e.src, u.depth + 1
                FROM edges e JOIN up u ON e.kind = 'CALLS' AND e.dst = u.qn
                WHERE u.depth < ?
            )
            SELECT DISTINCT u.qn, s.file_path, s.start_line
            FROM up u
            LEFT JOIN symbols s ON s.qualified_name = u.qn
            WHERE u.qn <> ?
            ORDER BY u.qn
            """,
            [fqn, max_depth, fqn],
        ).fetchall()
        return [
            CallSite(
                qualified_name=str(r[0]),
                file_path=None if r[1] is None else str(r[1]),
                line=_opt_int(r[2]),
            )
            for r in rows
        ]

    # -- aggregates / listings ---------------------------------------------------

    def counts_by_kind(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT kind, count(*) FROM symbols GROUP BY kind").fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def list_files(
        self, *, prefix: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[str]:
        params: list[object] = []
        where = "WHERE file_path IS NOT NULL"
        if prefix:
            where += " AND starts_with(file_path, ?)"
            params.append(prefix)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        rows = self.conn.execute(
            f"SELECT DISTINCT file_path FROM symbols {where} ORDER BY file_path LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [str(r[0]) for r in rows]

    def calls_edges(self) -> list[tuple[str, str]]:
        rows = self.conn.execute("SELECT src, dst FROM edges WHERE kind = 'CALLS'").fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    # -- centrality --------------------------------------------------------------

    def write_centrality(self, scores: Mapping[str, float]) -> int:
        """Replace the centrality snapshot wholesale (one transaction)."""
        now = int(time.time())
        items = [(qn, float(score), now) for qn, score in scores.items()]
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute("DELETE FROM centrality")
            if items:
                self.conn.executemany(
                    "INSERT INTO centrality (qualified_name, pagerank, updated_at) "
                    "VALUES (?, ?, ?)",
                    items,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return len(items)

    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]:
        qns = list(qualified_names)
        if not qns:
            return {}
        placeholders = ", ".join("?" for _ in qns)
        rows = self.conn.execute(
            f"SELECT qualified_name, pagerank FROM centrality "
            f"WHERE qualified_name IN ({placeholders})",
            qns,
        ).fetchall()
        return {str(r[0]): float(r[1]) for r in rows}

    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            "SELECT qualified_name, pagerank FROM centrality "
            "ORDER BY pagerank DESC, qualified_name LIMIT ?",
            [max(1, int(limit))],
        ).fetchall()
        return [(str(r[0]), float(r[1])) for r in rows]

    # -- incremental-reindex support -----------------------------------------------

    def content_hashes(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT qualified_name, content_hash FROM symbols WHERE content_hash IS NOT NULL"
        ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    # -- repo_meta kv ----------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO repo_meta (key, value, updated_at) VALUES (?, ?, ?)",
            [key, value, int(time.time())],
        )

    def get_meta(self, key: str) -> str | None:
        rows = self.conn.execute("SELECT value FROM repo_meta WHERE key = ?", [key]).fetchall()
        return str(rows[0][0]) if rows else None

    # -- internal --------------------------------------------------------------------

    def _scalar(self, sql: str, params: Sequence[object]) -> Any:
        rows = self.conn.execute(sql, list(params)).fetchall()
        return rows[0][0] if rows else None
