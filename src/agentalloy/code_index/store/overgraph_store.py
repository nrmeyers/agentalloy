"""OverGraph-backed per-repo symbol graph and vector store.

Replaces DuckDB/NebulaGraph + LanceDB with OverGraph, which unifies graph
and vector storage in a single embedded database. OverGraph is written in
Rust with Python bindings, uses GQL (Cypher-style) queries, and has built-in
HNSW vector indexes.

Schema uses node labels ``Symbol``, ``Decision``, ``Meta`` and edge labels
``Calls``, ``Imports``, ``Inherits``, ``Implements``, ``Overrides``,
``Defines``, ``HasMember``, ``Governs``, ``Requires``, ``Touches``,
``Constraints``, ``Command``, ``Stakeholder``.

Node IDs are integers assigned by OverGraph. We maintain a mapping from
qualified_name (key) to node_id for edge creation.

Edge-kind mapping (DuckDB flat table → OverGraph edge labels):

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

Centrality is stored as a ``pagerank`` property on Symbol nodes.
Metadata is stored on ``Meta`` nodes (one per key/value pair).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    CallSite,
    CodeEdge,
    CodeSearchHit,
    CodeSymbol,
    CodeVectorRow,
    DecisionRow,
    EmbeddingDimMismatch,
    l2_normalize,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge-kind ↔ OverGraph edge-label mapping
# ---------------------------------------------------------------------------

_KIND_TO_EDGE_LABEL: dict[str, str] = {
    "CALLS": "Calls",
    "IMPORTS": "Imports",
    "INHERITS": "Inherits",
    "IMPLEMENTS": "Implements",
    "OVERRIDES": "Overrides",
    "DEFINES": "Defines",
    "DEFINES_METHOD": "Defines_method",
    "CONTAINS": "HasMember",
    "CONTAINS_PACKAGE": "HasMember",
    "CONTAINS_FOLDER": "HasMember",
    "CONTAINS_FILE": "HasMember",
    "CONTAINS_MODULE": "HasMember",
    "HASMEMBER": "HasMember",
    "GOVERNS": "Governs",
    "REQUIRES": "Requires",
    "TOUCHES": "Touches",
    "CONSTRAINTS": "Constraints",
    "COMMAND": "Command",
    "STAKEHOLDER": "Stakeholder",
    "RE_EXPORTS": "Re_exports",
    "EXPORTS": "Re_exports",
    "EXPORTS_MODULE": "Re_exports",
    "IMPLEMENTS_MODULE": "Implements",
    "DEPENDS_ON_EXTERNAL": "Depends_on_external",
    "REBINDS": "Rebinds",
}

_EDGE_LABEL_TO_KIND: dict[str, str] = {v: k for k, v in _KIND_TO_EDGE_LABEL.items()}
_EDGE_LABEL_TO_KIND["HasMember"] = "CONTAINS"

_ALL_EDGE_LABELS = tuple(set(_KIND_TO_EDGE_LABEL.values()))

_ENTITY_EDGE_LABELS = ("Requires", "Touches", "Constraints", "Command", "Stakeholder")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opt_int(v: Any) -> int | None:
    return int(v) if v is not None else None


def _opt_line(v: Any) -> int | None:
    """Edge line columns default to 0 for 'unknown'; surface that as None."""
    return int(v) if v else None


def _edge_label_for(kind: str) -> str:
    """Return the OverGraph edge label for a DuckDB-style edge kind."""
    return _KIND_TO_EDGE_LABEL.get(kind.upper(), kind)


def _kind_for_edge_label(label: str) -> str:
    """Return the DuckDB-style edge kind for an OverGraph edge label."""
    return _EDGE_LABEL_TO_KIND.get(label, label.upper())


def _esc(s: str | None) -> str:
    """Escape a string for GQL string literals (single-quoted)."""
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _gql_str(s: str | None) -> str:
    """Wrap an escaped string in single quotes for GQL, or return empty."""
    if s is None:
        return "''"
    return f"'{_esc(s)}'"


def _gql_int(v: int | None) -> str:
    if v is None:
        return "NULL"
    return str(int(v))


def _gql_bool(v: bool | None) -> str:
    if v is None:
        return "NULL"
    return "true" if v else "false"


def _gql_float(v: float | None) -> str:
    if v is None:
        return "NULL"
    return repr(float(v))


def _decode(val: Any) -> str | None:
    """Decode an OverGraph value to a Python string (handle bytes)."""
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
# OverGraphCodeGraphStore
# ---------------------------------------------------------------------------


class OverGraphCodeGraphStore:
    """CodeGraphStore and CodeVectorStore backed by OverGraph.

    Implements both graph operations (CodeGraphStore protocol) and vector
    operations (CodeVectorStore protocol) using OverGraph's unified API.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        vector_dimension: int = EMBEDDING_DIM,
    ) -> None:
        """Open or create an OverGraph database.

        Args:
            db_path: Path to the OverGraph database directory.
            vector_dimension: Dimension of dense vectors for HNSW index.
                Defaults to EMBEDDING_DIM (768 for nomic-embed-text-v1.5).
        """
        import overgraph

        self._db_path = str(db_path)
        self._vector_dimension = vector_dimension
        self._db = overgraph.OverGraph.open(
            self._db_path,
            dense_vector_dimension=vector_dimension,
        )
        self._qn_to_id: dict[str, int] = {}
        self._verify_dimension_alignment(vector_dimension)
        logger.debug("OverGraph store opened at %s (vector_dim=%d)", db_path, vector_dimension)

    def _verify_dimension_alignment(self, expected_dim: int) -> None:
        """Verify that the database's vector dimension matches expectations.

        OverGraph persists dense_vector_dimension in its schema. If the
        embedding model changed (e.g. 768 → 1536), the existing HNSW index
        is incompatible and data must be re-indexed into a fresh directory.
        """
        try:
            schema = self._db.get_node_schema("Symbol")
            if schema and "dense_vector_dimension" in schema:
                actual = int(schema["dense_vector_dimension"])
                if actual != expected_dim:
                    raise EmbeddingDimMismatch(
                        f"OverGraph database at {self._db_path} was created with "
                        f"dense_vector_dimension={actual}, but the current embedding "
                        f"model requires {expected_dim}. Delete the OverGraph directory "
                        f"and re-index from scratch."
                    )
                logger.debug("vector dimension alignment verified: %d", actual)
        except EmbeddingDimMismatch:
            raise
        except Exception:
            # Schema may not expose this field yet (first open); that's fine.
            logger.debug("could not verify vector dimension from schema (first open?)")

    # -- low-level GQL execution --------------------------------------------

    def _execute_gql(self, stmt: str) -> Any:
        """Execute a GQL statement and return the raw result set."""
        try:
            result = self._db.execute_gql(stmt)
            return result
        except Exception:
            logger.warning("GQL exception: %s", stmt[:200], exc_info=True)
            return None

    def _fetch_rows(self, stmt: str) -> list[dict[str, Any]]:
        """Execute a read statement and return rows as dicts."""
        result = self._execute_gql(stmt)
        if result is None:
            return []
        try:
            # OverGraph returns a dict with 'rows' (list of dicts) and 'columns'
            if isinstance(result, dict):
                rows = result.get("rows", [])
                if rows and isinstance(rows[0], dict):
                    return rows
                # Fallback: rows as tuples + columns list
                col_names = result.get("columns", [])
                if col_names and rows:
                    return [dict(zip(col_names, row, strict=False)) for row in rows]
                return []
            # Legacy object-style result
            if hasattr(result, "rows"):
                raw_rows = result.rows
                if hasattr(result, "columns"):
                    col_names = result.columns
                    return [dict(zip(col_names, row, strict=False)) for row in raw_rows]
                return [{"value": row[0]} if len(row) == 1 else {"row": row} for row in raw_rows]
            return []
        except Exception:
            logger.debug("failed to decode result rows", exc_info=True)
            return []

    # -- ID mapping ----------------------------------------------------------

    def _get_node_id(self, qualified_name: str) -> int | None:
        """Get the node ID for a qualified name, checking cache first."""
        if qualified_name in self._qn_to_id:
            return self._qn_to_id[qualified_name]
        # Try to fetch from database
        try:
            node = self._db.get_node_by_key("Symbol", qualified_name)
            if node:
                # Extract node ID from the node object
                node_id = getattr(node, "id", None)
                if node_id is not None:
                    self._qn_to_id[qualified_name] = node_id
                    return node_id
        except Exception:
            pass
        return None

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the OverGraph database."""
        try:
            if hasattr(self._db, "close"):
                self._db.close()
        except Exception:
            logger.debug("failed to close OverGraph database", exc_info=True)

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Create schema (labels) and rebuild ID mapping."""
        # Ensure node labels exist
        self._db.ensure_node_label("Symbol")
        self._db.ensure_node_label("Decision")
        self._db.ensure_node_label("Meta")

        # Ensure edge labels exist
        for label in _ALL_EDGE_LABELS:
            self._db.ensure_edge_label(label)

        # Rebuild ID mapping
        self._rebuild_id_mapping()
        logger.debug("OverGraph schema migrated")

    # -- internal helpers ----------------------------------------------------

    def _node_exists(self, qualified_name: str) -> bool:
        """Check if a node with the given key exists."""
        try:
            node = self._db.get_node_by_key("Symbol", qualified_name)
            return node is not None
        except Exception:
            return False

    def _resolve_qn(self, fqn: str) -> str:
        """Tolerant FQN resolution — exact match first, then suffix lookup."""
        if self._node_exists(fqn):
            return fqn
        # Try suffix match via GQL
        short_name = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
        try:
            result = self._db.execute_gql(
                f"MATCH (n:Symbol) WHERE n.name = '{_esc(short_name)}' RETURN n.qualified_name LIMIT 1"
            )
            if isinstance(result, dict) and result.get("rows"):
                key = _decode(result["rows"][0].get("n.qualified_name"))
                if key:
                    return key
        except Exception:
            pass
        return fqn

    def _symbol_from_node(self, node: Any) -> CodeSymbol:
        """Build a CodeSymbol from an OverGraph node."""
        props = getattr(node, "props", {}) or {}
        key = getattr(node, "key", "") or ""

        def get(prop: str) -> Any:
            return props.get(prop)

        decos_raw = get("decorators")
        if isinstance(decos_raw, (list, tuple)):
            decorators = [str(d) for d in decos_raw]
        elif isinstance(decos_raw, str) and decos_raw:
            decorators = [decos_raw]
        else:
            decorators = []

        return CodeSymbol(
            qualified_name=key,
            kind=str(get("kind") or ""),
            name=str(get("name") or key.rsplit(".", 1)[-1]),
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

    def _symbol_props(self, s: CodeSymbol) -> dict[str, Any]:
        """Convert a CodeSymbol to a properties dict for OverGraph."""
        decos_str = ",".join(s.decorators) if s.decorators else ""
        return {
            "qualified_name": s.qualified_name,
            "kind": s.kind,
            "name": s.name,
            "file_path": s.file_path or "",
            "start_line": s.start_line,
            "end_line": s.end_line,
            "docstring": s.docstring or "",
            "decorators": decos_str,
            "is_exported": s.is_exported,
            "is_async": s.is_async,
            "is_generator": s.is_generator,
            "source_code": s.source_code or "",
            "contextual_prefix": s.contextual_prefix or "",
            "content_hash": s.content_hash or "",
            "pagerank": 0.0,
        }

    def _upsert_symbol_node(self, s: CodeSymbol) -> int:
        """Upsert a single Symbol node and return its ID."""
        props = self._symbol_props(s)
        node_id = self._db.upsert_node(
            labels=["Symbol"],
            key=s.qualified_name,
            props=props,
        )
        self._qn_to_id[s.qualified_name] = node_id
        return node_id

    def _batch_upsert_symbols(self, symbols: Sequence[CodeSymbol]) -> int:
        """Batch upsert Symbol nodes."""
        if not symbols:
            return 0

        # Prepare batch data
        batch_data = []
        for s in symbols:
            props = self._symbol_props(s)
            batch_data.append(
                {
                    "labels": ["Symbol"],
                    "key": s.qualified_name,
                    "props": props,
                }
            )

        try:
            # Use batch_upsert_nodes if available
            if hasattr(self._db, "batch_upsert_nodes"):
                self._db.batch_upsert_nodes(batch_data)
                # Rebuild ID mapping from database
                self._rebuild_id_mapping()
            else:
                # Fall back to individual upserts
                for s in symbols:
                    self._upsert_symbol_node(s)
                return len(symbols)
        except Exception:
            logger.warning(
                "batch_upsert_nodes failed, falling back to individual upserts", exc_info=True
            )
            for s in symbols:
                self._upsert_symbol_node(s)

        return len(symbols)

    def _rebuild_id_mapping(self) -> None:
        """Rebuild the qualified_name -> node_id mapping from the database.

        Uses the native API (``nodes_by_labels`` + ``get_nodes``) because
        ``n.key`` is not accessible via GQL (OverGraph stores the key
        separately from node properties).
        """
        try:
            self._qn_to_id.clear()
            node_ids = list(self._db.nodes_by_labels(["Symbol"]))
            if not node_ids:
                logger.debug("rebuilt ID mapping: 0 symbols (empty)")
                return
            # Fetch in chunks to avoid oversized requests
            chunk_size = 1000
            for i in range(0, len(node_ids), chunk_size):
                chunk = node_ids[i : i + chunk_size]
                nodes = self._db.get_nodes(chunk)
                for node in nodes:
                    key = getattr(node, "key", None)
                    nid = getattr(node, "id", None)
                    if key and nid is not None:
                        self._qn_to_id[key] = nid
            logger.debug("rebuilt ID mapping: %d symbols", len(self._qn_to_id))
        except Exception:
            logger.warning("failed to rebuild ID mapping", exc_info=True)

    def _upsert_edge(self, e: CodeEdge) -> int | None:
        """Upsert a single edge and return its ID."""
        src_id = self._get_node_id(e.src)
        dst_id = self._get_node_id(e.dst)
        if src_id is None or dst_id is None:
            return None

        label = _edge_label_for(e.kind)
        props: dict[str, Any] = {
            "confidence": e.confidence,
            "resolved_via": e.resolved_via,
            "file_path": e.file_path or "",
            "line_start": e.line_start,
        }

        if label == "Governs":
            props = {"resolution_tier": e.resolution_tier or 0}

        try:
            edge_id = self._db.upsert_edge(
                from_id=src_id,
                to_id=dst_id,
                label=label,
                props=props,
            )
            return edge_id
        except Exception:
            logger.debug("failed to upsert edge %s -%s-> %s", e.src, label, e.dst, exc_info=True)
            return None

    def _batch_upsert_edges(self, edges: Sequence[CodeEdge]) -> int:
        """Batch upsert edges."""
        if not edges:
            return 0

        # Prepare batch data
        batch_data = []
        for e in edges:
            src_id = self._get_node_id(e.src)
            dst_id = self._get_node_id(e.dst)
            if src_id is None or dst_id is None:
                continue

            label = _edge_label_for(e.kind)
            props: dict[str, Any] = {
                "confidence": e.confidence,
                "resolved_via": e.resolved_via,
                "file_path": e.file_path or "",
                "line_start": e.line_start,
            }

            if label == "Governs":
                props = {"resolution_tier": e.resolution_tier or 0}

            batch_data.append(
                {
                    "from_id": src_id,
                    "to_id": dst_id,
                    "label": label,
                    "props": props,
                }
            )

        try:
            if hasattr(self._db, "batch_upsert_edges"):
                self._db.batch_upsert_edges(batch_data)
            else:
                # Fall back to individual upserts
                for e in edges:
                    self._upsert_edge(e)
                return len(batch_data)
        except Exception:
            logger.warning(
                "batch_upsert_edges failed, falling back to individual upserts", exc_info=True
            )
            for e in edges:
                self._upsert_edge(e)

        return len(batch_data)

    # -- writes --------------------------------------------------------------

    def replace_all(
        self,
        symbols: Iterable[CodeSymbol],
        edges: Iterable[CodeEdge],
    ) -> tuple[int, int]:
        """Replace all symbols and edges in the graph."""
        sym_list = list(symbols)
        edge_list = list(edges)

        # Delete all nodes and edges
        try:
            # Delete all edges first
            for label in _ALL_EDGE_LABELS:
                self._db.execute_gql(f"MATCH ()-[r:{label}]->() DELETE r")
            # Delete all nodes
            self._db.execute_gql("MATCH (n:Symbol) DETACH DELETE n")
            self._db.execute_gql("MATCH (n:Decision) DETACH DELETE n")
        except Exception:
            logger.warning("failed to clear graph", exc_info=True)

        # Clear ID mapping
        self._qn_to_id.clear()

        # Insert symbols
        n_sym = self._batch_upsert_symbols(sym_list)

        # Insert edges
        n_edge = self._batch_upsert_edges(edge_list)

        return (n_sym, n_edge)

    def upsert_symbols(self, symbols: Iterable[CodeSymbol]) -> int:
        """Upsert symbols into the graph."""
        sym_list = list(symbols)
        if not sym_list:
            return 0
        return self._batch_upsert_symbols(sym_list)

    def upsert_edges(self, edges: Iterable[CodeEdge]) -> int:
        """Upsert edges into the graph."""
        edge_list = list(edges)
        if not edge_list:
            return 0
        return self._batch_upsert_edges(edge_list)

    def delete_for_files(self, file_paths: Sequence[str]) -> int:
        """Delete symbols and edges for the given files."""
        paths = list(file_paths)
        if not paths:
            return 0

        # Find symbols to delete
        deleted_count = 0
        for path in paths:
            try:
                result = self._db.execute_gql(
                    f"MATCH (n:Symbol) WHERE n.file_path = '{_esc(path)}' RETURN n.qualified_name, id(n)"
                )
                if isinstance(result, dict):
                    for row in result.get("rows", []):
                        key = _decode(row.get("n.qualified_name"))
                        node_id = row.get("id(n)")
                        if key and node_id is not None:
                            node_id = int(node_id)
                            # Delete edges touching this node
                            for label in _ALL_EDGE_LABELS:
                                if label == "Governs":
                                    continue
                                self._db.execute_gql(
                                    f"MATCH ()-[r:{label}]->() WHERE id(r) IN "
                                    f"(MATCH (n) WHERE id(n) = {node_id} RETURN id(n)) DELETE r"
                                )
                            # Delete the node
                            self._db.execute_gql(
                                f"MATCH (n) WHERE id(n) = {node_id} DETACH DELETE n"
                            )
                            self._qn_to_id.pop(key, None)
                            deleted_count += 1
            except Exception:
                logger.warning("failed to delete symbols for file %s", path, exc_info=True)

        return deleted_count

    # -- symbol lookup -------------------------------------------------------

    def symbol(self, qualified_name: str) -> CodeSymbol | None:
        """Look up a symbol by qualified name."""
        qualified_name = self._resolve_qn(qualified_name)
        try:
            node = self._db.get_node_by_key("Symbol", qualified_name)
            if node:
                return self._symbol_from_node(node)
        except Exception:
            logger.debug("failed to fetch symbol %s", qualified_name, exc_info=True)
        return None

    # -- relations -----------------------------------------------------------

    def callers(self, fqn: str) -> list[CallSite]:
        """Symbols that CALL fqn — reverse traversal over Calls edges."""
        fqn = self._resolve_qn(fqn)
        node_id = self._get_node_id(fqn)
        if node_id is None:
            return []

        results: list[CallSite] = []
        seen: set[str] = set()

        try:
            neighbors = list(self._db.neighbors(node_id, direction="incoming"))
            for neighbor in neighbors:
                # Check if the edge is a Calls edge
                edge_label = getattr(neighbor, "edge_label", None)
                if edge_label != "Calls":
                    continue

                caller_id = neighbor.node_id
                try:
                    caller_node = self._db.get_node(caller_id)
                    caller_key = getattr(caller_node, "key", "")
                    if not caller_key or caller_key in seen:
                        continue
                    seen.add(caller_key)

                    props = getattr(caller_node, "props", {}) or {}
                    fp = _decode_or_none(props.get("file_path"))
                    line = _opt_int(props.get("start_line"))

                    results.append(CallSite(qualified_name=caller_key, file_path=fp, line=line))
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch callers for %s", fqn, exc_info=True)

        results.sort(key=lambda c: (c.qualified_name, c.line or 0))
        return results

    def callees(self, fqn: str) -> list[CallSite]:
        """Symbols fqn CALLS — forward traversal over Calls edges."""
        fqn = self._resolve_qn(fqn)
        node_id = self._get_node_id(fqn)
        if node_id is None:
            return []

        results: list[CallSite] = []
        seen: set[str] = set()

        try:
            neighbors = list(self._db.neighbors(node_id, direction="outgoing"))
            for neighbor in neighbors:
                edge_label = getattr(neighbor, "edge_label", None)
                if edge_label != "Calls":
                    continue

                callee_id = neighbor.node_id
                try:
                    callee_node = self._db.get_node(callee_id)
                    callee_key = getattr(callee_node, "key", "")
                    if not callee_key or callee_key in seen:
                        continue
                    seen.add(callee_key)

                    props = getattr(callee_node, "props", {}) or {}
                    fp = _decode_or_none(props.get("file_path"))
                    line = _opt_int(props.get("start_line"))

                    results.append(CallSite(qualified_name=callee_key, file_path=fp, line=line))
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch callees for %s", fqn, exc_info=True)

        results.sort(key=lambda c: (c.qualified_name, c.line or 0))
        return results

    def transitive_callers(self, fqn: str, *, max_depth: int = 4) -> list[CallSite]:
        """All symbols that transitively call fqn within max_depth hops."""
        if max_depth < 1:
            return []
        fqn = self._resolve_qn(fqn)
        node_id = self._get_node_id(fqn)
        if node_id is None:
            return []

        # BFS traversal
        visited: set[int] = {node_id}
        frontier: set[int] = {node_id}
        caller_ids: set[int] = set()

        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier: set[int] = set()
            for nid in frontier:
                try:
                    neighbors = list(self._db.neighbors(nid, direction="incoming"))
                    for neighbor in neighbors:
                        edge_label = getattr(neighbor, "edge_label", None)
                        if edge_label != "Calls":
                            continue
                        caller_id = neighbor.node_id
                        if caller_id not in visited:
                            visited.add(caller_id)
                            caller_ids.add(caller_id)
                            next_frontier.add(caller_id)
                except Exception:
                    continue
            frontier = next_frontier

        if not caller_ids:
            return []

        # Fetch file_path and start_line for each caller
        results: list[CallSite] = []
        for caller_id in caller_ids:
            try:
                caller_node = self._db.get_node(caller_id)
                caller_key = getattr(caller_node, "key", "")
                props = getattr(caller_node, "props", {}) or {}
                fp = _decode_or_none(props.get("file_path"))
                line = _opt_int(props.get("start_line"))
                results.append(CallSite(qualified_name=caller_key, file_path=fp, line=line))
            except Exception:
                continue

        results.sort(key=lambda c: (c.qualified_name, c.line or 0))
        return results

    # -- decision / knowledge ------------------------------------------------

    def symbols_by_name(self, name: str) -> list[tuple[str, str]]:
        """Find symbols by short name."""
        results: list[tuple[str, str]] = []
        try:
            rows = self._fetch_rows(
                f"MATCH (n:Symbol) WHERE n.name = '{_esc(name)}' RETURN n.qualified_name, n.kind"
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                kind = _decode(row.get("n.kind"))
                if key and kind and kind != "MarkdownDoc":
                    results.append((key, kind))
        except Exception:
            logger.debug("failed to fetch symbols by name %s", name, exc_info=True)
        return results

    def symbols_by_file(self, file_path: str) -> list[tuple[str, str]]:
        """Find symbols by file path."""
        results: list[tuple[str, str]] = []
        try:
            rows = self._fetch_rows(
                f"MATCH (n:Symbol) WHERE n.file_path = '{_esc(file_path)}' "
                f"RETURN n.qualified_name, n.kind"
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                kind = _decode(row.get("n.kind"))
                if key and kind and kind != "MarkdownDoc":
                    results.append((key, kind))
        except Exception:
            logger.debug("failed to fetch symbols by file %s", file_path, exc_info=True)
        return results

    def decision_qns(self) -> list[str]:
        """List all decision qualified names."""
        results: list[str] = []
        try:
            rows = self._fetch_rows("MATCH (n:Decision) RETURN n.qualified_name")
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                if key:
                    results.append(key)
        except Exception:
            logger.debug("failed to fetch decision QNs", exc_info=True)
        return results

    def governing_decisions(self, fqn: str) -> list[DecisionRow]:
        """Decisions that govern fqn — reverse traversal over Governs edges."""
        fqn = self._resolve_qn(fqn)
        node_id = self._get_node_id(fqn)
        if node_id is None:
            return []

        results: list[DecisionRow] = []
        try:
            neighbors = list(self._db.neighbors(node_id, direction="incoming"))
            for neighbor in neighbors:
                edge_label = getattr(neighbor, "edge_label", None)
                if edge_label != "Governs":
                    continue

                decision_id = neighbor.node_id
                try:
                    decision_node = self._db.get_node(decision_id)
                    decision_key = getattr(decision_node, "key", "")
                    props = getattr(decision_node, "props", {}) or {}

                    results.append(
                        DecisionRow(
                            qualified_name=decision_key,
                            file_path=_decode_or_none(props.get("source_path")),
                            start_line=None,
                            heading=str(props.get("title") or ""),
                            snippet=_decode_or_none(props.get("body")),
                        )
                    )
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch governing decisions for %s", fqn, exc_info=True)

        return results

    def decisions_for_files(self, file_paths: Sequence[str]) -> list[DecisionRow]:
        """Decisions governing symbols in the given files."""
        paths = list(file_paths)
        if not paths:
            return []

        # Find symbols in these files
        sym_qns: list[str] = []
        for path in paths:
            try:
                rows = self._fetch_rows(
                    f"MATCH (n:Symbol) WHERE n.file_path = '{_esc(path)}' RETURN n.qualified_name"
                )
                for row in rows:
                    key = _decode(row.get("n.qualified_name"))
                    if key:
                        sym_qns.append(key)
            except Exception:
                continue

        if not sym_qns:
            return []

        # Find decisions governing these symbols
        results: list[DecisionRow] = []
        seen: set[str] = set()
        for qn in sym_qns:
            node_id = self._get_node_id(qn)
            if node_id is None:
                continue
            try:
                neighbors = list(self._db.neighbors(node_id, direction="incoming"))
                for neighbor in neighbors:
                    edge_label = getattr(neighbor, "edge_label", None)
                    if edge_label != "Governs":
                        continue

                    decision_id = neighbor.node_id
                    try:
                        decision_node = self._db.get_node(decision_id)
                        decision_key = getattr(decision_node, "key", "")
                        if decision_key in seen:
                            continue
                        seen.add(decision_key)

                        props = getattr(decision_node, "props", {}) or {}
                        results.append(
                            DecisionRow(
                                qualified_name=decision_key,
                                file_path=_decode_or_none(props.get("source_path")),
                                start_line=None,
                                heading=str(props.get("title") or ""),
                                snippet=_decode_or_none(props.get("body")),
                            )
                        )
                    except Exception:
                        continue
            except Exception:
                continue

        results.sort(key=lambda d: d.qualified_name)
        return results

    def decision_docs_governing(self, fqns: Sequence[str]) -> list[str]:
        """Document paths that govern the given symbols."""
        qns = list(fqns)
        if not qns:
            return []

        docs: set[str] = set()
        for qn in qns:
            node_id = self._get_node_id(qn)
            if node_id is None:
                continue
            try:
                neighbors = list(self._db.neighbors(node_id, direction="incoming"))
                for neighbor in neighbors:
                    edge_label = getattr(neighbor, "edge_label", None)
                    if edge_label != "Governs":
                        continue

                    decision_id = neighbor.node_id
                    try:
                        decision_node = self._db.get_node(decision_id)
                        props = getattr(decision_node, "props", {}) or {}
                        sp = _decode_or_none(props.get("source_path"))
                        if sp:
                            docs.add(sp)
                    except Exception:
                        continue
            except Exception:
                continue

        return sorted(docs)

    def delete_govern_edges_for_doc(self, doc_path: str) -> int:
        """Delete all Governs edges from decisions with the given source path."""
        count = 0
        try:
            # Find decisions with this source_path
            rows = self._fetch_rows(
                f"MATCH (n:Decision) WHERE n.source_path = '{_esc(doc_path)}' RETURN id(n) AS nid"
            )
            for row in rows:
                decision_id = row.get("nid")
                if decision_id is not None:
                    decision_id = int(decision_id)
                    # Delete outgoing Governs edges
                    try:
                        neighbors = list(self._db.neighbors(decision_id, direction="outgoing"))
                        for neighbor in neighbors:
                            if getattr(neighbor, "edge_label", None) == "Governs":
                                # Delete the edge
                                self._db.execute_gql(
                                    f"MATCH ()-[r:Governs]->() WHERE id(r) = {neighbor.edge_id} DELETE r"
                                )
                                count += 1
                    except Exception:
                        continue
        except Exception:
            logger.debug("failed to delete govern edges for doc %s", doc_path, exc_info=True)
        return count

    def delete_entity_edges_for_docs(self, file_paths: Sequence[str]) -> int:
        """Delete entity edges (Requires, Touches, etc.) for the given files."""
        # Entity edges don't have a direct file_path filter in OverGraph
        # Return 0 as a safe default (same as NebulaGraph implementation)
        logger.debug(
            "delete_entity_edges_for_docs: entity edge types have no file_path "
            "property in OverGraph schema; returning 0"
        )
        return 0

    def count_govern_edges_for_doc(self, doc_path: str) -> int:
        """Count Governs edges from decisions with the given source path."""
        count = 0
        try:
            rows = self._fetch_rows(
                f"MATCH (n:Decision) WHERE n.source_path = '{_esc(doc_path)}' RETURN id(n) AS nid"
            )
            for row in rows:
                decision_id = row.get("nid")
                if decision_id is not None:
                    decision_id = int(decision_id)
                    try:
                        neighbors = list(self._db.neighbors(decision_id, direction="outgoing"))
                        for neighbor in neighbors:
                            if getattr(neighbor, "edge_label", None) == "Governs":
                                count += 1
                    except Exception:
                        continue
        except Exception:
            logger.debug("failed to count govern edges for doc %s", doc_path, exc_info=True)
        return count

    def typed_edges_for_fqn(self, fqn: str) -> list[CodeEdge]:
        """Entity edges (Requires, Touches, etc.) incoming to fqn."""
        fqn = self._resolve_qn(fqn)
        node_id = self._get_node_id(fqn)
        if node_id is None:
            return []

        results: list[CodeEdge] = []
        try:
            for label in _ENTITY_EDGE_LABELS:
                kind = _kind_for_edge_label(label)
                neighbors = list(self._db.neighbors(node_id, direction="incoming"))
                for neighbor in neighbors:
                    if getattr(neighbor, "edge_label", None) != label:
                        continue
                    src_id = neighbor.node_id
                    try:
                        src_node = self._db.get_node(src_id)
                        src_key = getattr(src_node, "key", "")
                        if src_key:
                            results.append(
                                CodeEdge(
                                    src=src_key,
                                    dst=fqn,
                                    kind=kind,
                                    file_path="",
                                    span=None,
                                    resolution_tier=0,
                                )
                            )
                    except Exception:
                        continue
        except Exception:
            logger.debug("failed to fetch typed edges for %s", fqn, exc_info=True)

        results.sort(key=lambda e: (e.kind, e.src))
        return results

    def typed_edges_from_chunks(
        self,
        chunk_qns: Sequence[str],
        *,
        limit: int = 20,
    ) -> list[CodeEdge]:
        """Entity edges outgoing from the given chunks."""
        if not chunk_qns:
            return []

        results: list[CodeEdge] = []
        for label in _ENTITY_EDGE_LABELS:
            kind = _kind_for_edge_label(label)
            for qn in chunk_qns:
                if len(results) >= limit:
                    break
                node_id = self._get_node_id(qn)
                if node_id is None:
                    continue
                try:
                    neighbors = list(self._db.neighbors(node_id, direction="outgoing"))
                    for neighbor in neighbors:
                        if len(results) >= limit:
                            break
                        if getattr(neighbor, "edge_label", None) != label:
                            continue
                        dst_id = neighbor.node_id
                        try:
                            dst_node = self._db.get_node(dst_id)
                            dst_key = getattr(dst_node, "key", "")
                            if dst_key:
                                results.append(
                                    CodeEdge(
                                        src=qn,
                                        dst=dst_key,
                                        kind=kind,
                                        file_path="",
                                        span=None,
                                        resolution_tier=0,
                                    )
                                )
                        except Exception:
                            continue
                except Exception:
                    continue

        results.sort(key=lambda e: (e.kind, e.src))
        return results[:limit]

    # -- aggregates / listings -----------------------------------------------

    def counts_by_kind(self) -> dict[str, int]:
        """Count symbols by kind."""
        counts: dict[str, int] = {}
        try:
            rows = self._fetch_rows("MATCH (n:Symbol) RETURN n.kind")
            for row in rows:
                k = _decode(row.get("n.kind"))
                if k:
                    counts[k] = counts.get(k, 0) + 1
        except Exception:
            logger.debug("failed to fetch counts by kind", exc_info=True)
        return counts

    def list_files(
        self,
        *,
        prefix: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[str]:
        """List files containing symbols."""
        files: set[str] = set()
        try:
            if prefix:
                rows = self._fetch_rows(
                    f"MATCH (n:Symbol) WHERE n.file_path STARTS WITH '{_esc(prefix)}' "
                    f"RETURN n.file_path"
                )
            else:
                rows = self._fetch_rows("MATCH (n:Symbol) RETURN n.file_path")

            for row in rows:
                fp = _decode_or_none(row.get("n.file_path"))
                if fp:
                    files.add(fp)
        except Exception:
            logger.debug("failed to list files", exc_info=True)

        sorted_files = sorted(files)
        return sorted_files[offset : offset + max(1, int(limit))]

    def calls_edges(self) -> list[tuple[str, str]]:
        """All Calls edges as (src, dst) pairs."""
        results: list[tuple[str, str]] = []
        try:
            rows = self._fetch_rows(
                "MATCH (a:Symbol)-[r:Calls]->(b:Symbol) "
                "RETURN a.qualified_name AS src, b.qualified_name AS dst"
            )
            for row in rows:
                src = _decode(row.get("src"))
                dst = _decode(row.get("dst"))
                if src and dst:
                    results.append((src, dst))
        except Exception:
            logger.debug("failed to fetch calls edges", exc_info=True)
        return results

    # -- centrality ----------------------------------------------------------

    def write_centrality(self, scores: Mapping[str, float]) -> int:
        """Write pagerank scores to Symbol nodes."""
        if not scores:
            return 0

        count = 0
        for qn, score in scores.items():
            node_id = self._get_node_id(qn)
            if node_id is None:
                continue
            try:
                # Update the pagerank property
                self._db.execute_gql(
                    f"MATCH (n) WHERE id(n) = {node_id} SET n.pagerank = {float(score)}"
                )
                count += 1
            except Exception:
                logger.debug("failed to write centrality for %s", qn, exc_info=True)
        return count

    def read_centrality(self, qualified_names: Sequence[str]) -> dict[str, float]:
        """Read pagerank scores for the given symbols."""
        qns = list(qualified_names)
        if not qns:
            return {}

        result: dict[str, float] = {}
        for qn in qns:
            node_id = self._get_node_id(qn)
            if node_id is None:
                continue
            try:
                node = self._db.get_node(node_id)
                props = getattr(node, "props", {}) or {}
                pr = props.get("pagerank")
                if pr is not None:
                    result[qn] = float(pr)
            except Exception:
                continue
        return result

    def top_centrality(self, limit: int = 20) -> list[tuple[str, float]]:
        """Return the top symbols by pagerank."""
        scored: list[tuple[str, float]] = []
        try:
            rows = self._fetch_rows("MATCH (n:Symbol) RETURN n.qualified_name, n.pagerank")
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                pr = row.get("n.pagerank")
                if key and pr is not None:
                    scored.append((key, float(pr)))
        except Exception:
            logger.debug("failed to fetch top centrality", exc_info=True)

        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[: max(1, int(limit))]

    # -- incremental-reindex support -----------------------------------------

    def content_hashes(self) -> dict[str, str]:
        """Return content hashes for all symbols."""
        result: dict[str, str] = {}
        try:
            rows = self._fetch_rows("MATCH (n:Symbol) RETURN n.qualified_name, n.content_hash")
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                ch = _decode(row.get("n.content_hash"))
                if key and ch:
                    result[key] = ch
        except Exception:
            logger.debug("failed to fetch content hashes", exc_info=True)
        return result

    # -- repo_meta kv --------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        try:
            self._db.upsert_node(
                labels=["Meta"],
                key=key,
                props={
                    "meta_key": key,
                    "meta_value": value,
                    "updated_at": int(time.time()),
                },
            )
        except Exception:
            logger.warning("failed to set meta %s", key, exc_info=True)

    def get_meta(self, key: str) -> str | None:
        """Get a metadata value by key."""
        try:
            node = self._db.get_node_by_key("Meta", key)
            if node:
                props = getattr(node, "props", {}) or {}
                return _decode(props.get("meta_value"))
        except Exception:
            logger.debug("failed to get meta %s", key, exc_info=True)
        return None

    # =========================================================================
    # CodeVectorStore Protocol Implementation
    # =========================================================================

    def upsert(self, rows: Iterable[CodeVectorRow]) -> int:
        """Upsert vector rows (keyed on qualified_name).

        Uses ``upsert_node(dense_vector=...)`` for every write — this is the
        only path that updates OverGraph's HNSW index. GQL ``SET n.dense_vector``
        updates the node property but leaves the HNSW index stale, causing
        vector_search to return 0 hits or corrupt-record errors.

        After the batch completes, ``db.flush()`` ensures the HNSW segments
        are materialised to the mmap'd files before any similarity query.
        """
        batch = list(rows)
        if not batch:
            return 0

        # Check dimensions
        for r in batch:
            if len(r.embedding) != self._vector_dimension:
                raise EmbeddingDimMismatch(
                    f"qualified_name={r.qualified_name}: embedding has {len(r.embedding)} "
                    f"dimensions, expected {self._vector_dimension}",
                )

        # Use a write transaction for atomic batch ingestion with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                txn = self._db.begin_write_txn()
                for r in batch:
                    normalized_embedding = l2_normalize(r.embedding)
                    # Merge with existing node props so graph data (kind,
                    # name, docstring, etc.) is preserved when the vector
                    # leg writes its properties.
                    existing_props: dict[str, Any] = {}
                    node_id = self._get_node_id(r.qualified_name)
                    if node_id is not None:
                        try:
                            existing_node = self._db.get_node(node_id)
                            existing_props = dict(getattr(existing_node, "props", {}) or {})
                        except Exception:
                            pass
                    vector_props = {
                        "symbol_type": r.symbol_type,
                        "file_path": r.file_path,
                        "start_line": r.start_line,
                        "end_line": r.end_line,
                        "text": r.text or "",
                        "indexed_at": r.indexed_at,
                    }
                    merged = {**existing_props, **vector_props}
                    merged["qualified_name"] = r.qualified_name
                    txn.upsert_node(
                        labels=["Symbol"],
                        key=r.qualified_name,
                        props=merged,
                        dense_vector=normalized_embedding,
                    )
                txn.commit()
                break
            except Exception:
                with contextlib.suppress(Exception):
                    txn.rollback()
                if attempt == max_retries - 1:
                    logger.warning(
                        "failed to upsert vectors after %d attempts", max_retries, exc_info=True
                    )
                    return 0
                logger.debug("vector upsert txn attempt %d failed, retrying", attempt + 1)

        # Flush to materialise HNSW segments for search
        self._db.flush()

        # Rebuild ID mapping for any new nodes
        self._rebuild_id_mapping()

        return len(batch)

    def bulk_replace(self, rows: Iterable[CodeVectorRow]) -> int:
        """Atomically replace the entire vector dataset.

        Delegates to ``upsert()`` which uses ``upsert_node(dense_vector=...)``
        — this overwrites both the node properties and the HNSW index entries
        in a single transactional batch, followed by ``db.flush()``.
        """
        batch = list(rows)

        # Check dimensions
        for r in batch:
            if len(r.embedding) != self._vector_dimension:
                raise EmbeddingDimMismatch(
                    f"qualified_name={r.qualified_name}: embedding has {len(r.embedding)} "
                    f"dimensions, expected {self._vector_dimension}",
                )

        # upsert() handles txn + flush; it overwrites vectors for existing
        # nodes and creates new ones as needed.
        return self.upsert(batch)

    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        k: int = 10,
        where: str | None = None,
    ) -> list[CodeSearchHit]:
        """Top-k cosine similarity search."""
        if len(query_vec) != self._vector_dimension:
            raise EmbeddingDimMismatch(
                f"query vector has {len(query_vec)} dimensions, expected {self._vector_dimension}",
            )

        # Normalize query vector
        normalized_query = l2_normalize(query_vec)

        try:
            # Use OverGraph's vector search
            hits = list(
                self._db.vector_search(
                    mode="dense",
                    k=k,
                    dense_query=normalized_query,
                )
            )

            results: list[CodeSearchHit] = []
            for hit in hits:
                node_id = hit.node_id
                score = float(hit.score)  # OverGraph returns cosine similarity directly

                try:
                    node = self._db.get_node(node_id)
                    props = getattr(node, "props", {}) or {}
                    key = getattr(node, "key", "")

                    results.append(
                        CodeSearchHit(
                            qualified_name=key,
                            file_path=str(props.get("file_path") or ""),
                            start_line=_opt_int(props.get("start_line")),
                            end_line=_opt_int(props.get("end_line")),
                            score=score,
                        )
                    )
                except Exception:
                    continue

            return results
        except Exception:
            logger.debug("failed to perform vector search", exc_info=True)
            return []

    def search_bm25(
        self,
        query: str,
        *,
        k: int = 10,
        where: str | None = None,
    ) -> list[tuple[str, float]]:
        """BM25 full-text search over the text field."""
        if not query.strip():
            return []

        # OverGraph doesn't have built-in FTS, so we use GQL with LIKE
        # This is a simplified implementation; a real FTS would need a separate index
        results: list[tuple[str, float]] = []
        try:
            # Simple substring match (not true BM25)
            rows = self._fetch_rows(
                f"MATCH (n:Symbol) WHERE n.text CONTAINS '{_esc(query)}' "
                f"RETURN n.qualified_name, n.text LIMIT {k}"
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                text = _decode(row.get("n.text"))
                if key:
                    score = float(text.lower().count(query.lower())) if text else 0.0
                    results.append((key, score))
        except Exception:
            logger.debug("failed to perform BM25 search", exc_info=True)

        return results

    def delete(self, qualified_names: Sequence[str]) -> int:
        """Delete vectors by qualified name."""
        qns = list(qualified_names)
        if not qns:
            return 0

        count = 0
        for qn in qns:
            node_id = self._get_node_id(qn)
            if node_id is None:
                continue
            try:
                # Delete the node (which includes its vector)
                self._db.execute_gql(f"MATCH (n) WHERE id(n) = {node_id} DETACH DELETE n")
                self._qn_to_id.pop(qn, None)
                count += 1
            except Exception:
                logger.debug("failed to delete vector for %s", qn, exc_info=True)

        return count

    def count(self) -> int:
        """Count the number of vectors (Symbol nodes with indexed_at set)."""
        try:
            rows = self._fetch_rows(
                "MATCH (n:Symbol) WHERE n.indexed_at IS NOT NULL RETURN count(n) AS cnt"
            )
            if rows:
                return int(rows[0].get("cnt", 0))
        except Exception:
            logger.debug("failed to count vectors", exc_info=True)
        return 0

    def embedding_dim(self) -> int | None:
        """Return the embedding dimension, or None if empty."""
        return None if self.count() == 0 else self._vector_dimension

    def rebuild_fts_index(self) -> None:
        """Rebuild the full-text search index (no-op for OverGraph)."""
        # OverGraph doesn't have a separate FTS index to rebuild
        # This is a no-op to satisfy the protocol
        logger.debug("rebuild_fts_index: no-op for OverGraph")
