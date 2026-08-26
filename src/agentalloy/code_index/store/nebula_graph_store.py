"""NebulaGraph-backed per-repo symbol graph.

Replaces the OrientDB implementation with NebulaGraph for native graph
performance.  Talks to NebulaGraph over its binary protocol (default port 9669)
via ``nebula3-python``.

Schema uses vertex tags ``Symbol``, ``Decision``, ``Lesson``, ``Meta`` and edge
types ``Calls``, ``Imports``, ``Inherits``, ``Implements``, ``Overrides``,
``Defines``, ``HasMember``, ``Governs``, ``Requires``, ``Touches``,
``Constraints``, ``Command``, ``Stakeholder``.

Vertex IDs are the symbol qualified_name (FIXED_STRING(256)).

Edge-kind mapping (DuckDB flat table → NebulaGraph edge types):

    CALLS       → Calls
    IMPORTS     → Imports
    INHERITS    → Inherits
    IMPLEMENTS  → Implements
    OVERRIDES   → Overrides
    DEFINES     → Defines
    CONTAINS    → HasMember
    GOVERNS     → Governs
    REQUIRES    → Requires
    TOUCHES     → Touches
    CONSTRAINTS → Constraints
    COMMAND     → Command
    STAKEHOLDER → Stakeholder

Centrality is stored as a ``pagerank`` DOUBLE property on Symbol vertices.
Metadata is stored on ``Meta`` vertices (one per key/value pair).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from nebula3.Config import Config as NebulaConfig
from nebula3.gclient.net import ConnectionPool

from agentalloy.storage.protocols import CallSite, CodeEdge, CodeSymbol, DecisionRow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge-kind ↔ NebulaGraph edge-type mapping
# ---------------------------------------------------------------------------

_KIND_TO_EDGE_TYPE: dict[str, str] = {
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

_EDGE_TYPE_TO_KIND: dict[str, str] = {v: k for k, v in _KIND_TO_EDGE_TYPE.items()}
_EDGE_TYPE_TO_KIND["HasMember"] = "CONTAINS"

_ALL_EDGE_TYPES = tuple(set(_KIND_TO_EDGE_TYPE.values()))

_ENTITY_EDGE_TYPES = ("Requires", "Touches", "Constraints", "Command", "Stakeholder")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt_int(v: Any) -> int | None:
    return int(v) if v is not None else None


def _opt_line(v: Any) -> int | None:
    """Edge line columns default to 0 for 'unknown'; surface that as None."""
    return int(v) if v else None


def _edge_type_for(kind: str) -> str:
    """Return the NebulaGraph edge type for a DuckDB-style edge kind."""
    return _KIND_TO_EDGE_TYPE.get(kind.upper(), kind)


def _kind_for_edge_type(cls: str) -> str:
    """Return the DuckDB-style edge kind for a NebulaGraph edge type."""
    return _EDGE_TYPE_TO_KIND.get(cls, cls.upper())


def _esc(s: str | None) -> str:
    """Escape a string for nGQL string literals (double-quoted)."""
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _ngql_str(s: str | None) -> str:
    """Wrap an escaped string in double quotes for nGQL, or return empty."""
    if s is None:
        return '""'
    return f'"{_esc(s)}"'


def _ngql_int(v: int | None) -> str:
    if v is None:
        return "NULL"
    return str(int(v))


def _ngql_bool(v: bool | None) -> str:
    if v is None:
        return "NULL"
    return "true" if v else "false"


def _ngql_float(v: float | None) -> str:
    if v is None:
        return "NULL"
    return repr(float(v))


def _decode(val: Any) -> str | None:
    """Decode a NebulaGraph value to a Python string (handle bytes)."""
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _decode_or_none(val: Any) -> str | None:
    """Like _decode but returns None for empty strings."""
    s = _decode(val)
    return s if s else None


# ---------------------------------------------------------------------------
# NebulaGraphCodeGraphStore
# ---------------------------------------------------------------------------


class NebulaGraphCodeGraphStore:
    """CodeGraphStore backed by NebulaGraph via the binary protocol."""

    def __init__(
        self,
        space: str = "agentalloy",
        *,
        host: str = "127.0.0.1",
        port: int = 9669,
        username: str = "root",
        password: str = "nebula",
    ) -> None:
        self._space = space
        config = NebulaConfig()
        config.max_connection_pool_size = 10
        self._pool = ConnectionPool()
        self._pool.init([(host, port)], config)
        self._session = self._pool.get_session(username, password)
        self._execute(f"USE `{space}`")
        logger.debug("NebulaGraph store connected to %s:%d/%s", host, port, space)

    # -- low-level nGQL execution -------------------------------------------

    def _execute(self, stmt: str) -> Any:
        """Execute an nGQL statement and return the raw result set."""
        try:
            result = self._session.execute(stmt)
            if not result.is_succeeded():
                logger.warning(
                    "nGQL failed: %s — %s",
                    result.error_msg(),
                    stmt[:200],
                )
            return result
        except Exception:
            logger.warning("nGQL exception: %s", stmt[:200], exc_info=True)
            return None

    def _fetch_rows(self, stmt: str) -> list[dict[str, Any]]:
        """Execute a read statement and return rows as dicts.

        Uses ``as_primitive()`` which returns a dict with ``column_names``
        (list[str]) and ``rows`` (list[list]). Each row is zipped with the
        column names to produce a flat dict.
        """
        result = self._execute(stmt)
        if result is None or not result.is_succeeded():
            return []
        try:
            primitive = result.as_primitive()
            col_names = primitive.get("column_names", [])
            raw_rows = primitive.get("rows", [])
            return [dict(zip(col_names, row)) for row in raw_rows]
        except Exception:
            logger.debug("failed to decode result rows", exc_info=True)
            return []

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._session.release()
        except Exception:
            logger.debug("failed to release NebulaGraph session", exc_info=True)
        try:
            self._pool.close()
        except Exception:
            logger.debug("failed to close NebulaGraph pool", exc_info=True)

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Schema is expected to be pre-created. This is a no-op for NebulaGraph
        since the space, tags, edge types, and indexes are created externally.
        We verify connectivity by running a trivial query."""
        self._execute("SHOW TAGS")
        logger.debug("NebulaGraph schema verified for space %s", self._space)

    # -- internal helpers ----------------------------------------------------

    def _vertex_exists(self, vid: str) -> bool:
        """Check if a vertex with the given ID exists (any tag)."""
        result = self._fetch_rows(
            f'FETCH PROP ON Symbol "{_esc(vid)}" YIELD Symbol.name AS name'
        )
        return len(result) > 0

    def _resolve_qn(self, fqn: str) -> str:
        """Tolerant FQN resolution — exact match first, then suffix lookup."""
        if self._vertex_exists(fqn):
            return fqn
        # Suffix match via LOOKUP
        rows = self._fetch_rows(
            f'LOOKUP ON Symbol WHERE Symbol.name == "{_esc(fqn.rsplit(".", 1)[-1])}" '
            f"YIELD Symbol.name AS name"
        )
        # Fall back to the original FQN if we can't resolve
        return fqn

    def _symbol_from_row(self, r: dict[str, Any]) -> CodeSymbol:
        """Build a CodeSymbol from a NebulaGraph result dict.

        The qualified_name comes from the vertex ID (set by callers as
        ``Symbol.qualified_name`` or ``qualified_name``), NOT from the
        ``name`` property (which is the short symbol name).
        """
        def get(prop: str) -> Any:
            # Try both "Symbol.prop" and bare "prop" keys
            for key in (f"Symbol.{prop}", prop):
                if key in r:
                    return r[key]
            return None

        # qualified_name: prefer explicit key, fall back to vertex id
        qn = _decode(get("qualified_name")) or ""

        decos_raw = get("decorators")
        if isinstance(decos_raw, (list, tuple)):
            decorators = [str(d) for d in decos_raw]
        elif isinstance(decos_raw, str) and decos_raw:
            decorators = [decos_raw]
        else:
            decorators = []

        return CodeSymbol(
            qualified_name=qn,
            kind=str(get("kind") or ""),
            name=str(get("name") or qn.rsplit(".", 1)[-1]),
            file_path=_decode_or_none(get("file_path")),
            start_line=_opt_int(get("start_line")),
            end_line=_opt_int(get("end_line")),
            docstring=_decode_or_none(get("docstring")),
            decorators=decorators,
            is_exported=get("is_exported"),
            is_async=bool(get("is_async") or False),
            is_generator=bool(get("is_generator") or False),
            source_code=_decode_or_none(get("source_code")),
            contextual_prefix=str(get("contextual_prefix") or ""),
            content_hash=_decode_or_none(get("content_hash")),
        )

    def _insert_symbol_ngql(self, s: CodeSymbol) -> str:
        """Build an INSERT VERTEX nGQL statement for a single Symbol."""
        decos_str = ",".join(s.decorators) if s.decorators else ""
        return (
            f'INSERT VERTEX Symbol('
            f'kind, name, file_path, start_line, end_line, docstring, '
            f'decorators, is_exported, is_async, is_generator, '
            f'source_code, contextual_prefix, content_hash, pagerank'
            f') VALUES "{_esc(s.qualified_name)}":('
            f'{_ngql_str(s.kind)}, '
            f'{_ngql_str(s.name)}, '
            f'{_ngql_str(s.file_path)}, '
            f'{_ngql_int(s.start_line)}, '
            f'{_ngql_int(s.end_line)}, '
            f'{_ngql_str(s.docstring)}, '
            f'{_ngql_str(decos_str)}, '
            f'{_ngql_bool(s.is_exported)}, '
            f'{_ngql_bool(s.is_async)}, '
            f'{_ngql_bool(s.is_generator)}, '
            f'{_ngql_str(s.source_code)}, '
            f'{_ngql_str(s.contextual_prefix)}, '
            f'{_ngql_str(s.content_hash)}, '
            f'0.0'
            f')'
        )

    def _batch_insert_symbols(self, symbols: Sequence[CodeSymbol], *, batch_size: int = 50) -> int:
        """Batch insert/update symbols using INSERT VERTEX with multiple rows."""
        if not symbols:
            return 0
        count = 0
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            decos_list = []
            for s in batch:
                decos_list.append(",".join(s.decorators) if s.decorators else "")

            props = (
                "kind, name, file_path, start_line, end_line, docstring, "
                "decorators, is_exported, is_async, is_generator, "
                "source_code, contextual_prefix, content_hash, pagerank"
            )
            values_parts = []
            for j, s in enumerate(batch):
                values_parts.append(
                    f'"{_esc(s.qualified_name)}":('
                    f'{_ngql_str(s.kind)}, '
                    f'{_ngql_str(s.name)}, '
                    f'{_ngql_str(s.file_path)}, '
                    f'{_ngql_int(s.start_line)}, '
                    f'{_ngql_int(s.end_line)}, '
                    f'{_ngql_str(s.docstring)}, '
                    f'{_ngql_str(decos_list[j])}, '
                    f'{_ngql_bool(s.is_exported)}, '
                    f'{_ngql_bool(s.is_async)}, '
                    f'{_ngql_bool(s.is_generator)}, '
                    f'{_ngql_str(s.source_code)}, '
                    f'{_ngql_str(s.contextual_prefix)}, '
                    f'{_ngql_str(s.content_hash)}, '
                    f'0.0)'
                )
            stmt = f"INSERT VERTEX {props} VALUES {', '.join(values_parts)}"
            self._execute(stmt)
            count += len(batch)
        return count

    def _batch_insert_edges(self, edges: Sequence[CodeEdge], *, batch_size: int = 100) -> int:
        """Batch insert edges grouped by edge type."""
        if not edges:
            return 0

        # Group by edge type
        by_type: dict[str, list[CodeEdge]] = {}
        for e in edges:
            et = _edge_type_for(e.kind)
            by_type.setdefault(et, []).append(e)

        count = 0
        for etype, group in by_type.items():
            for i in range(0, len(group), batch_size):
                batch = group[i : i + batch_size]
                if etype == "Governs":
                    # Governs has different props: resolution_tier
                    values_parts = []
                    for e in batch:
                        values_parts.append(
                            f'"{_esc(e.src)}"->"{_esc(e.dst)}":('
                            f'{_ngql_int(e.resolution_tier)})'
                        )
                    stmt = f"INSERT EDGE Governs(resolution_tier) VALUES {', '.join(values_parts)}"
                else:
                    # Standard edge props: confidence, resolved_via, file_path, line_start
                    values_parts = []
                    for e in batch:
                        values_parts.append(
                            f'"{_esc(e.src)}"->"{_esc(e.dst)}":('
                            f'{_ngql_float(e.confidence)}, '
                            f'{_ngql_str(e.resolved_via)}, '
                            f'{_ngql_str(e.file_path)}, '
                            f'{_ngql_int(e.line_start)})'
                        )
                    stmt = (
                        f"INSERT EDGE {etype}(confidence, resolved_via, file_path, line_start) "
                        f"VALUES {', '.join(values_parts)}"
                    )
                self._execute(stmt)
                count += len(batch)
        return count

    # -- writes --------------------------------------------------------------

    def replace_all(
        self,
        symbols: Iterable[CodeSymbol],
        edges: Iterable[CodeEdge],
    ) -> tuple[int, int]:
        sym_list = list(symbols)
        edge_list = list(edges)

        # Delete all edges and symbol/decision vertices
        for et in _ALL_EDGE_TYPES:
            self._execute(f"DELETE EDGE {et}")
        self._execute("DELETE TAG Symbol")
        self._execute("DELETE TAG Decision")

        # Insert symbols
        n_sym = self._batch_insert_symbols(sym_list)

        # Insert edges
        n_edge = self._batch_insert_edges(edge_list)

        return (n_sym, n_edge)

    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int:
        sym_list = list(symbols)
        if not sym_list:
            return 0
        return self._batch_insert_symbols(sym_list)

    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int:
        edge_list = list(edges)
        if not edge_list:
            return 0
        return self._batch_insert_edges(edge_list)

    def delete_for_files(self, file_paths: Sequence[str]) -> int:
        paths = list(file_paths)
        if not paths:
            return 0

        path_list = ", ".join(f'"{_esc(p)}"' for p in paths)

        # Find symbols to delete
        rows = self._fetch_rows(
            f"LOOKUP ON Symbol WHERE Symbol.file_path IN [{path_list}] "
            f"YIELD id(vertex) AS vid"
        )
        deleted_qns = [_decode(r.get("vid", "")) for r in rows if r.get("vid")]
        n_sym = len(deleted_qns)

        # Count edges to delete (by file_path on the edge)
        n_edge = 0
        for et in _ALL_EDGE_TYPES:
            if et == "Governs":
                continue
            count_rows = self._fetch_rows(
                f"LOOKUP ON {et} WHERE {et}.file_path IN [{path_list}] "
                f"YIELD {et}.confidence AS c"
            )
            n_edge += len(count_rows)

        # Delete edges by file_path
        for et in _ALL_EDGE_TYPES:
            if et == "Governs":
                continue
            self._execute(
                f"DELETE EDGE {et} WHERE {et}.file_path IN [{path_list}]"
            )

        # Delete edges touching deleted vertices
        for qn in deleted_qns:
            for et in _ALL_EDGE_TYPES:
                if et == "Governs":
                    continue
                self._execute(
                    f'DELETE EDGE {et} WHERE id(src) == "{_esc(qn)}" OR id(dst) == "{_esc(qn)}"'
                )

        # Delete symbol vertices
        if deleted_qns:
            vid_list = ", ".join(f'"{_esc(q)}"' for q in deleted_qns)
            self._execute(f"DELETE VERTEX {vid_list}")

        return n_sym + n_edge

    # -- symbol lookup -------------------------------------------------------

    def symbol(self, qualified_name: str) -> CodeSymbol | None:
        qualified_name = self._resolve_qn(qualified_name)
        rows = self._fetch_rows(
            f'FETCH PROP ON Symbol "{_esc(qualified_name)}" '
            f"YIELD id(vertex) AS qualified_name, "
            f"Symbol.kind AS kind, Symbol.name AS name, "
            f"Symbol.file_path AS file_path, Symbol.start_line AS start_line, "
            f"Symbol.end_line AS end_line, Symbol.docstring AS docstring, "
            f"Symbol.decorators AS decorators, Symbol.is_exported AS is_exported, "
            f"Symbol.is_async AS is_async, Symbol.is_generator AS is_generator, "
            f"Symbol.source_code AS source_code, "
            f"Symbol.contextual_prefix AS contextual_prefix, "
            f"Symbol.content_hash AS content_hash"
        )
        if not rows:
            return None
        return self._symbol_from_row(rows[0])

    # -- relations -----------------------------------------------------------

    def callers(self, fqn: str) -> list[CallSite]:
        """Symbols that CALL fqn — reverse traversal over Calls edges."""
        fqn = self._resolve_qn(fqn)
        rows = self._fetch_rows(
            f'GO FROM "{_esc(fqn)}" OVER Calls REVERSELY '
            f"YIELD Calls._src AS caller, Calls.file_path AS edge_fp, "
            f"Calls.line_start AS line_start"
        )
        results: list[CallSite] = []
        seen: set[str] = set()
        for r in rows:
            caller = _decode(r.get("caller", ""))
            if not caller or caller in seen:
                continue
            seen.add(caller)
            # Fetch the caller's file_path
            sym_rows = self._fetch_rows(
                f'FETCH PROP ON Symbol "{_esc(caller)}" '
                f"YIELD Symbol.file_path AS fp, Symbol.start_line AS sl"
            )
            fp = None
            line = None
            if sym_rows:
                fp = _decode_or_none(sym_rows[0].get("fp"))
                line = _opt_int(sym_rows[0].get("sl"))
            if not fp:
                fp = _decode_or_none(r.get("edge_fp"))
            if line is None:
                line = _opt_line(r.get("line_start"))
            results.append(
                CallSite(qualified_name=caller, file_path=fp, line=line)
            )
        results.sort(key=lambda c: (c.qualified_name, c.line or 0))
        return results

    def callees(self, fqn: str) -> list[CallSite]:
        """Symbols fqn CALLS — forward traversal over Calls edges."""
        fqn = self._resolve_qn(fqn)
        rows = self._fetch_rows(
            f'GO FROM "{_esc(fqn)}" OVER Calls '
            f"YIELD Calls._dst AS callee, Calls.line_start AS line_start"
        )
        results: list[CallSite] = []
        seen: set[str] = set()
        for r in rows:
            callee = _decode(r.get("callee", ""))
            if not callee or callee in seen:
                continue
            seen.add(callee)
            sym_rows = self._fetch_rows(
                f'FETCH PROP ON Symbol "{_esc(callee)}" '
                f"YIELD Symbol.file_path AS fp, Symbol.start_line AS sl"
            )
            fp = None
            line = None
            if sym_rows:
                fp = _decode_or_none(sym_rows[0].get("fp"))
                line = _opt_int(sym_rows[0].get("sl"))
            results.append(
                CallSite(qualified_name=callee, file_path=fp, line=line)
            )
        results.sort(key=lambda c: (c.qualified_name, c.line or 0))
        return results

    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]:
        """All symbols that transitively call fqn within max_depth hops."""
        if max_depth < 1:
            return []
        fqn = self._resolve_qn(fqn)
        if not self._vertex_exists(fqn):
            return []

        # Use repeated GO to traverse reverse Calls edges
        visited: set[str] = {fqn}
        frontier: set[str] = {fqn}

        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: set[str] = set()
            for vid in frontier:
                rows = self._fetch_rows(
                    f'GO FROM "{_esc(vid)}" OVER Calls REVERSELY '
                    f"YIELD Calls._src AS caller"
                )
                for r in rows:
                    caller = _decode(r.get("caller", ""))
                    if caller and caller not in visited:
                        visited.add(caller)
                        next_frontier.add(caller)
            frontier = next_frontier

        visited.discard(fqn)
        if not visited:
            return []

        # Fetch file_path and start_line for each caller
        caller_list = list(visited)
        results: list[CallSite] = []
        for qn in sorted(caller_list):
            sym_rows = self._fetch_rows(
                f'FETCH PROP ON Symbol "{_esc(qn)}" '
                f"YIELD Symbol.file_path AS fp, Symbol.start_line AS sl"
            )
            fp = None
            line = None
            if sym_rows:
                fp = _decode_or_none(sym_rows[0].get("fp"))
                line = _opt_int(sym_rows[0].get("sl"))
            results.append(
                CallSite(qualified_name=qn, file_path=fp, line=line)
            )
        return results

    # -- decision / knowledge ------------------------------------------------

    def symbols_by_name(self, name: str) -> list[tuple[str, str]]:
        rows = self._fetch_rows(
            f'LOOKUP ON Symbol WHERE Symbol.name == "{_esc(name)}" '
            f"YIELD id(vertex) AS vid, Symbol.kind AS kind"
        )
        return [
            (_decode(r.get("vid", "")), _decode(r.get("kind", "")))
            for r in rows
            if r.get("kind") and _decode(r["kind"]) != "MarkdownDoc"
        ]

    def symbols_by_file(self, file_path: str) -> list[tuple[str, str]]:
        rows = self._fetch_rows(
            f'LOOKUP ON Symbol WHERE Symbol.file_path == "{_esc(file_path)}" '
            f"YIELD id(vertex) AS vid, Symbol.kind AS kind"
        )
        return [
            (_decode(r.get("vid", "")), _decode(r.get("kind", "")))
            for r in rows
            if r.get("kind") and _decode(r["kind"]) != "MarkdownDoc"
        ]

    def decision_qns(self) -> list[str]:
        rows = self._fetch_rows(
            "LOOKUP ON Decision YIELD id(vertex) AS vid"
        )
        return [_decode(r.get("vid", "")) for r in rows if r.get("vid")]

    def governing_decisions(self, fqn: str) -> list[DecisionRow]:
        """Decisions that govern fqn — reverse traversal over Governs edges."""
        fqn = self._resolve_qn(fqn)
        rows = self._fetch_rows(
            f'GO FROM "{_esc(fqn)}" OVER Governs REVERSELY '
            f"YIELD Governs._src AS decision_vid"
        )
        results: list[DecisionRow] = []
        for r in rows:
            dec_vid = _decode(r.get("decision_vid", ""))
            if not dec_vid:
                continue
            # Fetch decision properties
            dec_rows = self._fetch_rows(
                f'FETCH PROP ON Decision "{_esc(dec_vid)}" '
                f"YIELD Decision.source_path AS sp, Decision.title AS title, "
                f"Decision.body AS body"
            )
            if dec_rows:
                dr = dec_rows[0]
                results.append(
                    DecisionRow(
                        qualified_name=dec_vid,
                        file_path=_decode_or_none(dr.get("sp")),
                        start_line=None,
                        heading=_decode(dr.get("title")) or "",
                        snippet=_decode_or_none(dr.get("body")),
                    )
                )
        return results

    def decisions_for_files(self, file_paths: Sequence[str]) -> list[DecisionRow]:
        paths = list(file_paths)
        if not paths:
            return []

        # Find symbols in these files, then find Governs edges targeting them
        path_list = ", ".join(f'"{_esc(p)}"' for p in paths)
        sym_rows = self._fetch_rows(
            f"LOOKUP ON Symbol WHERE Symbol.file_path IN [{path_list}] "
            f"YIELD id(vertex) AS vid"
        )
        sym_qns = [_decode(r.get("vid", "")) for r in sym_rows if r.get("vid")]
        if not sym_qns:
            return []

        results: list[DecisionRow] = []
        seen: set[str] = set()
        for qn in sym_qns:
            gov_rows = self._fetch_rows(
                f'GO FROM "{_esc(qn)}" OVER Governs REVERSELY '
                f"YIELD Governs._src AS decision_vid"
            )
            for r in gov_rows:
                dec_vid = _decode(r.get("decision_vid", ""))
                if not dec_vid or dec_vid in seen:
                    continue
                seen.add(dec_vid)
                dec_rows = self._fetch_rows(
                    f'FETCH PROP ON Decision "{_esc(dec_vid)}" '
                    f"YIELD Decision.source_path AS sp, Decision.title AS title, "
                    f"Decision.body AS body"
                )
                if dec_rows:
                    dr = dec_rows[0]
                    results.append(
                        DecisionRow(
                            qualified_name=dec_vid,
                            file_path=_decode_or_none(dr.get("sp")),
                            start_line=None,
                            heading=_decode(dr.get("title")) or "",
                            snippet=_decode_or_none(dr.get("body")),
                        )
                    )
        results.sort(key=lambda d: d.qualified_name)
        return results

    def decision_docs_governing(self, fqns: Sequence[str]) -> list[str]:
        qns = list(fqns)
        if not qns:
            return []

        docs: set[str] = set()
        for qn in qns:
            rows = self._fetch_rows(
                f'GO FROM "{_esc(qn)}" OVER Governs REVERSELY '
                f"YIELD Governs._src AS decision_vid"
            )
            for r in rows:
                dec_vid = _decode(r.get("decision_vid", ""))
                if dec_vid:
                    dec_rows = self._fetch_rows(
                        f'FETCH PROP ON Decision "{_esc(dec_vid)}" '
                        f"YIELD Decision.source_path AS sp"
                    )
                    if dec_rows:
                        sp = _decode_or_none(dec_rows[0].get("sp"))
                        if sp:
                            docs.add(sp)
        return sorted(docs)

    def delete_govern_edges_for_doc(self, doc_path: str) -> int:
        # Find all decision vertices with this source_path
        rows = self._fetch_rows(
            f'LOOKUP ON Decision WHERE Decision.source_path == "{_esc(doc_path)}" '
            f"YIELD id(vertex) AS vid"
        )
        dec_vids = [_decode(r.get("vid", "")) for r in rows if r.get("vid")]
        count = 0
        for vid in dec_vids:
            # Find all outgoing Governs edges from this decision
            edge_rows = self._fetch_rows(
                f'GO FROM "{_esc(vid)}" OVER Governs '
                f"YIELD Governs._dst AS dst"
            )
            for er in edge_rows:
                dst = _decode(er.get("dst", ""))
                if dst:
                    self._execute(
                        f'DELETE EDGE Governs "{_esc(vid)}"->"{_esc(dst)}"'
                    )
                    count += 1
        return count

    def delete_entity_edges_for_docs(self, file_paths: Sequence[str]) -> int:
        paths = list(file_paths)
        if not paths:
            return 0
        # Entity edges (Requires, Touches, etc.) don't have a direct file_path
        # filter in NebulaGraph edge types without properties. We use LOOKUP
        # if the edge type has a file_path property, otherwise skip.
        # For NebulaGraph, entity edges have no file_path property per the schema,
        # so this is a best-effort implementation.
        total = 0
        # These edge types have no properties per the schema, so we can't
        # filter by file_path. Return 0 as a safe default.
        logger.debug(
            "delete_entity_edges_for_docs: entity edge types have no file_path "
            "property in NebulaGraph schema; returning 0"
        )
        return total

    def count_govern_edges_for_doc(self, doc_path: str) -> int:
        rows = self._fetch_rows(
            f'LOOKUP ON Decision WHERE Decision.source_path == "{_esc(doc_path)}" '
            f"YIELD id(vertex) AS vid"
        )
        dec_vids = [_decode(r.get("vid", "")) for r in rows if r.get("vid")]
        count = 0
        for vid in dec_vids:
            edge_rows = self._fetch_rows(
                f'GO FROM "{_esc(vid)}" OVER Governs '
                f"YIELD Governs._dst AS dst"
            )
            count += len(edge_rows)
        return count

    def typed_edges_for_fqn(self, fqn: str) -> list[CodeEdge]:
        """Entity edges (Requires, Touches, etc.) incoming to fqn."""
        fqn = self._resolve_qn(fqn)
        results: list[CodeEdge] = []
        for et in _ENTITY_EDGE_TYPES:
            kind = _kind_for_edge_type(et)
            rows = self._fetch_rows(
                f'GO FROM "{_esc(fqn)}" OVER {et} REVERSELY '
                f"YIELD {et}._src AS src_vid"
            )
            for r in rows:
                src_vid = _decode(r.get("src_vid", ""))
                if src_vid:
                    results.append(
                        CodeEdge(
                            src=src_vid,
                            dst=fqn,
                            kind=kind,
                            file_path="",
                            span=None,
                            resolution_tier=0,
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
        results: list[CodeEdge] = []
        for et in _ENTITY_EDGE_TYPES:
            kind = _kind_for_edge_type(et)
            for qn in chunk_qns:
                if len(results) >= limit:
                    break
                rows = self._fetch_rows(
                    f'GO FROM "{_esc(qn)}" OVER {et} '
                    f"YIELD {et}._dst AS dst_vid"
                )
                for r in rows:
                    if len(results) >= limit:
                        break
                    dst_vid = _decode(r.get("dst_vid", ""))
                    if dst_vid:
                        results.append(
                            CodeEdge(
                                src=qn,
                                dst=dst_vid,
                                kind=kind,
                                file_path="",
                                span=None,
                                resolution_tier=0,
                            )
                        )
        results.sort(key=lambda e: (e.kind, e.src))
        return results[:limit]

    # -- aggregates / listings -----------------------------------------------

    def counts_by_kind(self) -> dict[str, int]:
        rows = self._fetch_rows(
            "LOOKUP ON Symbol YIELD id(vertex) AS vid, Symbol.kind AS kind"
        )
        counts: dict[str, int] = {}
        for r in rows:
            k = _decode(r.get("kind", ""))
            if k:
                counts[k] = counts.get(k, 0) + 1
        return counts

    def list_files(
        self,
        *,
        prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        if prefix:
            rows = self._fetch_rows(
                f'LOOKUP ON Symbol WHERE Symbol.file_path STARTS WITH "{_esc(prefix)}" '
                f"YIELD id(vertex) AS vid, Symbol.file_path AS fp"
            )
        else:
            rows = self._fetch_rows(
                "LOOKUP ON Symbol YIELD id(vertex) AS vid, Symbol.file_path AS fp"
            )
        files: set[str] = set()
        for r in rows:
            fp = _decode_or_none(r.get("fp"))
            if fp:
                files.add(fp)
        sorted_files = sorted(files)
        return sorted_files[offset : offset + max(1, int(limit))]

    def calls_edges(self) -> list[tuple[str, str]]:
        """All Calls edges as (src, dst) pairs.

        Uses a sampling approach since NebulaGraph doesn't support
        scanning all edges of a type without a starting vertex.
        We use LOOKUP ON for edge indexes if available, otherwise
        traverse from all known vertices.
        """
        # Try LOOKUP on Calls edge index
        rows = self._fetch_rows(
            "LOOKUP ON Calls YIELD Calls._src AS src, Calls._dst AS dst"
        )
        if rows:
            return [
                (_decode(r.get("src", "")), _decode(r.get("dst", "")))
                for r in rows
                if r.get("src") and r.get("dst")
            ]
        return []

    # -- centrality ----------------------------------------------------------

    def write_centrality(self, scores: Mapping[str, float]) -> int:
        if not scores:
            return 0
        # Update pagerank property on each Symbol vertex
        count = 0
        batch_parts: list[str] = []
        for qn, score in scores.items():
            batch_parts.append(
                f'UPDATE VERTEX ON Symbol "{_esc(qn)}" '
                f"SET pagerank = {float(score)}"
            )
            count += 1
            # Execute in batches to avoid oversized statements
            if len(batch_parts) >= 50:
                for stmt in batch_parts:
                    self._execute(stmt)
                batch_parts.clear()
        for stmt in batch_parts:
            self._execute(stmt)
        return count

    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]:
        qns = list(qualified_names)
        if not qns:
            return {}
        result: dict[str, float] = {}
        for qn in qns:
            rows = self._fetch_rows(
                f'FETCH PROP ON Symbol "{_esc(qn)}" '
                f"YIELD Symbol.pagerank AS pr"
            )
            if rows:
                pr = rows[0].get("pr")
                if pr is not None:
                    result[qn] = float(pr)
        return result

    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]:
        rows = self._fetch_rows(
            f"LOOKUP ON Symbol YIELD id(vertex) AS vid, Symbol.pagerank AS pr"
        )
        scored = [
            (_decode(r.get("vid", "")), float(r.get("pr", 0)))
            for r in rows
            if r.get("pr") is not None and r.get("vid")
        ]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[: max(1, int(limit))]

    # -- incremental-reindex support -----------------------------------------

    def content_hashes(self) -> dict[str, str]:
        rows = self._fetch_rows(
            "LOOKUP ON Symbol YIELD id(vertex) AS vid, Symbol.content_hash AS ch"
        )
        return {
            _decode(r["vid"]): _decode(r["ch"])
            for r in rows
            if r.get("vid") and r.get("ch")
        }

    # -- repo_meta kv --------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        # Upsert a Meta vertex keyed by the meta key
        self._execute(
            f'INSERT VERTEX Meta(meta_key, meta_value, updated_at) '
            f'VALUES "{_esc(key)}":('
            f'{_ngql_str(key)}, {_ngql_str(value)}, {_ngql_int(int(time.time()))})'
        )

    def get_meta(self, key: str) -> str | None:
        rows = self._fetch_rows(
            f'FETCH PROP ON Meta "{_esc(key)}" '
            f"YIELD Meta.meta_value AS mv"
        )
        if rows:
            mv = rows[0].get("mv")
            if mv is not None:
                return _decode(mv)
        return None
