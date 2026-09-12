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
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agentalloy.code_index.protocols import (
    EMBEDDING_DIM,
    CallSite,
    CodeEdge,
    CodeSearchHit,
    CodeSymbol,
    CodeVectorRow,
    DecisionRow,
    EmbeddingDimMismatchError,
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

# Label for anchor nodes standing in for edge endpoints that were never
# indexed as symbols (dangling call sources/targets).
_DANGLING_LABEL = "Dangling"

# Anchor key for standalone entity edges (COMMAND / STAKEHOLDER) whose dst is
# deliberately empty — reads map this anchor back to "".
_STANDALONE_ANCHOR = "<standalone>"

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


def _parse_where_qn_set(where: str | None) -> set[str] | None:
    """Minimal evaluator for the retrieval layer's ``where`` clause shape:
    ``qualified_name IN ('a', 'b', ...)``. Returns the permitted qn set, or
    None when the clause is absent/unrecognised (no filtering)."""
    if not where:
        return None
    m = re.match(r"^\s*qualified_name\s+IN\s*\((.*)\)\s*$", where, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    items = re.findall(r"'((?:[^']|'')*)'", m.group(1))
    return {i.replace("''", "'") for i in items}


def _decision_row_from_src(key: str, props: dict[str, Any]) -> DecisionRow:
    """DecisionRow off a GOVERNS edge's source node — a MarkdownDoc symbol:
    file_path/start_line/name/source_code carry the doc location, the heading
    and the snippet."""
    return DecisionRow(
        qualified_name=key,
        file_path=_decode_or_none(props.get("file_path")),
        start_line=_opt_int(props.get("start_line")),
        heading=str(props.get("name") or ""),
        snippet=_decode_or_none(props.get("source_code")),
    )


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
                    raise EmbeddingDimMismatchError(
                        f"OverGraph database at {self._db_path} was created with "
                        f"dense_vector_dimension={actual}, but the current embedding "
                        f"model requires {expected_dim}. Delete the OverGraph directory "
                        f"and re-index from scratch."
                    )
                logger.debug("vector dimension alignment verified: %d", actual)
        except EmbeddingDimMismatchError:
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

    def _fetch_all_rows(
        self, match_clause: str, return_clause: str, order_by: str, page_size: int = 10_000
    ) -> list[dict[str, Any]]:
        """Full-scan read via stable ``ORDER BY ... SKIP/LIMIT`` pagination.

        The GQL engine silently caps every row fetch at 10,000 rows (even an
        explicit ``LIMIT 20000`` returns 10,000), so unbounded full scans
        must page. ``order_by`` must be a stable total order over the matched
        rows — ``n.qualified_name`` for symbols (the node key, unique), and
        both endpoint qnames for edges (identical parallel-edge rows dedupe
        downstream, so the tie is harmless).
        """
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            stmt = (
                f"{match_clause} {return_clause} "
                f"ORDER BY {order_by} SKIP {offset} LIMIT {page_size}"
            )
            page = self._fetch_rows(stmt)
            out.extend(page)
            if len(page) < page_size:
                return out
            offset += len(page)

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

    def _any_node_id(self, qualified_name: str) -> int | None:
        """Node id across Symbol and dangling anchors (read path)."""
        node_id = self._get_node_id(qualified_name)
        if node_id is not None:
            return node_id
        try:
            node = self._db.get_node_by_key(_DANGLING_LABEL, qualified_name)
            nid = getattr(node, "id", None)
            if nid is not None:
                return int(nid)
        except Exception:
            pass
        return None

    def _ensure_endpoint_id(self, qualified_name: str, edge_file_path: str) -> int | None:
        """Node id for an edge endpoint, creating a dangling anchor when the
        symbol was never indexed (edges may legitimately point outside the
        indexed set)."""
        if qualified_name == "":
            qualified_name = _STANDALONE_ANCHOR
        node_id = self._get_node_id(qualified_name)
        if node_id is not None:
            return node_id
        try:
            node = self._db.get_node_by_key(_DANGLING_LABEL, qualified_name)
            if node is not None:
                nid = getattr(node, "id", None)
                if nid is not None:
                    return int(nid)
            return int(
                self._db.upsert_node(
                    labels=[_DANGLING_LABEL],
                    key=qualified_name,
                    props={
                        "qualified_name": qualified_name,
                        "name": qualified_name.rsplit(".", 1)[-1],
                        "file_path": edge_file_path or "",
                    },
                )
            )
        except Exception:
            logger.debug("failed to ensure endpoint node for %s", qualified_name, exc_info=True)
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
        # Anchor nodes for edge endpoints that were never indexed as symbols
        # (dangling call targets/sources). Not Symbols: symbol() misses them.
        self._db.ensure_node_label(_DANGLING_LABEL)

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
        """Tolerant FQN resolution — exact match first, then suffix lookup.

        An ambiguous suffix (more than one symbol shares the name) is a miss:
        the input is returned unchanged rather than guessing between matches.
        """
        if self._node_exists(fqn):
            return fqn
        # Try suffix match via GQL
        short_name = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
        try:
            result = self._db.execute_gql(
                f"MATCH (n:Symbol) WHERE n.name = '{_esc(short_name)}' "
                "RETURN n.qualified_name LIMIT 2"
            )
            if isinstance(result, dict):
                rows = result.get("rows") or []
                if len(rows) == 1:
                    key = _decode(rows[0].get("n.qualified_name"))
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
            # Stored comma-joined (see _symbol_props) — split back out.
            decorators = [d for d in decos_raw.split(",") if d]
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
            repo=str(get("repo") or ""),
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
            "repo": s.repo or "",
            # Absent until write_centrality stamps it (read paths treat None
            # as "no score", not zero).
            "pagerank": None,
            # NOTE: indexed_at/text/dense_vector are deliberately NOT set
            # here — a symbol re-upsert must not wipe its vector membership
            # (the unified node carries both the graph row and its embedding).
        }

    def _existing_symbol_props(self, qualified_name: str) -> dict[str, Any]:
        """Props of an existing node for this qn (Symbol or dangling anchor),
        used as the merge base so upserts don't wipe vector membership."""
        try:
            node_id = self._any_node_id(qualified_name)
            if node_id is not None:
                node = self._db.get_node(node_id)
                return dict(getattr(node, "props", {}) or {})
        except Exception:
            pass
        return {}

    def _upsert_symbol_node(self, s: CodeSymbol) -> int:
        """Upsert a single Symbol node and return its ID."""
        props = {**self._existing_symbol_props(s.qualified_name), **self._symbol_props(s)}
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

        # Prepare batch data. Merge over the existing node's props (upsert
        # replaces them wholesale): the unified node may already carry vector
        # membership (indexed_at/text) that a symbol re-upsert must preserve.
        batch_data = []
        for s in symbols:
            props = {**self._existing_symbol_props(s.qualified_name), **self._symbol_props(s)}
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
        src_id = self._ensure_endpoint_id(e.src, e.file_path or "")
        dst_id = self._ensure_endpoint_id(e.dst, e.file_path or "")
        if src_id is None or dst_id is None:
            return None

        label = _edge_label_for(e.kind)
        props: dict[str, Any] = {
            "confidence": e.confidence,
            "resolved_via": e.resolved_via,
            "file_path": e.file_path or "",
            "line_start": e.line_start,
            "span": e.span or "",
            "repo": e.repo or "",
        }

        if label == "Governs":
            props = {
                "resolution_tier": e.resolution_tier or 0,
                "span": e.span or "",
                "file_path": e.file_path or "",
                "repo": e.repo or "",
            }

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
            src_id = self._ensure_endpoint_id(e.src, e.file_path or "")
            dst_id = self._ensure_endpoint_id(e.dst, e.file_path or "")
            if src_id is None or dst_id is None:
                continue

            label = _edge_label_for(e.kind)
            props: dict[str, Any] = {
                "confidence": e.confidence,
                "resolved_via": e.resolved_via,
                "file_path": e.file_path or "",
                "line_start": e.line_start,
                "span": e.span or "",
                "repo": e.repo or "",
                "resolution_tier": e.resolution_tier or 0,
            }

            if label == "Governs":
                props = {
                    "resolution_tier": e.resolution_tier or 0,
                    "span": e.span or "",
                    "file_path": e.file_path or "",
                    "repo": e.repo or "",
                }

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

    def _delete_edges_matching(self, match_where: str) -> int:
        """Drain-delete every edge matched by ``match_where`` (a full
        ``MATCH ()-[r:...]->() WHERE ...`` clause binding ``r``).

        The GQL engine silently caps row fetches at 10,000, so a single
        id-scan misses edges beyond the cap; delete a page, re-scan, repeat
        until the scan is empty (or a pass deletes nothing, so persistent
        failures can't loop forever).
        """
        removed = 0
        while True:
            rows = self._fetch_rows(f"{match_where} RETURN id(r) AS rid LIMIT 10000")
            rids = [row.get("rid") for row in rows if row.get("rid") is not None]
            if not rids:
                return removed
            deleted_this_pass = 0
            for rid in rids:
                try:
                    self._db.delete_edge(int(rid))
                    deleted_this_pass += 1
                except Exception:
                    logger.debug("failed to delete edge %s", rid, exc_info=True)
            removed += deleted_this_pass
            if deleted_this_pass == 0 or len(rows) < 10_000:
                return removed

    def delete_for_files(self, file_paths: Sequence[str]) -> int:
        """Delete the symbols and edges indexed from the given files.

        Mirrors the relational model this store replaces: symbol rows are
        removed by file, and edge rows by their OWN file — an edge whose own
        file is kept survives even when an endpoint symbol is deleted (the
        anchor node is tombstoned: its Symbol label is dropped, so symbol()
        misses it, but the edge stays traversable/listable). Returns the
        number of symbols + edges removed.
        """
        paths = list(file_paths)
        if not paths:
            return 0

        removed_edges = 0
        removed_symbols = 0
        for path in paths:
            # Edges indexed from this file (their own file_path property).
            # GOVERNS is exempt: its lifecycle is doc-scoped
            # (delete_govern_edges_for_doc), and stale govern edges must
            # survive symbol churn so the rename re-derive flow can see them.
            for label in _ALL_EDGE_LABELS:
                if label == "Governs":
                    continue
                try:
                    removed_edges += self._delete_edges_matching(
                        f"MATCH ()-[r:{label}]->() WHERE r.file_path = '{_esc(path)}'"
                    )
                except Exception:
                    logger.debug("failed to scan %s edges for file %s", label, path, exc_info=True)

            # Symbols indexed from this file — tombstone, don't detach.
            # Drain-loop around the engine's silent 10k row cap: each
            # tombstone removes the Symbol label, so the re-scan shrinks.
            try:
                while True:
                    result = self._db.execute_gql(
                        f"MATCH (n:Symbol) WHERE n.file_path = '{_esc(path)}' "
                        "RETURN n.qualified_name, id(n) LIMIT 10000"
                    )
                    rows = result.get("rows", []) if isinstance(result, dict) else []
                    tombstoned_this_pass = 0
                    for row in rows:
                        key = _decode(row.get("n.qualified_name"))
                        node_id = row.get("id(n)")
                        if node_id is None:
                            continue
                        try:
                            # Nodes must keep at least one label — swap
                            # Symbol for the dangling anchor label.
                            self._db.add_node_label(int(node_id), _DANGLING_LABEL)
                            self._db.remove_node_label(int(node_id), "Symbol")
                            # Clear the vector marker: the HNSW entry may
                            # outlive the label change, and search_similar
                            # filters stale hits on indexed_at IS None.
                            self._db.execute_gql(
                                f"MATCH (n) WHERE id(n) = {int(node_id)} SET n.indexed_at = NULL"
                            )
                            removed_symbols += 1
                            tombstoned_this_pass += 1
                        except Exception:
                            logger.debug("failed to tombstone %s", key, exc_info=True)
                        if key:
                            self._qn_to_id.pop(key, None)
                    if not rows or tombstoned_this_pass == 0 or len(rows) < 10_000:
                        break
            except Exception:
                logger.warning("failed to delete symbols for file %s", path, exc_info=True)

        if removed_edges or removed_symbols:
            self._db.flush()
        return removed_symbols + removed_edges

    def delete_for_repo(self, repo: str) -> int:
        """Delete every code-index row owned by ``repo``.

        Full-reindex path: the caller re-parses the repo from scratch. All
        edge labels are swept on the edge's own ``repo`` property (GOVERNS
        included — its doc rows are re-ingested with the repo), then every
        Symbol node of the repo (code symbols and MarkdownDoc chunks alike)
        is DETACH-deleted. Cross-repo edges anchored on this repo's symbols
        go with them and are re-observed on the other repos' next ingest.

        Dangling anchors carry no ``repo`` property (they name unindexed
        externals shared across repos) and are left alone. Returns symbols +
        edges removed.
        """
        removed_edges = 0
        for label in _ALL_EDGE_LABELS:
            try:
                result = self._db.execute_gql(
                    f"MATCH ()-[r:{label}]->() WHERE r.repo = '{_esc(repo)}' RETURN count(r) AS cnt"
                )
                if isinstance(result, dict):
                    for row in result.get("rows", []):
                        removed_edges += int(_opt_int(row.get("cnt")) or 0)
            except Exception:
                logger.debug("failed to count %s edges for repo %s", label, repo, exc_info=True)
            try:
                self._db.execute_gql(
                    f"MATCH ()-[r:{label}]->() WHERE r.repo = '{_esc(repo)}' DELETE r"
                )
            except Exception:
                logger.debug("failed to delete %s edges for repo %s", label, repo, exc_info=True)

        stale_qns: list[str] = []
        try:
            # Paginated: a >10k-symbol repo would otherwise leave stale
            # entries in _qn_to_id that later resolve to deleted node ids.
            rows = self._fetch_all_rows(
                f"MATCH (n:Symbol) WHERE n.repo = '{_esc(repo)}'",
                "RETURN n.qualified_name",
                "n.qualified_name",
            )
            stale_qns = [k for k in (_decode(r.get("n.qualified_name")) for r in rows) if k]
        except Exception:
            logger.debug("failed to list symbols for repo %s", repo, exc_info=True)
        try:
            self._db.execute_gql(f"MATCH (n:Symbol) WHERE n.repo = '{_esc(repo)}' DETACH DELETE n")
        except Exception:
            logger.warning("failed to delete symbols for repo %s", repo, exc_info=True)
        for key in stale_qns:
            self._qn_to_id.pop(key, None)

        if removed_edges or stale_qns:
            self._db.flush()
        return removed_edges + len(stale_qns)

    def repo_symbol_count(self, repo: str) -> int:
        """Count Symbol nodes owned by ``repo`` (0 for unknown/empty repos)."""
        try:
            rows = self._fetch_rows(
                f"MATCH (n:Symbol) WHERE n.repo = '{_esc(repo)}' RETURN count(n) AS cnt"
            )
            if rows:
                return int(_opt_int(rows[0].get("cnt")) or 0)
        except Exception:
            logger.debug("failed to count symbols for repo %s", repo, exc_info=True)
        return 0

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
                edge_label = getattr(neighbor, "label", None)
                if edge_label != "Calls":
                    continue

                caller_id = neighbor.node_id
                try:
                    caller_node = self._db.get_node(caller_id)
                    caller_key = getattr(caller_node, "key", "")
                    if not caller_key or caller_key in seen:
                        continue
                    seen.add(caller_key)

                    # Call-site location comes off the edge; the file falls
                    # back to the caller symbol (edges may carry no file, and
                    # dangling callers carry none at all).
                    edge = self._db.get_edge(getattr(neighbor, "edge_id", -1))
                    eprops = getattr(edge, "props", {}) or {} if edge else {}
                    line = _opt_int(eprops.get("line_start"))
                    fp = _decode_or_none(eprops.get("file_path"))
                    if not fp:
                        nprops = getattr(caller_node, "props", {}) or {}
                        fp = _decode_or_none(nprops.get("file_path"))

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
                edge_label = getattr(neighbor, "label", None)
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
                        edge_label = getattr(neighbor, "label", None)
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

    def symbols_matching(self, pattern: str, *, limit: int = 500) -> list[CodeSymbol]:
        """Substring match on qualified name / short name (the ``symbols``
        tool contract). Empty ``pattern`` returns everything, capped and
        name-sorted. Lightweight rows — no source code.
        """
        pattern = (pattern or "").strip()
        where = ""
        if pattern:
            where = (
                f" AND (n.qualified_name CONTAINS '{_esc(pattern)}' "
                f"OR n.name CONTAINS '{_esc(pattern)}')"
            )
        rows = self._fetch_rows(
            f"MATCH (n:Symbol) WHERE n.kind IS NOT NULL{where} "
            "RETURN n.qualified_name, n.kind, n.name, n.file_path, "
            "n.start_line, n.end_line, n.docstring, n.repo"
        )
        out: list[CodeSymbol] = []
        for row in rows:
            qn = _decode(row.get("n.qualified_name"))
            kind = _decode(row.get("n.kind"))
            if not qn or not kind or kind == "MarkdownDoc":
                continue
            out.append(
                CodeSymbol(
                    qualified_name=qn,
                    kind=kind,
                    name=_decode(row.get("n.name")) or qn.rsplit(".", 1)[-1],
                    file_path=_decode_or_none(row.get("n.file_path")),
                    start_line=_opt_int(row.get("n.start_line")),
                    end_line=_opt_int(row.get("n.end_line")),
                    docstring=_decode_or_none(row.get("n.docstring")),
                    decorators=[],
                    is_exported=None,
                    is_async=False,
                    is_generator=False,
                    source_code=None,
                    repo=_decode(row.get("n.repo")) or "",
                )
            )
        out.sort(key=lambda s: s.qualified_name)
        return out[: max(1, int(limit))]

    def decision_qns(self) -> list[str]:
        """List all decision qualified names — decision docs are indexed as
        MarkdownDoc symbols, sorted for stable ``where`` clause building."""
        results: list[str] = []
        try:
            rows = self._fetch_rows(
                "MATCH (n:Symbol) WHERE n.kind = 'MarkdownDoc' RETURN n.qualified_name"
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                if key:
                    results.append(key)
        except Exception:
            logger.debug("failed to fetch decision QNs", exc_info=True)
        return sorted(results)

    def governing_decisions(self, fqn: str) -> list[DecisionRow]:
        """Decisions that govern fqn — reverse traversal over Governs edges.

        Tombstone-aware: a renamed-away symbol's stale edges still surface
        (that is how the rename re-derive flow detects them). Exact match
        first — suffix resolution must not remap a tombstoned qn onto its
        renamed sibling."""
        node_id = self._any_node_id(fqn)
        if node_id is None:
            fqn = self._resolve_qn(fqn)
            node_id = self._any_node_id(fqn)
        if node_id is None:
            return []

        results: list[DecisionRow] = []
        try:
            neighbors = list(self._db.neighbors(node_id, direction="incoming"))
            for neighbor in neighbors:
                edge_label = getattr(neighbor, "label", None)
                if edge_label != "Governs":
                    continue

                decision_id = neighbor.node_id
                try:
                    decision_node = self._db.get_node(decision_id)
                    decision_key = getattr(decision_node, "key", "")
                    props = getattr(decision_node, "props", {}) or {}
                    results.append(_decision_row_from_src(decision_key, props))
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch governing decisions for %s", fqn, exc_info=True)

        return results

    def governs_edges_for_symbol(self, fqn: str) -> list[CodeEdge]:
        """Incoming GOVERNS edges to fqn with their provenance (span +
        resolution tier + decision-doc file path) intact."""
        node_id = self._any_node_id(fqn)
        if node_id is None:
            fqn = self._resolve_qn(fqn)
            node_id = self._any_node_id(fqn)
        if node_id is None:
            return []

        results: list[CodeEdge] = []
        try:
            neighbors = list(self._db.neighbors(node_id, direction="incoming"))
            for neighbor in neighbors:
                if getattr(neighbor, "label", None) != "Governs":
                    continue
                try:
                    src_node = self._db.get_node(neighbor.node_id)
                    src_key = getattr(src_node, "key", "")
                    edge = self._db.get_edge(getattr(neighbor, "edge_id", -1))
                    eprops = getattr(edge, "props", {}) or {} if edge else {}
                    results.append(
                        CodeEdge(
                            src=src_key,
                            dst=fqn,
                            kind="GOVERNS",
                            file_path=_decode_or_none(eprops.get("file_path")) or "",
                            span=_decode_or_none(eprops.get("span")),
                            resolution_tier=_opt_int(eprops.get("resolution_tier")) or 0,
                        )
                    )
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch governs edges for %s", fqn, exc_info=True)
        results.sort(key=lambda e: (e.src, e.dst))
        return results

    def governs_edges_from(self, fqn: str) -> list[CodeEdge]:
        """Outgoing GOVERNS edges from fqn (a decision chunk's declared
        governance targets), provenance intact. Mirror of
        :meth:`governs_edges_for_symbol` on the other direction."""
        node_id = self._any_node_id(fqn)
        if node_id is None:
            fqn = self._resolve_qn(fqn)
            node_id = self._any_node_id(fqn)
        if node_id is None:
            return []

        results: list[CodeEdge] = []
        try:
            neighbors = list(self._db.neighbors(node_id, direction="outgoing"))
            for neighbor in neighbors:
                if getattr(neighbor, "label", None) != "Governs":
                    continue
                try:
                    dst_node = self._db.get_node(neighbor.node_id)
                    dst_key = getattr(dst_node, "key", "")
                    edge = self._db.get_edge(getattr(neighbor, "edge_id", -1))
                    eprops = getattr(edge, "props", {}) or {} if edge else {}
                    results.append(
                        CodeEdge(
                            src=fqn,
                            dst=dst_key,
                            kind="GOVERNS",
                            file_path=_decode_or_none(eprops.get("file_path")) or "",
                            span=_decode_or_none(eprops.get("span")),
                            resolution_tier=_opt_int(eprops.get("resolution_tier")) or 0,
                        )
                    )
                except Exception:
                    continue
        except Exception:
            logger.debug("failed to fetch outgoing governs edges for %s", fqn, exc_info=True)
        results.sort(key=lambda e: (e.src, e.dst))
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
                    edge_label = getattr(neighbor, "label", None)
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
                        results.append(_decision_row_from_src(decision_key, props))
                    except Exception:
                        continue
            except Exception:
                continue

        results.sort(key=lambda d: d.qualified_name)
        return results

    def decision_docs_governing(self, fqns: Sequence[str]) -> list[str]:
        """Document paths that govern the given symbols.

        Tombstone-aware on purpose: this is the rename-detection query (a
        renamed-away fqn must NOT require a live Symbol join)."""
        qns = list(fqns)
        if not qns:
            return []

        docs: set[str] = set()
        for qn in qns:
            node_id = self._any_node_id(qn)
            if node_id is None:
                continue
            try:
                neighbors = list(self._db.neighbors(node_id, direction="incoming"))
                for neighbor in neighbors:
                    edge_label = getattr(neighbor, "label", None)
                    if edge_label != "Governs":
                        continue

                    decision_id = neighbor.node_id
                    try:
                        decision_node = self._db.get_node(decision_id)
                        props = getattr(decision_node, "props", {}) or {}
                        sp = _decode_or_none(props.get("file_path"))
                        if sp:
                            docs.add(sp)
                    except Exception:
                        continue
            except Exception:
                continue

        return sorted(docs)

    def delete_govern_edges_for_doc(self, doc_path: str) -> int:
        """Delete all Governs edges whose source (decision doc) lives in the
        given file. Scoped: other docs' GOVERNS edges and every non-Governs
        edge are untouched."""
        count = 0
        try:
            count = self._delete_edges_matching(
                f"MATCH (a)-[r:Governs]->() WHERE a.file_path = '{_esc(doc_path)}'"
            )
            if count:
                self._db.flush()
        except Exception:
            logger.debug("failed to delete govern edges for doc %s", doc_path, exc_info=True)
        return count

    def delete_entity_edges_for_docs(self, file_paths: Sequence[str]) -> int:
        """Delete entity edges (Requires, Touches, etc.) indexed from the
        given doc files. Entity edges carry the doc path on the edge itself,
        and GOVERNS edges are out of scope (they belong to the decision
        phase)."""
        removed = 0
        for path in file_paths:
            for label in _ENTITY_EDGE_LABELS:
                try:
                    removed += self._delete_edges_matching(
                        f"MATCH ()-[r:{label}]->() WHERE r.file_path = '{_esc(path)}'"
                    )
                except Exception:
                    logger.debug("failed to scan %s edges for doc %s", label, path, exc_info=True)
        if removed:
            self._db.flush()
        return removed

    def count_govern_edges_for_doc(self, doc_path: str) -> int:
        """Count Governs edges whose source (decision doc) lives in the given
        file. Non-destructive read."""
        try:
            rows = self._fetch_rows(
                f"MATCH (a)-[r:Governs]->() WHERE a.file_path = '{_esc(doc_path)}' "
                "RETURN id(r) AS rid"
            )
            return len(rows)
        except Exception:
            logger.debug("failed to count govern edges for doc %s", doc_path, exc_info=True)
            return 0

    def typed_edges_for_fqn(self, fqn: str) -> list[CodeEdge]:
        """Entity edges (Requires, Touches, etc.) incoming to fqn."""
        node_id = self._any_node_id(fqn)
        if node_id is None:
            fqn = self._resolve_qn(fqn)
            node_id = self._any_node_id(fqn)
        if node_id is None:
            return []

        results: list[CodeEdge] = []
        try:
            for label in _ENTITY_EDGE_LABELS:
                kind = _kind_for_edge_label(label)
                neighbors = list(self._db.neighbors(node_id, direction="incoming"))
                for neighbor in neighbors:
                    if getattr(neighbor, "label", None) != label:
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
                node_id = self._any_node_id(qn)
                if node_id is None:
                    continue
                try:
                    neighbors = list(self._db.neighbors(node_id, direction="outgoing"))
                    for neighbor in neighbors:
                        if len(results) >= limit:
                            break
                        if getattr(neighbor, "label", None) != label:
                            continue
                        dst_id = neighbor.node_id
                        try:
                            dst_node = self._db.get_node(dst_id)
                            dst_key = getattr(dst_node, "key", "")
                            if dst_key == _STANDALONE_ANCHOR:
                                dst_key = ""
                            edge = self._db.get_edge(getattr(neighbor, "edge_id", -1))
                            eprops = getattr(edge, "props", {}) or {} if edge else {}
                            results.append(
                                CodeEdge(
                                    src=qn,
                                    dst=dst_key,
                                    kind=kind,
                                    file_path=_decode_or_none(eprops.get("file_path")) or "",
                                    span=_decode_or_none(eprops.get("span")),
                                    resolution_tier=_opt_int(eprops.get("resolution_tier")) or 0,
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
        """Count symbols by kind (GQL group-by aggregate — immune to row caps)."""
        counts: dict[str, int] = {}
        try:
            rows = self._fetch_rows("MATCH (n:Symbol) RETURN n.kind, count(n) AS c")
            for row in rows:
                k = _decode(row.get("n.kind"))
                c = row.get("c")
                if k and c is not None:
                    counts[k] = int(c)
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
                rows = self._fetch_all_rows(
                    f"MATCH (n:Symbol) WHERE n.file_path STARTS WITH '{_esc(prefix)}'",
                    "RETURN n.file_path",
                    "n.file_path",
                )
            else:
                rows = self._fetch_all_rows("MATCH (n:Symbol)", "RETURN n.file_path", "n.file_path")

            for row in rows:
                fp = _decode_or_none(row.get("n.file_path"))
                if fp:
                    files.add(fp)
        except Exception:
            logger.debug("failed to list files", exc_info=True)

        sorted_files = sorted(files)
        return sorted_files[offset : offset + max(1, int(limit))]

    def calls_edges(self) -> list[tuple[str, str]]:
        """All Calls edges as (src, dst) pairs.

        Endpoints are matched label-free so edges anchored on tombstoned or
        dangling nodes still surface (edges are independent rows in the
        relational model this replaces).
        """
        results: list[tuple[str, str]] = []
        try:
            rows = self._fetch_all_rows(
                "MATCH (a)-[r:Calls]->(b)",
                "RETURN a.qualified_name AS src, b.qualified_name AS dst",
                "a.qualified_name, b.qualified_name",
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
        """Replace all pagerank scores (symbols absent from ``scores`` lose
        any prior score)."""
        try:
            # Replace semantics: clear every existing score first.
            self._db.execute_gql("MATCH (n:Symbol) SET n.pagerank = NULL")
        except Exception:
            logger.debug("failed to clear prior centrality scores", exc_info=True)
        if not scores:
            self._db.flush()
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
        self._db.flush()
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
            rows = self._fetch_rows(
                "MATCH (n:Symbol) WHERE n.pagerank IS NOT NULL "
                "RETURN n.qualified_name, n.pagerank "
                f"ORDER BY n.pagerank DESC LIMIT {max(1, int(limit))}"
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                pr = row.get("n.pagerank")
                if key and pr is not None:
                    scored.append((key, float(pr)))
        except Exception:
            logger.debug("failed to fetch top centrality", exc_info=True)
        return scored

    def subgraph(
        self,
        seeds: Sequence[str],
        *,
        hops: int = 1,
        limit: int = 20,
        repo: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """N-hop undirected neighborhood around seed qualified names.

        BFS over both edge directions via the native ``neighbors`` API (the
        GQL layer here has no variable-length path syntax). Seeds are
        resolved tolerantly (exact, then unique short-name suffix) and count
        as hop distance 0. Returns ``(nodes, relationships)`` as wire-ready
        dicts: nodes carry id/qname/kind/file/repo/centrality/hop_distance;
        relationships carry source/target qnames, the DuckDB-style edge
        ``type``, confidence and file. Nodes are capped at
        ``min(limit * 4, 200)`` before the final ``limit`` truncation so a
        hub seed cannot blow up the query.
        """
        hops = max(1, min(int(hops), 3))
        limit = max(1, int(limit))
        node_cap = min(max(limit * 4, 20), 200)

        nodes: dict[int, dict[str, Any]] = {}
        relationships: list[dict[str, Any]] = []
        rel_seen: set[tuple[str, str, str]] = set()

        def add_node(nid: int, hop: int) -> bool:
            if nid in nodes or len(nodes) >= node_cap:
                return nid in nodes
            try:
                node = self._db.get_node(nid)
            except Exception:
                return False
            props = getattr(node, "props", {}) or {}
            key = _decode(getattr(node, "key", "") or "")
            if not key:
                return False
            pr = props.get("pagerank")
            nodes[nid] = {
                "id": nid,
                "qname": key,
                "kind": str(props.get("kind") or ""),
                "file": _decode_or_none(props.get("file_path")) or "",
                "repo": str(props.get("repo") or ""),
                "centrality": float(pr) if pr is not None else None,
                "hop_distance": hop,
            }
            return True

        def add_relation(src_qn: str, dst_qn: str, edge_id: int, label: str) -> None:
            edge_type = _kind_for_edge_label(label)
            dedup = (src_qn, dst_qn, edge_type)
            if dedup in rel_seen:
                return
            rel_seen.add(dedup)
            confidence = 1.0
            file_path = ""
            try:
                edge = self._db.get_edge(edge_id)
                eprops = getattr(edge, "props", {}) or {}
                c = eprops.get("confidence")
                if c is not None:
                    confidence = float(c)
                file_path = _decode_or_none(eprops.get("file_path")) or ""
            except Exception:
                pass
            relationships.append(
                {
                    "source": src_qn,
                    "target": dst_qn,
                    "type": edge_type,
                    "confidence": confidence,
                    "file": file_path,
                }
            )

        frontier: list[int] = []
        for seed in seeds:
            if not seed:
                continue
            qn = self._resolve_qn(seed)
            nid = self._get_node_id(qn)
            if nid is None:
                nid = self._any_node_id(qn)
            if nid is None or nid in nodes:
                continue
            if add_node(int(nid), 0):
                frontier.append(int(nid))

        for hop in range(1, hops + 1):
            if not frontier:
                break
            next_frontier: list[int] = []
            for nid in frontier:
                src_node = self._db.get_node(nid)
                src_qn = _decode(getattr(src_node, "key", "") or "")
                for direction in ("outgoing", "incoming"):
                    try:
                        neighbors = list(self._db.neighbors(nid, direction=direction))
                    except Exception:
                        logger.debug("neighbors(%s) failed", direction, exc_info=True)
                        continue
                    for nb in neighbors:
                        edge_label = getattr(nb, "label", None)
                        other_id = getattr(nb, "node_id", None)
                        edge_id = getattr(nb, "edge_id", None)
                        if not edge_label or other_id is None or edge_id is None:
                            continue
                        other_id = int(other_id)
                        other_node = self._db.get_node(other_id)
                        other_qn = _decode(getattr(other_node, "key", "") or "")
                        if not other_qn:
                            continue
                        # Direction-independent: source is always the BFS
                        # parent, target the neighbor traversed to.
                        add_relation(src_qn, other_qn, int(edge_id), str(edge_label))
                        if other_id not in nodes and add_node(other_id, hop):
                            next_frontier.append(other_id)
            frontier = next_frontier

        # Repo filter (nodes), then trim relationships to the kept node set.
        if repo:
            kept = {nid for nid, n in nodes.items() if n["repo"] == repo}
            nodes = {nid: n for nid, n in nodes.items() if nid in kept}
            kept_qns = {n["qname"] for n in nodes.values()}
            relationships = [
                r for r in relationships if r["source"] in kept_qns and r["target"] in kept_qns
            ]

        # Sort: hop distance, then centrality (missing last), then qname.
        ordered = sorted(
            nodes.values(),
            key=lambda n: (n["hop_distance"], -(n["centrality"] or 0.0), n["qname"]),
        )
        return ordered[:limit], relationships

    # -- incremental-reindex support -----------------------------------------

    def content_hashes(self) -> dict[str, str]:
        """Return content hashes for all symbols."""
        result: dict[str, str] = {}
        try:
            rows = self._fetch_all_rows(
                "MATCH (n:Symbol)",
                "RETURN n.qualified_name, n.content_hash",
                "n.qualified_name",
            )
            for row in rows:
                key = _decode(row.get("n.qualified_name"))
                ch = _decode(row.get("n.content_hash"))
                if key and ch:
                    result[key] = ch
        except Exception:
            logger.debug("failed to fetch content hashes", exc_info=True)
        return result

    def fts_docs(self) -> list[tuple[str, str]]:
        """``(qname, text)`` for every symbol — the FTS source.

        Embedded symbols contribute their stored embed text (``n.text``,
        written by the vector upsert). Symbols WITHOUT a live vector —
        lexical-only ingest with the embed server down — contribute a
        composed fallback from their graph props (docstring + source), so
        BM25 works with no embedder at all instead of indexing zero docs.
        """
        out: list[tuple[str, str]] = []
        try:
            rows = self._fetch_all_rows(
                "MATCH (n:Symbol)",
                "RETURN n.qualified_name, n.text, n.indexed_at, n.kind, "
                "n.docstring, n.source_code",
                "n.qualified_name",
            )
            for row in rows:
                qn = _decode(row.get("n.qualified_name"))
                if not qn:
                    continue
                if row.get("n.indexed_at") is not None:
                    text = _decode(row.get("n.text"))
                    if text:
                        out.append((qn, text))
                        continue
                kind = _decode(row.get("n.kind")) or ""
                docstring = _decode(row.get("n.docstring")) or ""
                source = _decode(row.get("n.source_code")) or ""
                if not docstring and not source:
                    continue
                parts = [f"# {kind}: {qn}" if kind else f"# {qn}", "# ---"]
                if docstring:
                    parts.append(docstring)
                if source:
                    parts.append(source)
                # Same order-of-magnitude cap as the embed text (see
                # ingest.embed_text.MAX_EMBED_TEXT_CHARS) so tantivy docs
                # stay bounded.
                out.append((qn, "\n".join(parts)[:4200]))
        except Exception:
            logger.warning("failed to fetch FTS docs", exc_info=True)
        return out

    def restore_vector_membership(self, qns: Sequence[str]) -> int:
        """Re-mark vector membership for symbols that survived a
        tombstone/re-upsert cycle unchanged (delta ingest skip path).

        ``delete_for_files`` nulls ``indexed_at`` while the merge-preserving
        symbol upsert keeps ``text``/``dense_vector``; setting ``indexed_at``
        back makes the surviving HNSW entry live again without re-embedding.
        Only nodes that still carry their embed text qualify.
        """
        restored = 0
        now = int(time.time())
        for qn in qns:
            try:
                self._db.execute_gql(
                    f"MATCH (n:Symbol) WHERE n.qualified_name = '{_esc(qn)}' "
                    f"AND n.text IS NOT NULL AND n.indexed_at IS NULL "
                    f"SET n.indexed_at = {now}"
                )
                restored += 1
            except Exception:
                logger.debug("failed to restore vector membership for %s", qn, exc_info=True)
        if restored:
            with contextlib.suppress(Exception):
                self._db.flush()
        return restored

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
                raise EmbeddingDimMismatchError(
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

        Unified-store invariant: Symbol nodes also back the code graph, so the
        replace clears the *vector membership* of existing symbols
        (``indexed_at`` — the marker count/search/read paths filter on) rather
        than deleting nodes. Stale HNSW entries for cleared symbols are then
        filtered out of search results. The new batch is upserted on top.
        """
        batch = list(rows)

        # Check dimensions
        for r in batch:
            if len(r.embedding) != self._vector_dimension:
                raise EmbeddingDimMismatchError(
                    f"qualified_name={r.qualified_name}: embedding has {len(r.embedding)} "
                    f"dimensions, expected {self._vector_dimension}",
                )

        # Clear vector membership for all existing symbols.
        try:
            self._db.execute_gql(
                "MATCH (n:Symbol) WHERE n.indexed_at IS NOT NULL SET n.indexed_at = NULL"
            )
            self._db.flush()
        except Exception:
            logger.debug("bulk_replace: failed to clear existing vectors", exc_info=True)

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
            raise EmbeddingDimMismatchError(
                f"query vector has {len(query_vec)} dimensions, expected {self._vector_dimension}",
            )

        # Normalize query vector
        normalized_query = l2_normalize(query_vec)
        allowed_qns = _parse_where_qn_set(where)

        try:
            # Over-fetch when a where filter applies so k hits survive it.
            fetch_k = max(k * 4, 64) if allowed_qns is not None else k
            hits = list(
                self._db.vector_search(
                    mode="dense",
                    k=fetch_k,
                    dense_query=normalized_query,
                )
            )

            results: list[CodeSearchHit] = []
            for hit in hits:
                if len(results) >= k:
                    break
                node_id = hit.node_id
                score = float(hit.score)  # OverGraph returns cosine similarity directly

                try:
                    node = self._db.get_node(node_id)
                    props = getattr(node, "props", {}) or {}
                    # Skip symbols whose vector membership was cleared (a
                    # bulk_replace left their HNSW entry behind).
                    if props.get("indexed_at") is None:
                        continue
                    key = getattr(node, "key", "")
                    if allowed_qns is not None and key not in allowed_qns:
                        continue

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
            logger.warning(
                "vector search failed — returning no hits "
                "(corrupt or missing index? run `agentalloy code index` to rebuild)",
                exc_info=True,
            )
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
        allowed_qns = _parse_where_qn_set(where)
        results: list[tuple[str, float]] = []
        try:
            # Over-fetch when a where filter applies so k hits survive it.
            fetch_k = max(k * 4, 64) if allowed_qns is not None else k
            # Simple substring match (not true BM25)
            rows = self._fetch_rows(
                f"MATCH (n:Symbol) WHERE n.text CONTAINS '{_esc(query)}' "
                f"RETURN n.qualified_name, n.text LIMIT {fetch_k}"
            )
            for row in rows:
                if len(results) >= k:
                    break
                key = _decode(row.get("n.qualified_name"))
                text = _decode(row.get("n.text"))
                if not key or (allowed_qns is not None and key not in allowed_qns):
                    continue
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
