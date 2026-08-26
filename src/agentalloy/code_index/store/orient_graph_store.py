"""OrientDB-backed per-repo symbol graph (replaces DuckDB ``graph.duck``).

Talks to OrientDB 3.2.x over its HTTP REST API via ``httpx``.  The schema
uses vertex classes ``Symbol``, ``Decision``, ``Lesson`` and edge classes
``Calls``, ``Imports``, ``Inherits``, ``Implements``, ``Overrides``,
``Defines``, ``HasMember``, ``Governs``, ``Requires``, ``Touches``,
``Constraints``, ``Command``, ``Stakeholder``.

Edge-kind mapping (DuckDB flat table → OrientDB typed edge classes):

    CALLS       → Calls
    IMPORTS     → Imports
    INHERITS    → Inherits
    IMPLEMENTS  → Implements
    OVERRIDES   → Overrides
    DEFINES     → Defines
    CONTAINS    → HasMember   (Contains is reserved in OrientDB)
    GOVERNS     → Governs
    REQUIRES    → Requires
    TOUCHES     → Touches
    CONSTRAINTS → Constraints
    COMMAND     → Command
    STAKEHOLDER → Stakeholder

Centrality is stored as a ``pagerank`` FLOAT property on Symbol vertices.
Metadata is stored on a singleton ``Meta`` vertex (key/value pairs serialised
as an EMBEDDEDMAP).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import httpx

from agentalloy.storage.protocols import CallSite, CodeEdge, CodeSymbol, DecisionRow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge-kind ↔ OrientDB class mapping
# ---------------------------------------------------------------------------

_KIND_TO_EDGE_CLASS: dict[str, str] = {
    "CALLS": "Calls",
    "IMPORTS": "Imports",
    "INHERITS": "Inherits",
    "IMPLEMENTS": "Implements",
    "OVERRIDES": "Overrides",
    "DEFINES": "Defines",
    "CONTAINS": "HasMember",
    "HASMEMBER": "HasMember",
    "GOVERNS": "Governs",
    "REQUIRES": "Requires",
    "TOUCHES": "Touches",
    "CONSTRAINTS": "Constraints",
    "COMMAND": "Command",
    "STAKEHOLDER": "Stakeholder",
}

_EDGE_CLASS_TO_KIND: dict[str, str] = {v: k for k, v in _KIND_TO_EDGE_CLASS.items()}
# HasMember maps back to CONTAINS
_EDGE_CLASS_TO_KIND["HasMember"] = "CONTAINS"

_ALL_EDGE_CLASSES = tuple(set(_KIND_TO_EDGE_CLASS.values()))

_ENTITY_EDGE_CLASSES = ("Requires", "Touches", "Constraints", "Command", "Stakeholder")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt_int(v: Any) -> int | None:
    return int(v) if v is not None else None


def _opt_line(v: Any) -> int | None:
    """Edge line columns default to 0 for 'unknown'; surface that as None."""
    return int(v) if v else None


def _edge_class_for(kind: str) -> str:
    """Return the OrientDB edge class for a DuckDB-style edge kind."""
    return _KIND_TO_EDGE_CLASS.get(kind.upper(), kind)


def _kind_for_edge_class(cls: str) -> str:
    """Return the DuckDB-style edge kind for an OrientDB edge class."""
    return _EDGE_CLASS_TO_KIND.get(cls, cls.upper())


# ---------------------------------------------------------------------------
# OrientDB REST client (thin wrapper)
# ---------------------------------------------------------------------------


class _OrientClient:
    """Minimal OrientDB REST client — command / query / batch."""

    def __init__(
        self,
        database: str,
        *,
        base_url: str = "http://localhost:2481",
        username: str = "admin",
        password: str = "admin",
    ) -> None:
        self.database = database
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            auth=(username, password),
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

    # -- low-level -----------------------------------------------------------

    def command(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a write (POST /command/{db}/sql)."""
        body: dict[str, Any] = {"command": sql}
        if params:
            body["parameters"] = params
        try:
            r = self._client.post(f"/command/{self.database}/sql", json=body)
            r.raise_for_status()
            return r.json()
        except Exception:
            logger.warning("OrientDB command failed: %s", sql[:200], exc_info=True)
            return None

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a read (POST /command/{db}/sql). Returns list of record dicts."""
        body: dict[str, Any] = {"command": sql}
        if params:
            body["parameters"] = params
        try:
            r = self._client.post(f"/command/{self.database}/sql", json=body)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            # OrientDB sometimes wraps in {"result": [...]}
            return data.get("result", []) if isinstance(data, dict) else []
        except Exception:
            logger.warning("OrientDB query failed: %s", sql[:200], exc_info=True)
            return []

    def batch(self, commands: list[str]) -> Any:
        """Execute a transactional batch (POST /batch/{db})."""
        body = {
            "transaction": True,
            "operations": [
                {"type": "cmd", "language": "sql", "command": cmd} for cmd in commands
            ],
        }
        try:
            r = self._client.post(f"/batch/{self.database}", json=body)
            r.raise_for_status()
            return r.json()
        except Exception:
            logger.warning("OrientDB batch failed (%d ops)", len(commands), exc_info=True)
            return None

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# OrientDBCodeGraphStore
# ---------------------------------------------------------------------------


class OrientDBCodeGraphStore:
    """CodeGraphStore backed by OrientDB via HTTP REST API."""

    def __init__(
        self,
        database: str,
        *,
        base_url: str = "http://localhost:2481",
        username: str = "admin",
        password: str = "admin",
    ) -> None:
        self._database = database
        self._client = _OrientClient(
            database,
            base_url=base_url,
            username=username,
            password=password,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.debug("failed to close OrientDB HTTP client", exc_info=True)

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Ensure vertex/edge classes and indexes exist. Idempotent."""
        # OrientDB schema is expected to be pre-created (per the task spec),
        # but we attempt lightweight class creation for robustness.
        classes = [
            "CREATE CLASS Symbol IF NOT EXISTS EXTENDS V",
            "CREATE CLASS Decision IF NOT EXISTS EXTENDS V",
            "CREATE CLASS Lesson IF NOT EXISTS EXTENDS V",
            "CREATE CLASS Meta IF NOT EXISTS EXTENDS V",
        ]
        for ec in _ALL_EDGE_CLASSES:
            classes.append(f"CREATE CLASS {ec} IF NOT EXISTS EXTENDS E")
        # Indexes
        classes.append(
            "CREATE INDEX IF NOT EXISTS Symbol.qualified_name ON Symbol (qualified_name UNIQUE)"
        )
        classes.append("CREATE INDEX IF NOT EXISTS Symbol.kind ON Symbol (kind NOTUNIQUE)")
        classes.append("CREATE INDEX IF NOT EXISTS Symbol.name ON Symbol (name NOTUNIQUE)")
        classes.append("CREATE INDEX IF NOT EXISTS Symbol.file_path ON Symbol (file_path NOTUNIQUE)")
        for cmd in classes:
            self._client.command(cmd)
        logger.debug("OrientDB schema ensured for %s", self._database)

    # -- internal helpers ----------------------------------------------------

    def _resolve_qn(self, fqn: str) -> str:
        """Tolerant FQN resolution — exact match first, then unique suffix."""
        rows = self._client.query(
            "SELECT qualified_name FROM Symbol WHERE qualified_name = ? LIMIT 1",
            [fqn],
        )
        if rows:
            return fqn
        # Suffix match on dot boundary
        esc = fqn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._client.query(
            "SELECT qualified_name FROM Symbol WHERE qualified_name LIKE ? LIMIT 2",
            ["%." + esc],
        )
        if len(rows) == 1:
            return str(rows[0].get("qualified_name", fqn))
        return fqn

    def _rid_for_qn(self, fqn: str) -> str | None:
        """Resolve a qualified_name to its OrientDB @rid."""
        rows = self._client.query(
            "SELECT @rid FROM Symbol WHERE qualified_name = ? LIMIT 1",
            [fqn],
        )
        if rows:
            rid = rows[0].get("@rid")
            return str(rid) if rid else None
        return None

    def _rids_for_qns(self, qns: Sequence[str]) -> dict[str, str]:
        """Batch-resolve qualified_names to RIDs."""
        if not qns:
            return {}
        placeholders = ", ".join("?" for _ in qns)
        rows = self._client.query(
            f"SELECT qualified_name, @rid FROM Symbol WHERE qualified_name IN ({placeholders})",
            list(qns),
        )
        return {str(r["qualified_name"]): str(r["@rid"]) for r in rows if r.get("@rid")}

    @staticmethod
    def _symbol_from_row(r: dict[str, Any]) -> CodeSymbol:
        decos = r.get("decorators") or []
        if isinstance(decos, str):
            decos = [decos]
        return CodeSymbol(
            qualified_name=str(r.get("qualified_name", "")),
            kind=str(r.get("kind", "")),
            name=str(r.get("name", "")),
            file_path=r.get("file_path"),
            start_line=_opt_int(r.get("start_line")),
            end_line=_opt_int(r.get("end_line")),
            docstring=r.get("docstring"),
            decorators=[str(d) for d in decos],
            is_exported=r.get("is_exported"),
            is_async=bool(r.get("is_async", False)),
            is_generator=bool(r.get("is_generator", False)),
            source_code=r.get("source_code"),
            contextual_prefix=str(r.get("contextual_prefix") or ""),
            content_hash=r.get("content_hash"),
        )

    def _insert_symbol(self, s: CodeSymbol) -> None:
        """INSERT (upsert) a single Symbol vertex."""
        props = (
            f"qualified_name = ?, kind = ?, name = ?, file_path = ?, "
            f"start_line = ?, end_line = ?, docstring = ?, "
            f"decorators = ?, is_exported = ?, is_async = ?, "
            f"is_generator = ?, source_code = ?, contextual_prefix = ?, "
            f"content_hash = ?"
        )
        params: list[Any] = [
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
        ]
        # Use UPDATE ... UPSERT for idempotent insert-or-update
        self._client.command(
            f"UPDATE Symbol SET {props} UPSERT WHERE qualified_name = ?",
            params + [s.qualified_name],
        )

    def _create_edge(
        self,
        src_rid: str,
        dst_rid: str,
        edge: CodeEdge,
        edge_class: str,
    ) -> None:
        """Create a single edge between two vertex RIDs."""
        self._client.command(
            f"CREATE EDGE {edge_class} FROM {src_rid} TO {dst_rid} "
            f"SET src = ?, dst = ?, kind = ?, file_path = ?, "
            f"line_start = ?, col_start = ?, resolved_via = ?, "
            f"confidence = ?, new_target = ?, span = ?, resolution_tier = ?",
            [
                edge.src,
                edge.dst,
                edge.kind,
                edge.file_path,
                edge.line_start,
                edge.col_start,
                edge.resolved_via,
                edge.confidence,
                edge.new_target,
                edge.span,
                edge.resolution_tier,
            ],
        )

    # -- writes --------------------------------------------------------------

    def replace_all(
        self,
        symbols: Iterable[CodeSymbol],
        edges: Iterable[CodeEdge],
    ) -> tuple[int, int]:
        sym_list = list(symbols)
        edge_list = list(edges)

        # Delete all edges first, then all symbol vertices
        for ec in _ALL_EDGE_CLASSES:
            self._client.command(f"DELETE EDGE {ec}")
        self._client.command("DELETE VERTEX Symbol")
        self._client.command("DELETE VERTEX Decision")

        # Insert symbols
        for s in sym_list:
            self._insert_symbol(s)

        # Insert edges — resolve RIDs in bulk
        qns_needed = set()
        for e in edge_list:
            qns_needed.add(e.src)
            qns_needed.add(e.dst)
        rid_map = self._rids_for_qns(list(qns_needed))

        inserted = 0
        for e in edge_list:
            src_rid = rid_map.get(e.src)
            dst_rid = rid_map.get(e.dst)
            if not src_rid or not dst_rid:
                logger.debug("skipping dangling edge %s -> %s", e.src, e.dst)
                continue
            ec = _edge_class_for(e.kind)
            self._create_edge(src_rid, dst_rid, e, ec)
            inserted += 1

        return (len(sym_list), inserted)

    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int:
        count = 0
        for s in symbols:
            self._insert_symbol(s)
            count += 1
        return count

    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int:
        edge_list = list(edges)
        if not edge_list:
            return 0

        # Resolve RIDs
        qns_needed = set()
        for e in edge_list:
            qns_needed.add(e.src)
            qns_needed.add(e.dst)
        rid_map = self._rids_for_qns(list(qns_needed))

        inserted = 0
        for e in edge_list:
            src_rid = rid_map.get(e.src)
            dst_rid = rid_map.get(e.dst)
            if not src_rid or not dst_rid:
                logger.debug("skipping dangling edge %s -> %s", e.src, e.dst)
                continue
            ec = _edge_class_for(e.kind)
            self._create_edge(src_rid, dst_rid, e, ec)
            inserted += 1
        return inserted

    def delete_for_files(self, file_paths: Sequence[str]) -> int:
        paths = list(file_paths)
        if not paths:
            return 0
        placeholders = ", ".join("?" for _ in paths)

        # Count symbols to delete
        sym_rows = self._client.query(
            f"SELECT qualified_name FROM Symbol WHERE file_path IN ({placeholders})",
            paths,
        )
        deleted_qns = {str(r["qualified_name"]) for r in sym_rows}
        n_sym = len(deleted_qns)

        # Count non-GOVERNS edges to delete (by file_path on the edge)
        n_edge = 0
        for ec in _ALL_EDGE_CLASSES:
            if ec == "Governs":
                continue
            rows = self._client.query(
                f"SELECT count(*) AS cnt FROM {ec} WHERE file_path IN ({placeholders})",
                paths,
            )
            if rows:
                n_edge += int(rows[0].get("cnt", 0) or 0)

        # Delete non-GOVERNS edges by file_path
        for ec in _ALL_EDGE_CLASSES:
            if ec == "Governs":
                continue
            self._client.command(
                f"DELETE EDGE {ec} WHERE file_path IN ({placeholders})",
                paths,
            )

        # Also delete edges where src or dst is a deleted symbol
        if deleted_qns:
            rid_map = self._rids_for_qns(list(deleted_qns))
            for rid in rid_map.values():
                for ec in _ALL_EDGE_CLASSES:
                    if ec == "Governs":
                        continue
                    self._client.command(
                        f"DELETE EDGE {ec} WHERE out() = {rid} OR in() = {rid}"
                    )

        # Delete symbol vertices
        self._client.command(
            f"DELETE VERTEX Symbol WHERE file_path IN ({placeholders})",
            paths,
        )

        return n_sym + n_edge

    # -- symbol lookup -------------------------------------------------------

    def symbol(self, qualified_name: str) -> CodeSymbol | None:
        qualified_name = self._resolve_qn(qualified_name)
        rows = self._client.query(
            "SELECT FROM Symbol WHERE qualified_name = ?",
            [qualified_name],
        )
        if not rows:
            return None
        return self._symbol_from_row(rows[0])

    # -- relations -----------------------------------------------------------

    def callers(self, fqn: str) -> list[CallSite]:
        """Symbols that CALL fqn — inE('Calls') edges pointing TO fqn."""
        fqn = self._resolve_qn(fqn)
        rows = self._client.query(
            """
            SELECT e.src AS src_qn, e.file_path AS edge_fp, e.line_start AS line_start,
                   s.file_path AS sym_fp
            FROM (SELECT expand(inE('Calls')) FROM Symbol WHERE qualified_name = ?) e
            LEFT JOIN Symbol s ON s.qualified_name = e.src
            ORDER BY e.src, e.line_start
            """,
            [fqn],
        )
        results: list[CallSite] = []
        for r in rows:
            fp = r.get("sym_fp") or r.get("edge_fp") or None
            fp = str(fp) if fp else None
            results.append(
                CallSite(
                    qualified_name=str(r.get("src_qn", "")),
                    file_path=fp,
                    line=_opt_line(r.get("line_start")),
                )
            )
        return results

    def callees(self, fqn: str) -> list[CallSite]:
        """Symbols fqn CALLS — outE('Calls') edges going OUT from fqn."""
        fqn = self._resolve_qn(fqn)
        rows = self._client.query(
            """
            SELECT e.dst AS dst_qn, e.line_start AS line_start,
                   s.file_path AS sym_fp, s.start_line AS start_line
            FROM (SELECT expand(outE('Calls')) FROM Symbol WHERE qualified_name = ?) e
            LEFT JOIN Symbol s ON s.qualified_name = e.dst
            ORDER BY e.dst, e.line_start
            """,
            [fqn],
        )
        results: list[CallSite] = []
        for r in rows:
            results.append(
                CallSite(
                    qualified_name=str(r.get("dst_qn", "")),
                    file_path=str(r["sym_fp"]) if r.get("sym_fp") else None,
                    line=_opt_int(r.get("start_line")),
                )
            )
        return results

    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]:
        """All symbols that transitively call fqn within max_depth hops.

        Uses OrientDB TRAVERSE — no depth limit issues vs recursive CTEs.
        """
        if max_depth < 1:
            return []
        fqn = self._resolve_qn(fqn)
        rid = self._rid_for_qn(fqn)
        if not rid:
            return []

        rows = self._client.query(
            f"TRAVERSE in('Calls') FROM {rid} WHILE $depth < {max_depth}",
        )
        if not rows:
            return []

        # Collect distinct caller QNs (exclude the seed)
        caller_qns: list[str] = []
        for r in rows:
            qn = r.get("qualified_name")
            if qn and str(qn) != fqn:
                caller_qns.append(str(qn))

        if not caller_qns:
            return []

        # Fetch file_path and start_line for each
        placeholders = ", ".join("?" for _ in caller_qns)
        sym_rows = self._client.query(
            f"SELECT qualified_name, file_path, start_line FROM Symbol "
            f"WHERE qualified_name IN ({placeholders})",
            caller_qns,
        )
        sym_map = {str(r["qualified_name"]): r for r in sym_rows}

        results: list[CallSite] = []
        for qn in sorted(set(caller_qns)):
            sr = sym_map.get(qn, {})
            results.append(
                CallSite(
                    qualified_name=qn,
                    file_path=str(sr["file_path"]) if sr.get("file_path") else None,
                    line=_opt_int(sr.get("start_line")),
                )
            )
        return results

    # -- decision / knowledge ------------------------------------------------

    def symbols_by_name(self, name: str) -> list[tuple[str, str]]:
        rows = self._client.query(
            "SELECT qualified_name, kind FROM Symbol "
            "WHERE name = ? AND kind <> 'MarkdownDoc' ORDER BY qualified_name",
            [name],
        )
        return [(str(r["qualified_name"]), str(r["kind"])) for r in rows]

    def symbols_by_file(self, file_path: str) -> list[tuple[str, str]]:
        rows = self._client.query(
            "SELECT qualified_name, kind FROM Symbol "
            "WHERE file_path = ? AND kind <> 'MarkdownDoc' ORDER BY qualified_name",
            [file_path],
        )
        return [(str(r["qualified_name"]), str(r["kind"])) for r in rows]

    def decision_qns(self) -> list[str]:
        rows = self._client.query(
            "SELECT qualified_name FROM Symbol WHERE kind = 'MarkdownDoc' ORDER BY qualified_name"
        )
        return [str(r["qualified_name"]) for r in rows]

    def governing_decisions(self, fqn: str) -> list[DecisionRow]:
        fqn = self._resolve_qn(fqn)
        rows = self._client.query(
            """
            SELECT e.src AS src_qn, e.file_path AS edge_fp,
                   d.file_path AS d_fp, d.start_line AS d_sl,
                   d.name AS d_name, d.source_code AS d_sc
            FROM (SELECT expand(inE('Governs')) FROM Symbol WHERE qualified_name = ?) e
            LEFT JOIN Symbol d ON d.qualified_name = e.src
            ORDER BY e.src
            """,
            [fqn],
        )
        return [
            DecisionRow(
                qualified_name=str(r.get("src_qn", "")),
                file_path=str(r["d_fp"]) if r.get("d_fp") else None,
                start_line=_opt_int(r.get("d_sl")),
                heading=str(r["d_name"]) if r.get("d_name") else "",
                snippet=str(r["d_sc"]) if r.get("d_sc") else None,
            )
            for r in rows
        ]

    def decisions_for_files(self, file_paths: Sequence[str]) -> list[DecisionRow]:
        paths = list(file_paths)
        if not paths:
            return []
        placeholders = ", ".join("?" for _ in paths)
        rows = self._client.query(
            f"""
            SELECT DISTINCT e.src AS src_qn,
                   d.file_path AS d_fp, d.start_line AS d_sl,
                   d.name AS d_name, d.source_code AS d_sc
            FROM Governs e
            JOIN Symbol code ON e.in() = code.@rid AND code.file_path IN ({placeholders})
            LEFT JOIN Symbol d ON d.qualified_name = e.src
            ORDER BY e.src
            """,
            paths,
        )
        return [
            DecisionRow(
                qualified_name=str(r.get("src_qn", "")),
                file_path=str(r["d_fp"]) if r.get("d_fp") else None,
                start_line=_opt_int(r.get("d_sl")),
                heading=str(r["d_name"]) if r.get("d_name") else "",
                snippet=str(r["d_sc"]) if r.get("d_sc") else None,
            )
            for r in rows
        ]

    def decision_docs_governing(self, fqns: Sequence[str]) -> list[str]:
        qns = list(fqns)
        if not qns:
            return []
        placeholders = ", ".join("?" for _ in qns)
        rows = self._client.query(
            f"""
            SELECT DISTINCT file_path FROM Governs
            WHERE dst IN ({placeholders}) AND file_path IS NOT NULL
            ORDER BY file_path
            """,
            qns,
        )
        return [str(r["file_path"]) for r in rows if r.get("file_path")]

    def delete_govern_edges_for_doc(self, doc_path: str) -> int:
        # Count first
        rows = self._client.query(
            "SELECT count(*) AS cnt FROM Governs WHERE file_path = ?",
            [doc_path],
        )
        n = int(rows[0]["cnt"]) if rows else 0
        self._client.command(
            "DELETE EDGE Governs WHERE file_path = ?",
            [doc_path],
        )
        return n

    def delete_entity_edges_for_docs(self, file_paths: Sequence[str]) -> int:
        paths = list(file_paths)
        if not paths:
            return 0
        placeholders = ", ".join("?" for _ in paths)
        total = 0
        for ec in _ENTITY_EDGE_CLASSES:
            rows = self._client.query(
                f"SELECT count(*) AS cnt FROM {ec} WHERE file_path IN ({placeholders})",
                paths,
            )
            if rows:
                total += int(rows[0].get("cnt", 0) or 0)
            self._client.command(
                f"DELETE EDGE {ec} WHERE file_path IN ({placeholders})",
                paths,
            )
        return total

    def count_govern_edges_for_doc(self, doc_path: str) -> int:
        rows = self._client.query(
            "SELECT count(*) AS cnt FROM Governs WHERE file_path = ?",
            [doc_path],
        )
        return int(rows[0]["cnt"]) if rows else 0

    def typed_edges_for_fqn(self, fqn: str) -> list[CodeEdge]:
        fqn = self._resolve_qn(fqn)
        results: list[CodeEdge] = []
        for ec in _ENTITY_EDGE_CLASSES:
            kind = _kind_for_edge_class(ec)
            rows = self._client.query(
                f"""
                SELECT e.src AS src_qn, e.dst AS dst_qn, e.file_path AS fp,
                       e.span AS span, e.resolution_tier AS rt
                FROM (SELECT expand(inE('{ec}')) FROM Symbol WHERE qualified_name = ?) e
                ORDER BY e.src
                """,
                [fqn],
            )
            for r in rows:
                results.append(
                    CodeEdge(
                        src=str(r.get("src_qn", "")),
                        dst=str(r.get("dst_qn", fqn)),
                        kind=kind,
                        file_path=str(r.get("fp") or ""),
                        span=str(r["span"]) if r.get("span") else None,
                        resolution_tier=_opt_int(r.get("rt")) or 0,
                    )
                )
        results.sort(key=lambda e: (e.kind, e.src))
        return results

    def typed_edges_from_chunks(
        self,
        chunk_qns: Sequence[str],
        *,
        limit: int = 20,
    ) -> list[CodeEdge]:
        if not chunk_qns:
            return []
        file_paths = list({qn.split("::", 1)[0] for qn in chunk_qns})
        qn_ph = ", ".join("?" for _ in chunk_qns)
        fp_ph = ", ".join("?" for _ in file_paths)

        results: list[CodeEdge] = []
        for ec in _ENTITY_EDGE_CLASSES:
            kind = _kind_for_edge_class(ec)
            rows = self._client.query(
                f"""
                SELECT e.src AS src_qn, e.dst AS dst_qn, e.file_path AS fp,
                       e.span AS span, e.resolution_tier AS rt
                FROM {ec} e
                WHERE (e.src IN ({qn_ph}) OR e.file_path IN ({fp_ph}))
                ORDER BY e.src
                LIMIT ?
                """,
                list(chunk_qns) + file_paths + [limit],
            )
            for r in rows:
                results.append(
                    CodeEdge(
                        src=str(r.get("src_qn", "")),
                        dst=str(r.get("dst_qn", "")),
                        kind=kind,
                        file_path=str(r.get("fp") or ""),
                        span=str(r["span"]) if r.get("span") else None,
                        resolution_tier=_opt_int(r.get("rt")) or 0,
                    )
                )
        results.sort(key=lambda e: (e.kind, e.src))
        return results[:limit]

    # -- aggregates / listings -----------------------------------------------

    def counts_by_kind(self) -> dict[str, int]:
        rows = self._client.query(
            "SELECT kind, count(*) AS cnt FROM Symbol GROUP BY kind"
        )
        return {str(r["kind"]): int(r["cnt"]) for r in rows}

    def list_files(
        self,
        *,
        prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        params: list[Any] = []
        if prefix:
            rows = self._client.query(
                "SELECT DISTINCT file_path FROM Symbol "
                "WHERE file_path IS NOT NULL AND file_path LIKE ? "
                "ORDER BY file_path LIMIT ? SKIP ?",
                [prefix + "%", max(1, int(limit)), max(0, int(offset))],
            )
        else:
            rows = self._client.query(
                "SELECT DISTINCT file_path FROM Symbol "
                "WHERE file_path IS NOT NULL "
                "ORDER BY file_path LIMIT ? SKIP ?",
                [max(1, int(limit)), max(0, int(offset))],
            )
        return [str(r["file_path"]) for r in rows if r.get("file_path")]

    def calls_edges(self) -> list[tuple[str, str]]:
        rows = self._client.query("SELECT src, dst FROM Calls")
        return [(str(r["src"]), str(r["dst"])) for r in rows]

    # -- centrality ----------------------------------------------------------

    def write_centrality(self, scores: Mapping[str, float]) -> int:
        # Clear existing centrality
        self._client.command("UPDATE Symbol REMOVE pagerank")
        count = 0
        for qn, score in scores.items():
            self._client.command(
                "UPDATE Symbol SET pagerank = ? WHERE qualified_name = ?",
                [float(score), qn],
            )
            count += 1
        return count

    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]:
        qns = list(qualified_names)
        if not qns:
            return {}
        placeholders = ", ".join("?" for _ in qns)
        rows = self._client.query(
            f"SELECT qualified_name, pagerank FROM Symbol "
            f"WHERE qualified_name IN ({placeholders}) AND pagerank IS NOT NULL",
            qns,
        )
        return {str(r["qualified_name"]): float(r["pagerank"]) for r in rows}

    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]:
        rows = self._client.query(
            "SELECT qualified_name, pagerank FROM Symbol "
            "WHERE pagerank IS NOT NULL "
            "ORDER BY pagerank DESC, qualified_name LIMIT ?",
            [max(1, int(limit))],
        )
        return [(str(r["qualified_name"]), float(r["pagerank"])) for r in rows]

    # -- incremental-reindex support -----------------------------------------

    def content_hashes(self) -> dict[str, str]:
        rows = self._client.query(
            "SELECT qualified_name, content_hash FROM Symbol WHERE content_hash IS NOT NULL"
        )
        return {str(r["qualified_name"]): str(r["content_hash"]) for r in rows}

    # -- repo_meta kv --------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        # Use a dedicated Meta vertex with key/value properties
        existing = self._client.query(
            "SELECT @rid FROM Meta WHERE meta_key = ? LIMIT 1",
            [key],
        )
        if existing:
            rid = existing[0].get("@rid")
            self._client.command(
                f"UPDATE {rid} SET meta_value = ?, updated_at = ?",
                [value, int(time.time())],
            )
        else:
            self._client.command(
                "INSERT INTO Meta SET meta_key = ?, meta_value = ?, updated_at = ?",
                [key, value, int(time.time())],
            )

    def get_meta(self, key: str) -> str | None:
        rows = self._client.query(
            "SELECT meta_value FROM Meta WHERE meta_key = ? LIMIT 1",
            [key],
        )
        if rows and rows[0].get("meta_value") is not None:
            return str(rows[0]["meta_value"])
        return None

    # -- OrientDB-specific graph algorithms ----------------------------------

    def shortest_path(self, from_fqn: str, to_fqn: str) -> list[str]:
        """Find the shortest path between two symbols via CALLS edges.

        Returns a list of qualified_names along the path (including endpoints).
        Returns empty list if no path exists or either symbol is missing.
        """
        from_rid = self._resolve_qn(from_fqn)
        to_rid = self._resolve_qn(to_fqn)
        if not from_rid or not to_rid:
            return []

        rows = self._client.query(
            f"SELECT shortestPath({from_rid}, {to_rid}, 'UNDIRECTED') AS path"
        )
        if not rows:
            return []

        path_rids = rows[0].get("path", [])
        if not path_rids:
            return []

        # Resolve RIDs back to qualified_names
        result = []
        for rid in path_rids:
            sym_rows = self._client.query(
                f"SELECT qualified_name FROM Symbol WHERE @rid = {rid}"
            )
            if sym_rows:
                result.append(str(sym_rows[0]["qualified_name"]))
        return result

    def community_detection(self, fqn: str, *, algorithm: str = "leiden") -> list[str]:
        """Find the community cluster around a symbol.

        Uses OrientDB's community detection algorithms to find structurally
        cohesive symbols around the given FQN. Returns qualified_names of
        symbols in the same community.

        Note: This is a simplified implementation that traverses the local
        neighborhood. Full Leiden/Louvain community detection would require
        running the algorithm over the entire graph and caching results.
        """
        start_rid = self._resolve_qn(fqn)
        if not start_rid:
            return []

        # Traverse the local neighborhood (2 hops in each direction)
        # This gives a rough "community" based on structural proximity
        rows = self._client.query(
            f"TRAVERSE both('Calls') FROM {start_rid} WHILE $depth < 3"
        )
        if not rows:
            return []

        # Extract qualified_names from traversal results
        result = []
        for row in rows:
            qn = row.get("qualified_name")
            if qn:
                result.append(str(qn))
        return result
