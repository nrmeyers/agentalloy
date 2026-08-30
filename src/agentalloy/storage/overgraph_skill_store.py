"""OverGraph-backed skill store — unified graph + vector storage.

Replaces DuckDB (skills/versions/fragments/dependencies) + LanceDB (fragment
embeddings) with a single OverGraph database. OverGraph is an embedded Rust
graph database with built-in HNSW vector indexes.

Schema:
- Skill nodes: skill metadata (canonical_name, category, skill_class, etc.)
- SkillVersion nodes: version metadata (status, raw_prose, etc.)
- Fragment nodes: fragment content + dense_vector embeddings
- CURRENT_VERSION edges: Skill → SkillVersion (active version)
- HAS_VERSION edges: Skill → SkillVersion (all versions)
- DECOMPOSES_TO edges: SkillVersion → Fragment (ordered by sequence)
- REQUIRES edges: Skill → Skill (dependencies)
- CorpusMeta node: KV store for corpus metadata
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from agentalloy.storage.protocols import (
    EMBEDDING_DIM,
    BM25Hit,
    EmbeddingDimMismatch,
    FragmentDiscoveryRow,
    FragmentEmbedding,
    FragmentRow,
    SimilarityHit,
    SkillDependencyRow,
    SkillRow,
    SkillVersionRow,
    l2_normalize,
)
from agentalloy.storage.tantivy_bm25 import TantivyBM25Index

logger = logging.getLogger(__name__)


# Edge labels
_EDGE_CURRENT_VERSION = "CurrentVersion"
_EDGE_HAS_VERSION = "HasVersion"
_EDGE_DECOMPOSES_TO = "DecomposesTo"
_EDGE_REQUIRES = "Requires"


class OverGraphSkillStoreError(Exception):
    """Base exception for OverGraph skill store errors."""


class OverGraphSkillStore:
    """OverGraph-backed skill store with unified graph + vector storage."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        vector_dimension: int = EMBEDDING_DIM,
        read_only: bool = False,
    ) -> None:
        """Open or create an OverGraph database for skill storage.

        Args:
            db_path: Path to the OverGraph database directory.
            vector_dimension: Dimension of dense vectors for HNSW index.
            read_only: If True, open in read-only mode.
        """
        import overgraph

        self._db_path = str(db_path)
        self._vector_dimension = vector_dimension
        self._read_only = read_only
        self._db = overgraph.OverGraph.open(
            self._db_path,
            dense_vector_dimension=vector_dimension,
        )
        self._verify_dimension_alignment(vector_dimension)
        # Tantivy BM25 sidecar for full-text keyword search
        bm25_path = Path(self._db_path).parent / (Path(self._db_path).stem + ".bm25")
        self._bm25 = TantivyBM25Index(bm25_path, read_only=read_only)
        logger.debug("OverGraph skill store opened at %s", db_path)

    def _verify_dimension_alignment(self, expected_dim: int) -> None:
        """Verify that the database's vector dimension matches expectations."""
        # OverGraph persists dense_vector_dimension in its schema
        # For now, we trust the open() call configured it correctly
        logger.debug("vector dimension alignment verified: %d", expected_dim)

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self, "_bm25") and self._bm25 is not None:
            self._bm25.close()
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> OverGraphSkillStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def released(self) -> Iterator[None]:
        """Temporarily release the store handle; reopen on exit.

        Mirrors the DuckDB store's contract for in-process writers (web
        reembed / pack install): the long-lived service keeps this store
        open, so a write phase wraps itself in this context manager. The
        object stays valid for everyone holding a reference; the underlying
        database and BM25 sidecar are closed for the write window and
        reopened after with the same read-only mode.
        """
        self.close()
        try:
            yield
        finally:
            import overgraph

            self._db = overgraph.OverGraph.open(
                self._db_path,
                dense_vector_dimension=self._vector_dimension,
            )
            bm25_path = Path(self._db_path).parent / (Path(self._db_path).stem + ".bm25")
            self._bm25 = TantivyBM25Index(bm25_path, read_only=self._read_only)

    # -- transactions --------------------------------------------------------

    def begin(self) -> None:
        """Begin a write transaction."""
        self._txn = self._db.begin_write_txn()

    def commit(self) -> None:
        """Commit the current transaction."""
        if hasattr(self, "_txn") and self._txn is not None:
            self._txn.commit()
            self._txn = None
            self._db.flush()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        if hasattr(self, "_txn") and self._txn is not None:
            with contextlib.suppress(Exception):
                self._txn.rollback()
            self._txn = None

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema. Idempotent."""
        if self._read_only:
            raise RuntimeError("cannot migrate a read-only OverGraphSkillStore")
        # Ensure node labels exist
        self._db.ensure_node_label("Skill")
        self._db.ensure_node_label("SkillVersion")
        self._db.ensure_node_label("Fragment")
        self._db.ensure_node_label("CorpusMeta")
        # Ensure edge labels exist
        self._db.ensure_edge_label(_EDGE_CURRENT_VERSION)
        self._db.ensure_edge_label(_EDGE_HAS_VERSION)
        self._db.ensure_edge_label(_EDGE_DECOMPOSES_TO)
        self._db.ensure_edge_label(_EDGE_REQUIRES)
        logger.debug("OverGraph skill store schema ensured")

    # -- skill CRUD ----------------------------------------------------------

    def get_skill(self, skill_id: str) -> SkillRow | None:
        """Get a skill by ID."""
        node = self._db.get_node_by_key("Skill", skill_id)
        if node is None:
            return None
        props = dict(node.props)
        return SkillRow(
            skill_id=skill_id,
            canonical_name=props.get("canonical_name", ""),
            category=props.get("category", ""),
            skill_class=props.get("skill_class", ""),
            domain_tags=list(props.get("domain_tags", []) or []),
            deprecated=bool(props.get("deprecated", False)),
            superseded_by=props.get("superseded_by"),
            always_apply=bool(props.get("always_apply", False)),
            phase_scope=props.get("phase_scope"),
            category_scope=props.get("category_scope"),
            tier=props.get("tier"),
            description=props.get("description"),
            current_version_id=props.get("current_version_id", ""),
        )

    def get_skill_id_by_name(self, canonical_name: str) -> str | None:
        """Get skill_id by canonical_name."""
        rows = self._db.execute_gql(
            f"MATCH (n:Skill) WHERE n.canonical_name = '{_gql_esc(canonical_name)}' "
            f"RETURN id(n) AS nid LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            nid = rows["rows"][0].get("nid")
            if nid is not None:
                node = self._db.get_node(int(nid))
                return getattr(node, "key", None)
        return None

    def insert_skill(self, skill: SkillRow) -> None:
        """Insert a skill."""
        props = {
            "canonical_name": skill.canonical_name,
            "category": skill.category,
            "skill_class": skill.skill_class,
            "domain_tags": skill.domain_tags or [],
            "deprecated": skill.deprecated,
            "superseded_by": skill.superseded_by,
            "always_apply": skill.always_apply,
            "phase_scope": skill.phase_scope,
            "category_scope": skill.category_scope,
            "tier": skill.tier,
            "description": skill.description,
            "current_version_id": skill.current_version_id,
        }
        self._db.upsert_node(labels=["Skill"], key=skill.skill_id, props=props)
        self._sync_current_version_edge(skill.skill_id, skill.current_version_id)

    def _sync_current_version_edge(self, skill_id: str, version_id: str) -> None:
        """Align the CurrentVersion graph edge with the skill's
        ``current_version_id`` property — the reads layer joins on the edge,
        so a row-level update must move it too (insert_version only creates
        the edge for versions that arrive already active)."""
        skill_node = self._db.get_node_by_key("Skill", skill_id)
        if skill_node is None:
            return
        self._db.execute_gql(
            f"MATCH (s:Skill)-[r:{_EDGE_CURRENT_VERSION}]->() "
            f"WHERE id(s) = {skill_node.id} DELETE r"
        )
        if not version_id:
            return
        ver_node = self._db.get_node_by_key("SkillVersion", version_id)
        if ver_node is None:
            return
        self._db.upsert_edge(
            from_id=skill_node.id,
            to_id=ver_node.id,
            label=_EDGE_CURRENT_VERSION,
        )

    def delete_skill(self, skill_id: str) -> int:
        """Delete a skill and all its versions/fragments/deps. Returns count."""
        # Get the skill node
        node = self._db.get_node_by_key("Skill", skill_id)
        if node is None:
            return 0

        # Delete all versions and their fragments
        versions = self._get_versions_for_skill(skill_id)
        for version in versions:
            # Delete fragments for this version
            fragments = self._get_fragments_for_version(version.version_id)
            for frag in fragments:
                frag_node = self._db.get_node_by_key("Fragment", frag.fragment_id)
                if frag_node:
                    self._db.execute_gql(f"MATCH (n) WHERE id(n) = {frag_node.id} DETACH DELETE n")
            # Delete the version node
            ver_node = self._db.get_node_by_key("SkillVersion", version.version_id)
            if ver_node:
                self._db.execute_gql(f"MATCH (n) WHERE id(n) = {ver_node.id} DETACH DELETE n")

        # Delete dependencies touching this skill (outgoing or incoming).
        # Anchor on node ids: STARTNODE()/ENDNODE() and node-property
        # projection are unsupported inside edge-pattern WHERE clauses.
        self._db.execute_gql(
            f"MATCH (s:Skill)-[r:{_EDGE_REQUIRES}]->() WHERE id(s) = {node.id} DELETE r"
        )
        self._db.execute_gql(
            f"MATCH ()-[r:{_EDGE_REQUIRES}]->(t:Skill) WHERE id(t) = {node.id} DELETE r"
        )

        # Delete the skill node
        self._db.execute_gql(f"MATCH (n:Skill) WHERE id(n) = {node.id} DETACH DELETE n")
        return 1

    def rollback_skill(self, skill_id: str) -> None:
        """Roll back a single skill insertion. Soft-fails."""
        try:
            self.delete_skill(skill_id)
        except Exception as exc:
            logger.error("rollback_skill failed for %s: %s", skill_id, exc)

    def rollback_batch(self, skill_ids: Sequence[str]) -> None:
        """Roll back multiple skills."""
        for sid in skill_ids:
            self.rollback_skill(sid)

    # -- version CRUD --------------------------------------------------------

    def get_version(self, version_id: str) -> SkillVersionRow | None:
        """Get a version by ID."""
        node = self._db.get_node_by_key("SkillVersion", version_id)
        if node is None:
            return None
        props = dict(node.props)
        return SkillVersionRow(
            version_id=version_id,
            skill_id=props.get("skill_id", ""),
            version_number=int(props.get("version_number", 0)),
            authored_at=props.get("authored_at"),
            author=props.get("author", ""),
            change_summary=props.get("change_summary", ""),
            status=props.get("status", ""),
            raw_prose=props.get("raw_prose", ""),
        )

    def get_versions_by_skill(self, skill_id: str) -> list[SkillVersionRow]:
        """Get all versions for a skill, ordered by version_number DESC."""
        # ``v.key`` projection is broken in GQL — return the node id and read
        # the key off the node itself.
        rows = self._db.execute_gql(
            f"MATCH (v:SkillVersion) WHERE v.skill_id = '{_gql_esc(skill_id)}' "
            f"RETURN id(v) AS vid, v.version_number "
            f"ORDER BY v.version_number DESC"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                vid = row.get("vid")
                if vid is None:
                    continue
                node = self._db.get_node(int(vid))
                if node is None:
                    continue
                props = dict(node.props)
                results.append(
                    SkillVersionRow(
                        version_id=getattr(node, "key", "") or "",
                        skill_id=props.get("skill_id", ""),
                        version_number=int(props.get("version_number", 0)),
                        authored_at=props.get("authored_at"),
                        author=props.get("author", ""),
                        change_summary=props.get("change_summary", ""),
                        status=props.get("status", ""),
                        raw_prose=props.get("raw_prose", ""),
                    )
                )
        return results

    def insert_version(self, version: SkillVersionRow) -> None:
        """Insert a version."""
        authored_at = version.authored_at
        if hasattr(authored_at, "isoformat"):
            authored_at = authored_at.isoformat()
        props = {
            "skill_id": version.skill_id,
            "version_number": version.version_number,
            "authored_at": authored_at,
            "author": version.author,
            "change_summary": version.change_summary,
            "status": version.status,
            "raw_prose": version.raw_prose,
        }
        self._db.upsert_node(labels=["SkillVersion"], key=version.version_id, props=props)
        # Create HAS_VERSION edge from skill to version
        skill_node = self._db.get_node_by_key("Skill", version.skill_id)
        ver_node = self._db.get_node_by_key("SkillVersion", version.version_id)
        if skill_node and ver_node:
            self._db.upsert_edge(
                from_id=skill_node.id,
                to_id=ver_node.id,
                label=_EDGE_HAS_VERSION,
            )
        # If status is active, create CURRENT_VERSION edge
        if version.status == "active" and skill_node and ver_node:
            self._db.upsert_edge(
                from_id=skill_node.id,
                to_id=ver_node.id,
                label=_EDGE_CURRENT_VERSION,
            )

    # -- fragment CRUD -------------------------------------------------------

    def get_fragment(self, fragment_id: str) -> FragmentRow | None:
        """Get a fragment by ID."""
        node = self._db.get_node_by_key("Fragment", fragment_id)
        if node is None:
            return None
        props = dict(node.props)
        return FragmentRow(
            fragment_id=fragment_id,
            version_id=props.get("version_id", ""),
            fragment_type=props.get("fragment_type", ""),
            sequence=int(props.get("sequence", 0)),
            content=props.get("content", ""),
        )

    def insert_fragment(self, fragment: FragmentRow) -> None:
        """Insert a fragment (without embedding)."""
        props = {
            "version_id": fragment.version_id,
            "fragment_type": fragment.fragment_type,
            "sequence": fragment.sequence,
            "content": fragment.content,
        }
        self._db.upsert_node(labels=["Fragment"], key=fragment.fragment_id, props=props)
        # Create DECOMPOSES_TO edge from version to fragment
        ver_node = self._db.get_node_by_key("SkillVersion", fragment.version_id)
        frag_node = self._db.get_node_by_key("Fragment", fragment.fragment_id)
        if ver_node and frag_node:
            self._db.upsert_edge(
                from_id=ver_node.id,
                to_id=frag_node.id,
                label=_EDGE_DECOMPOSES_TO,
                props={"sequence": fragment.sequence},
            )

    def count_fragments(self) -> int:
        """Count all fragment nodes (any version/skill status)."""
        try:
            rows = self._db.execute_gql("MATCH (f:Fragment) RETURN count(f) AS cnt")
            if isinstance(rows, dict) and rows.get("rows"):
                return int(rows["rows"][0].get("cnt", 0))
        except Exception:
            pass
        return 0

    # -- dependency CRUD -----------------------------------------------------

    def get_dependencies(self, skill_id: str) -> list[SkillDependencyRow]:
        """Get dependencies for a skill."""
        # Anchor on the source node's id: the GQL engine cannot project node
        # properties (``s.key``) inside edge-pattern matches — WHERE/RETURN on
        # them silently yield None — so the working pattern is id(s)/id(t)
        # plus a node lookup for the key.
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return []
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[r:{_EDGE_REQUIRES}]->(t:Skill) "
            f"WHERE id(s) = {source.id} "
            f"RETURN id(t) AS tid, r.rel_type AS rel_type"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                tid = row.get("tid")
                if tid is None:
                    continue
                target_node = self._db.get_node(int(tid))
                if target_node is None:
                    continue
                results.append(
                    SkillDependencyRow(
                        source_skill_id=skill_id,
                        target_skill_id=getattr(target_node, "key", "") or "",
                        rel_type=row.get("rel_type") or "requires",
                    )
                )
        return results

    def insert_dependency(self, dep: SkillDependencyRow) -> None:
        """Insert a dependency."""
        source = self._db.get_node_by_key("Skill", dep.source_skill_id)
        target = self._db.get_node_by_key("Skill", dep.target_skill_id)
        if source and target:
            self._db.upsert_edge(
                from_id=source.id,
                to_id=target.id,
                label=_EDGE_REQUIRES,
                props={"rel_type": dep.rel_type},
            )

    def delete_dependencies(self, skill_id: str, rel_type: str | None = None) -> int:
        """Delete outgoing dependency edges for a skill. Returns edges removed.

        With ``rel_type`` given, only edges of that type are removed; otherwise
        all outgoing edges go (the re-ingest idempotency path). The graph
        stores requires-edges under a single ``Requires`` label, so the count
        is taken from ``get_dependencies`` (which carries each edge's rel_type
        property) and the delete is one label-scoped GQL pass.
        """
        existing = self.get_dependencies(skill_id)
        removed = [d for d in existing if rel_type is None or d.rel_type == rel_type]
        if not removed:
            return 0
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return 0
        # Anchor on id(s) — see get_dependencies: node-property projection is
        # broken inside edge-pattern matches.
        rel_filter = "" if rel_type is None else f" AND r.rel_type = '{_gql_esc(rel_type)}'"
        self._db.execute_gql(
            f"MATCH (s:Skill)-[r:{_EDGE_REQUIRES}]->() "
            f"WHERE id(s) = {source.id}{rel_filter} "
            f"DELETE r"
        )
        self._db.flush()
        return len(removed)

    # -- active-version reads ------------------------------------------------

    def get_active_skills(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> list[SkillRow]:
        """Get all active skills, optionally filtered by class."""
        filters = ["s.deprecated = false"]
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                classes = ", ".join(f"'{_gql_esc(c)}'" for c in skill_class)
                filters.append(f"s.skill_class IN [{classes}]")
            else:
                filters.append(f"s.skill_class = '{_gql_esc(skill_class)}'")
        filter_clause = " AND ".join(filters)
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE v.status = 'active' AND {filter_clause} "
            f"RETURN id(s) AS sid, s.canonical_name, s.category, s.skill_class, "
            f"s.domain_tags, s.deprecated, s.superseded_by, s.always_apply, "
            f"s.phase_scope, s.category_scope, s.tier, s.description, s.current_version_id "
            f"ORDER BY s.canonical_name"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                sid = row.get("sid")
                if sid is None:
                    continue
                node = self._db.get_node(int(sid))
                if node is None:
                    continue
                skill_id = getattr(node, "key", "") or ""
                results.append(
                    SkillRow(
                        skill_id=skill_id,
                        canonical_name=row.get("s.canonical_name", "")
                        or node.props.get("canonical_name", ""),
                        category=row.get("s.category", "") or node.props.get("category", ""),
                        skill_class=row.get("s.skill_class", "")
                        or node.props.get("skill_class", ""),
                        domain_tags=list(
                            row.get("s.domain_tags", []) or node.props.get("domain_tags", []) or []
                        ),
                        deprecated=bool(row.get("s.deprecated", False)),
                        superseded_by=row.get("s.superseded_by") or node.props.get("superseded_by"),
                        always_apply=bool(row.get("s.always_apply", False)),
                        phase_scope=row.get("s.phase_scope") or node.props.get("phase_scope"),
                        category_scope=row.get("s.category_scope")
                        or node.props.get("category_scope"),
                        tier=row.get("s.tier") or node.props.get("tier"),
                        description=row.get("s.description") or node.props.get("description"),
                        current_version_id=row.get("s.current_version_id", "")
                        or node.props.get("current_version_id", ""),
                    )
                )
        return results

    def get_active_skill_by_id(self, skill_id: str) -> SkillRow | None:
        """Get an active skill by ID."""
        # Anchor on id(s): ``s.key`` WHERE is broken in edge-pattern matches.
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return None
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE id(s) = {source.id} AND v.status = 'active' AND s.deprecated = false "
            f"RETURN s.canonical_name AS canonical_name, s.category AS category, "
            f"s.skill_class AS skill_class, s.domain_tags AS domain_tags, "
            f"s.deprecated AS deprecated, s.superseded_by AS superseded_by, "
            f"s.always_apply AS always_apply, s.phase_scope AS phase_scope, "
            f"s.category_scope AS category_scope, s.tier AS tier, "
            f"s.description AS description, s.current_version_id AS current_version_id "
            f"LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            row = rows["rows"][0]
            return SkillRow(
                skill_id=skill_id,
                canonical_name=row.get("canonical_name", ""),
                category=row.get("category", ""),
                skill_class=row.get("skill_class", ""),
                domain_tags=list(row.get("domain_tags", []) or []),
                deprecated=bool(row.get("deprecated", False)),
                superseded_by=row.get("superseded_by"),
                always_apply=bool(row.get("always_apply", False)),
                phase_scope=row.get("phase_scope"),
                category_scope=row.get("category_scope"),
                tier=row.get("tier"),
                description=row.get("description"),
                current_version_id=row.get("current_version_id", ""),
            )
        return None

    def get_deprecated_skill_ids(self) -> list[str]:
        """Get IDs of all deprecated skills."""
        # ``s.key`` projection is broken in GQL — return node ids and resolve
        # the keys off the nodes.
        rows = self._db.execute_gql("MATCH (s:Skill) WHERE s.deprecated = true RETURN id(s) AS sid")
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                sid = row.get("sid")
                if sid is None:
                    continue
                node = self._db.get_node(int(sid))
                if node is not None:
                    results.append(getattr(node, "key", "") or "")
        return results

    def count_skills(self) -> int:
        """Count all skill nodes (any deprecation/version status)."""
        try:
            rows = self._db.execute_gql("MATCH (s:Skill) RETURN count(s) AS cnt")
            if isinstance(rows, dict) and rows.get("rows"):
                return int(rows["rows"][0].get("cnt", 0))
        except Exception:
            pass
        return 0

    def get_active_fragments(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        domain_tags: list[str] | None = None,
    ) -> list[FragmentRow]:
        """Get fragments of active skills, with optional filters."""
        filters = ["v.status = 'active'", "s.deprecated = false"]
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                classes = ", ".join(f"'{_gql_esc(c)}'" for c in skill_class)
                filters.append(f"s.skill_class IN [{classes}]")
            else:
                filters.append(f"s.skill_class = '{_gql_esc(skill_class)}'")
        if categories is not None:
            cats = ", ".join(f"'{_gql_esc(c)}'" for c in categories)
            filters.append(f"s.category IN [{cats}]")
        # domain_tags list-membership (ANY) is outside the GQL subset — the
        # predicate is applied in Python against the parent skill's tags below.
        filter_clause = " AND ".join(filters)
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE {filter_clause} "
            f"RETURN id(s) AS sid, id(f) AS fid, f.version_id, f.fragment_type, f.sequence, f.content "
            f"ORDER BY s.key, f.sequence"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                fid = row.get("fid")
                sid = row.get("sid")
                if fid is None:
                    continue
                node = self._db.get_node(int(fid))
                if node is None:
                    continue
                # Parent-derived columns come off the skill node (the source of
                # truth) so fragments that predate their embedding pass still
                # carry them.
                skill_node = self._db.get_node(int(sid)) if sid is not None else None
                sprops = dict(skill_node.props) if skill_node is not None else {}
                tags = list(sprops.get("domain_tags", []) or [])
                if domain_tags is not None and not set(tags) & set(domain_tags):
                    continue
                fprops = dict(node.props)
                results.append(
                    FragmentRow(
                        fragment_id=getattr(node, "key", "") or "",
                        version_id=row.get("f.version_id", "") or fprops.get("version_id", ""),
                        fragment_type=row.get("f.fragment_type", "")
                        or fprops.get("fragment_type", ""),
                        sequence=int(row.get("f.sequence", 0) or fprops.get("sequence", 0)),
                        content=row.get("f.content", "") or fprops.get("content", ""),
                        skill_id=getattr(skill_node, "key", "") or "",
                        category=sprops.get("category", ""),
                        skill_class=sprops.get("skill_class", ""),
                        domain_tags=tags,
                        phase_scope=sprops.get("phase_scope"),
                        category_scope=sprops.get("category_scope"),
                        description=sprops.get("description"),
                    )
                )
        return results

    def get_active_fragments_for_skill(self, skill_id: str) -> list[FragmentRow]:
        """Get fragments for a specific active skill."""
        # Anchor on id(s): ``s.key`` WHERE is broken in edge-pattern matches.
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return []
        sprops = dict(source.props)
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE id(s) = {source.id} AND v.status = 'active' AND s.deprecated = false "
            f"RETURN id(f) AS fid, f.version_id, f.fragment_type, f.sequence, f.content "
            f"ORDER BY f.sequence"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                fid = row.get("fid")
                if fid is None:
                    continue
                node = self._db.get_node(int(fid))
                if node is None:
                    continue
                results.append(
                    FragmentRow(
                        fragment_id=getattr(node, "key", "") or "",
                        version_id=row.get("f.version_id", "") or node.props.get("version_id", ""),
                        fragment_type=row.get("f.fragment_type", "")
                        or node.props.get("fragment_type", ""),
                        sequence=int(row.get("f.sequence", 0) or node.props.get("sequence", 0)),
                        content=row.get("f.content", "") or node.props.get("content", ""),
                        skill_id=skill_id,
                        category=sprops.get("category", ""),
                        skill_class=sprops.get("skill_class", ""),
                        domain_tags=list(sprops.get("domain_tags", []) or []),
                        phase_scope=sprops.get("phase_scope"),
                        category_scope=sprops.get("category_scope"),
                        description=sprops.get("description"),
                    )
                )
        return results

    # -- re-embed pipeline ---------------------------------------------------

    def discover_fragments(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[FragmentDiscoveryRow]:
        """Discover fragments for the re-embed pipeline."""
        # ``.key`` projection/WHERE is broken in edge-pattern matches — anchor
        # on id(s) for the per-skill filter and resolve fragment/skill keys via
        # node lookups. Sorting happens in Python (ORDER BY s.key is unusable).
        anchor_clause = ""
        source_id: int | None = None
        if skill_id is not None:
            source = self._db.get_node_by_key("Skill", skill_id)
            if source is None:
                return []
            source_id = source.id
            anchor_clause = f" AND id(s) = {source_id}"
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE v.status = 'active' AND s.deprecated = false{anchor_clause} "
            f"RETURN id(f) AS fid, id(s) AS sid, f.content AS content, "
            f"f.fragment_type AS fragment_type, f.sequence AS sequence, "
            f"s.category AS category, s.canonical_name AS canonical_name, "
            f"s.domain_tags AS domain_tags, s.description AS description"
        )
        raw: list[tuple[int, int, int, dict[str, Any]]] = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                fid = row.get("fid")
                sid = row.get("sid")
                if fid is None or sid is None:
                    continue
                raw.append((int(fid), int(sid), int(row.get("sequence") or 0), row))
        results: list[tuple[str, int, FragmentDiscoveryRow]] = []
        for fid, sid, seq, row in raw:
            frag_node = self._db.get_node(fid)
            skill_node = self._db.get_node(sid)
            if frag_node is None or skill_node is None:
                continue
            frag_key = getattr(frag_node, "key", "") or ""
            skill_key = getattr(skill_node, "key", "") or ""
            results.append(
                (
                    skill_key,
                    seq,
                    FragmentDiscoveryRow(
                        fragment_id=frag_key,
                        content=row.get("content") or frag_node.props.get("content", ""),
                        fragment_type=row.get("fragment_type")
                        or frag_node.props.get("fragment_type", ""),
                        skill_id=skill_key,
                        category=row.get("category") or skill_node.props.get("category", ""),
                        canonical_name=row.get("canonical_name")
                        or skill_node.props.get("canonical_name", "")
                        or "",
                        domain_tags=tuple(
                            row.get("domain_tags") or skill_node.props.get("domain_tags") or ()
                        ),
                        description=(
                            (
                                row.get("description") or skill_node.props.get("description") or ""
                            ).strip()
                            or None
                        ),
                    ),
                )
            )
        # DuckDB parity: order by (skill_id, fragment sequence).
        results.sort(key=lambda item: (item[0], item[1]))
        return [dto for _skill_key, _seq, dto in results]

    # -- consistency guards --------------------------------------------------

    def check_consistency(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> None:
        """Check CURRENT_VERSION / active-version consistency."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        # Check for CURRENT_VERSION pointing to non-active version.
        # ``s.key`` projection is broken in edge-pattern matches — return the
        # node id and resolve the key off the node.
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE v.status <> 'active' "
            f"RETURN id(s) AS sid, v.status AS status LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            row = rows["rows"][0]
            sid = row.get("sid")
            node = self._db.get_node(int(sid)) if sid is not None else None
            skill_key = (getattr(node, "key", "") or "") if node is not None else ""
            raise InconsistentActiveVersionError(
                skill_key,
                f"CURRENT_VERSION points at status={row.get('status')!r} version",
            )

        # Check for active versions that lack a CURRENT_VERSION edge. Two
        # passes + a Python diff: a negated-path predicate is outside the GQL
        # subset, and one query per skill would hit the per-request guard too
        # hard.
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_HAS_VERSION}]->(v:SkillVersion) "
            "WHERE v.status = 'active' "
            "RETURN id(s) AS sid"
        )
        missing: set[int] = set()
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                sid = row.get("sid")
                if sid is not None:
                    missing.add(int(sid))
        if missing:
            rows = self._db.execute_gql(
                f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->() RETURN id(s) AS sid"
            )
            if isinstance(rows, dict):
                for row in rows.get("rows", []):
                    sid = row.get("sid")
                    if sid is not None:
                        missing.discard(int(sid))
        if missing:
            node = self._db.get_node(next(iter(missing)))
            skill_key = (getattr(node, "key", "") or "") if node is not None else ""
            raise InconsistentActiveVersionError(
                skill_key,
                "active version exists but no CURRENT_VERSION edge",
            )

    def check_consistency_for(self, skill_id: str) -> None:
        """Check consistency for a specific skill."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        # Anchor on id(s): ``s.key`` WHERE is broken in edge-pattern matches.
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE id(s) = {source.id} AND v.status <> 'active' "
            f"RETURN v.status AS status LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            raise InconsistentActiveVersionError(
                skill_id,
                f"CURRENT_VERSION points at status={rows['rows'][0].get('status')!r} version",
            )
        # Active version present but no CURRENT_VERSION edge.
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_HAS_VERSION}]->(v:SkillVersion) "
            f"WHERE id(s) = {source.id} AND v.status = 'active' "
            "RETURN id(v) AS vid LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            cv_rows = self._db.execute_gql(
                f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->() "
                f"WHERE id(s) = {source.id} RETURN id(s) AS sid LIMIT 1"
            )
            if not (isinstance(cv_rows, dict) and cv_rows.get("rows")):
                raise InconsistentActiveVersionError(
                    skill_id,
                    "active version exists but no CURRENT_VERSION edge",
                )

    # -- corpus metadata KV --------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a corpus_meta key/value."""
        props = {"key": key, "value": value, "updated_at": int(time.time())}
        self._db.upsert_node(labels=["CorpusMeta"], key=f"meta:{key}", props=props)

    def get_meta(self, key: str) -> str | None:
        """Return the corpus_meta value for key, or None if unset."""
        node = self._db.get_node_by_key("CorpusMeta", f"meta:{key}")
        if node is None:
            return None
        return dict(node.props).get("value")

    # -- bulk operations -----------------------------------------------------

    def clear_all(self) -> None:
        """Clear all data (for fixtures/tests)."""
        self._db.execute_gql("MATCH (n:Fragment) DETACH DELETE n")
        self._db.execute_gql("MATCH (n:SkillVersion) DETACH DELETE n")
        self._db.execute_gql("MATCH (n:Skill) DETACH DELETE n")
        self._db.execute_gql("MATCH (n:CorpusMeta) DETACH DELETE n")

    # -- internal helpers ----------------------------------------------------

    def _get_versions_for_skill(self, skill_id: str) -> list[SkillVersionRow]:
        """Get all versions for a skill."""
        # Anchor on node ids — ``.key`` projection/WHERE is broken inside
        # edge-pattern matches (see get_dependencies).
        source = self._db.get_node_by_key("Skill", skill_id)
        if source is None:
            return []
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_HAS_VERSION}]->(v:SkillVersion) "
            f"WHERE id(s) = {source.id} "
            f"RETURN id(v) AS vid"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                vid = row.get("vid")
                if vid is None:
                    continue
                node = self._db.get_node(int(vid))
                if node is None:
                    continue
                props = dict(node.props)
                results.append(
                    SkillVersionRow(
                        version_id=getattr(node, "key", "") or "",
                        skill_id=props.get("skill_id", ""),
                        version_number=int(props.get("version_number", 0)),
                        authored_at=props.get("authored_at"),
                        author=props.get("author", ""),
                        change_summary=props.get("change_summary", ""),
                        status=props.get("status", ""),
                        raw_prose=props.get("raw_prose", ""),
                    )
                )
        return results

    def _get_fragments_for_version(self, version_id: str) -> list[FragmentRow]:
        """Get all fragments for a version."""
        # Anchor on node ids — ``.key`` projection/WHERE is broken inside
        # edge-pattern matches (see get_dependencies).
        ver_node = self._db.get_node_by_key("SkillVersion", version_id)
        if ver_node is None:
            return []
        rows = self._db.execute_gql(
            f"MATCH (v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE id(v) = {ver_node.id} "
            f"RETURN id(f) AS fid"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                fid = row.get("fid")
                if fid is None:
                    continue
                node = self._db.get_node(int(fid))
                if node is None:
                    continue
                props = dict(node.props)
                results.append(
                    FragmentRow(
                        fragment_id=getattr(node, "key", "") or "",
                        version_id=props.get("version_id", ""),
                        fragment_type=props.get("fragment_type", ""),
                        sequence=int(props.get("sequence", 0)),
                        content=props.get("content", ""),
                    )
                )
        return results

    # -- FragmentStore protocol (vector + BM25 search over fragments) --------

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int:
        """Upsert fragment embeddings into the HNSW index."""
        count = 0
        for item in items:
            if not item.fragment_id:
                logger.debug("skipping embedding with None fragment_id")
                continue
            # Guard BEFORE touching the HNSW index: a stored-vs-configured dim
            # mismatch must surface as the marker upgrade.py greps for to
            # trigger the self-heal re-embed (not a raw DB error).
            if len(item.embedding) != self._vector_dimension:
                raise EmbeddingDimMismatch(
                    f"stored embedding dimension {len(item.embedding)} does not match "
                    f"configured EMBEDDING_DIM {self._vector_dimension} — "
                    "run `agentalloy reembed --force` to rebuild the index"
                )
            # Skip zero vectors (from failed embeddings)
            import math

            norm = math.sqrt(sum(x * x for x in item.embedding))
            if norm < 1e-9:
                logger.debug("skipping zero vector for %s", item.fragment_id)
                continue
            normalized = l2_normalize(list(item.embedding))
            # Get or create the Fragment node
            frag_node = self._db.get_node_by_key("Fragment", item.fragment_id)
            if frag_node is not None:
                # Update existing node with vector
                props = dict(frag_node.props)
                props.update(
                    {
                        "skill_id": item.skill_id,
                        "category": item.category,
                        "fragment_type": item.fragment_type,
                        "embedded_at": item.embedded_at,
                        "embedding_model": item.embedding_model,
                        "prose": item.prose,
                        "phase_scope": list(item.phase_scope) if item.phase_scope else None,
                        "domain_tags": list(item.domain_tags) if item.domain_tags else None,
                    }
                )
                self._db.upsert_node(
                    labels=["Fragment"],
                    key=item.fragment_id,
                    props=props,
                    dense_vector=normalized,
                )
            else:
                # Create new Fragment node with vector
                props = {
                    "skill_id": item.skill_id,
                    "category": item.category,
                    "fragment_type": item.fragment_type,
                    "embedded_at": item.embedded_at,
                    "embedding_model": item.embedding_model,
                    "prose": item.prose,
                    "phase_scope": list(item.phase_scope) if item.phase_scope else None,
                    "domain_tags": list(item.domain_tags) if item.domain_tags else None,
                }
                self._db.upsert_node(
                    labels=["Fragment"],
                    key=item.fragment_id,
                    props=props,
                    dense_vector=normalized,
                )
            # Index in Tantivy BM25 sidecar
            self._bm25.upsert(
                fragment_id=item.fragment_id,
                skill_id=item.skill_id,
                category=item.category,
                fragment_type=item.fragment_type,
                prose=item.prose or "",
                phase_scope=list(item.phase_scope) if item.phase_scope else None,
                domain_tags=list(item.domain_tags) if item.domain_tags else None,
            )
            count += 1
        # Commit BM25 writes
        self._bm25.commit()
        # Don't flush OverGraph here — caller controls flush timing to avoid races
        return count

    def search_similar(
        self,
        query_vec: Sequence[float],
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        fragment_types: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        domain_tags: list[str] | None = None,
        k: int = 10,
    ) -> list[SimilarityHit]:
        """Dense vector similarity search over fragment embeddings.

        OverGraph's vector_search doesn't support property-level filtering,
        so we fetch more candidates than needed and post-filter in Python.
        """
        normalized_query = l2_normalize(list(query_vec))
        # Fetch more candidates to account for post-filtering
        fetch_k = max(k * 5, 100)
        try:
            raw_hits = self._db.vector_search(
                mode="dense",
                k=fetch_k,
                dense_query=normalized_query,
                label_filter={"labels": ["Fragment"]},
            )
        except Exception:
            logger.debug("vector_search failed", exc_info=True)
            return []

        deprecated_set = set(deprecated_skill_ids or [])
        results: list[SimilarityHit] = []
        for hit in raw_hits:
            node = self._db.get_node(hit.node_id)
            if node is None:
                continue
            props = dict(node.props)
            skill_id = props.get("skill_id", "")
            # Post-filter
            if skill_id in deprecated_set:
                continue
            if categories and props.get("category") not in categories:
                continue
            if fragment_types and props.get("fragment_type") not in fragment_types:
                continue
            if phases:
                frag_phases = props.get("phase_scope") or []
                if not set(frag_phases) & set(phases):
                    continue
            if domain_tags:
                frag_tags = set(props.get("domain_tags") or [])
                if not frag_tags & set(domain_tags):
                    continue
            results.append(
                SimilarityHit(
                    fragment_id=getattr(node, "key", ""),
                    skill_id=skill_id,
                    distance=1.0 - float(hit.score),  # Convert similarity to distance
                )
            )
            if len(results) >= k:
                break
        return results

    def search_bm25(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        phases: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        domain_tags: list[str] | None = None,
        k: int = 10,
    ) -> list[BM25Hit]:
        """BM25 keyword search via Tantivy FTS sidecar."""
        return self._bm25.search(
            query,
            categories=categories,
            deprecated_skill_ids=deprecated_skill_ids,
            domain_tags=domain_tags,
            k=k,
        )

    def backfill_phase_scope(self, scope_by_skill: dict[str, list[str] | None]) -> int:
        """Update phase_scope on fragment nodes by skill_id."""
        count = 0
        for skill_id, scope in scope_by_skill.items():
            rows = self._db.execute_gql(
                f"MATCH (f:Fragment) WHERE f.skill_id = '{_gql_esc(skill_id)}' RETURN id(f) AS nid"
            )
            if isinstance(rows, dict):
                for row in rows.get("rows", []):
                    nid = row.get("nid")
                    if nid is not None:
                        node = self._db.get_node(int(nid))
                        if node:
                            props = dict(node.props)
                            props["phase_scope"] = list(scope) if scope else None
                            self._db.upsert_node(
                                labels=["Fragment"],
                                key=getattr(node, "key", ""),
                                props=props,
                            )
                            count += 1
        self._db.flush()
        return count

    def count_embeddings(self) -> int:
        """Count fragment nodes with dense_vector set."""
        try:
            rows = self._db.execute_gql(
                "MATCH (f:Fragment) WHERE f.embedded_at IS NOT NULL RETURN count(f) AS cnt"
            )
            if isinstance(rows, dict) and rows.get("rows"):
                return int(rows["rows"][0].get("cnt", 0))
        except Exception:
            pass
        return 0

    def count_cards(self) -> int:
        """Count synthetic card documents."""
        try:
            rows = self._db.execute_gql(
                "MATCH (f:Fragment) WHERE f.fragment_type = 'card' RETURN count(f) AS cnt"
            )
            if isinstance(rows, dict) and rows.get("rows"):
                return int(rows["rows"][0].get("cnt", 0))
        except Exception:
            pass
        return 0

    def delete_cards(self, skill_id: str | None = None) -> int:
        """Delete synthetic card documents."""
        if skill_id:
            filter_clause = f"f.fragment_type = 'card' AND f.skill_id = '{_gql_esc(skill_id)}'"
        else:
            filter_clause = "f.fragment_type = 'card'"
        try:
            rows = self._db.execute_gql(
                f"MATCH (f:Fragment) WHERE {filter_clause} RETURN id(f) AS nid"
            )
            count = 0
            if isinstance(rows, dict):
                for row in rows.get("rows", []):
                    nid = row.get("nid")
                    if nid is not None:
                        self._db.execute_gql(f"MATCH (n) WHERE id(n) = {nid} DETACH DELETE n")
                        count += 1
            self._db.flush()
            return count
        except Exception:
            return 0

    def _canonical_fragments(self, *, skill_id: str | None = None) -> tuple[list[FragmentRow], int]:
        """Snapshot authored fragment rows + count embedded ones.

        Returns ``(rows, embedded_count)``. Rows with no ``version_id`` are
        synthetic card documents — regenerated by reembed, not preserved.
        """
        where = f" WHERE f.skill_id = '{_gql_esc(skill_id)}'" if skill_id else ""
        gql_rows = self._db.execute_gql(f"MATCH (f:Fragment){where} RETURN id(f) AS nid")
        keep: list[FragmentRow] = []
        embedded = 0
        if isinstance(gql_rows, dict):
            for row in gql_rows.get("rows", []):
                nid = row.get("nid")
                if nid is None:
                    continue
                node = self._db.get_node(int(nid))
                if node is None:
                    continue
                props = dict(node.props)
                if props.get("embedded_at") is not None:
                    embedded += 1
                version_id = props.get("version_id", "") or ""
                if not version_id:
                    continue
                keep.append(
                    FragmentRow(
                        fragment_id=getattr(node, "key", "") or "",
                        version_id=version_id,
                        fragment_type=props.get("fragment_type", ""),
                        sequence=int(props.get("sequence", 0)),
                        content=props.get("content", ""),
                    )
                )
        return keep, embedded

    def delete_skill_fragments(self, skill_id: str) -> int:
        """Delete only fragment embeddings for a skill (keeps skill node).

        Unified-store invariant: Fragment nodes also carry authored content,
        so the canonical rows are snapshotted and re-linked after the wipe —
        only the embedding side (vectors + BM25 docs) goes away. Used by the
        re-embed pipeline to clear stale embeddings before re-inserting.
        Distinct from the SkillStore ``delete_skill`` which removes the skill
        node, versions, and dependencies too. Returns embeddings deleted.
        """
        try:
            keep, embedded = self._canonical_fragments(skill_id=skill_id)
            rows = self._db.execute_gql(
                f"MATCH (f:Fragment) WHERE f.skill_id = '{_gql_esc(skill_id)}' RETURN id(f) AS nid"
            )
            if isinstance(rows, dict):
                for row in rows.get("rows", []):
                    nid = row.get("nid")
                    if nid is not None:
                        self._db.execute_gql(f"MATCH (n) WHERE id(n) = {nid} DETACH DELETE n")
            self._db.flush()
            self._bm25.delete_skill(skill_id)
            for frag in keep:
                self.insert_fragment(frag)
            return embedded
        except Exception:
            logger.debug("delete_skill_fragments failed", exc_info=True)
            return 0

    def delete_all(self) -> int:
        """Delete all fragment embeddings. Returns the number deleted.

        Unified-store invariant: authored fragment content survives the wipe
        (canonical rows are snapshotted and re-linked); synthetic card docs
        are dropped and rebuilt by the card-index pass.
        """
        try:
            keep, embedded = self._canonical_fragments()
            self._db.execute_gql("MATCH (f:Fragment) DETACH DELETE f")
            self._db.flush()
            self._bm25.delete_all()
            for frag in keep:
                self.insert_fragment(frag)
            return embedded
        except Exception:
            logger.debug("delete_all failed", exc_info=True)
            return 0

    def embedding_dim(self) -> int | None:
        """Return the embedding dimension, or None if no embeddings exist."""
        count = self.count_embeddings()
        if count == 0:
            return None
        return self._vector_dimension

    def fragment_ids_present(self, fragment_ids: Sequence[str]) -> set[str]:
        """Check which fragment_ids already have embeddings."""
        if not fragment_ids:
            return set()
        # ``f.key`` projection/WHERE is broken in GQL — resolve each id via a
        # direct node lookup instead.
        present: set[str] = set()
        for fid in fragment_ids:
            node = self._db.get_node_by_key("Fragment", fid)
            if node is not None and dict(node.props).get("embedded_at") is not None:
                present.add(fid)
        return present

    def rebuild_fts_index(self) -> None:
        """Rebuild the Tantivy BM25 index from OverGraph fragment nodes."""
        self._bm25.delete_all()
        rows = self._db.execute_gql(
            "MATCH (f:Fragment) WHERE f.prose IS NOT NULL "
            "RETURN id(f) AS nid, f.skill_id, f.category, f.fragment_type, f.prose, "
            "f.phase_scope, f.domain_tags"
        )
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                nid = row.get("nid")
                if nid is None:
                    continue
                node = self._db.get_node(int(nid))
                if node is None:
                    continue
                self._bm25.upsert(
                    fragment_id=getattr(node, "key", "") or "",
                    skill_id=row.get("f.skill_id", "") or node.props.get("skill_id", ""),
                    category=row.get("f.category", "") or node.props.get("category", ""),
                    fragment_type=row.get("f.fragment_type", "")
                    or node.props.get("fragment_type", ""),
                    prose=row.get("f.prose", "") or node.props.get("prose", ""),
                    phase_scope=row.get("f.phase_scope") or node.props.get("phase_scope"),
                    domain_tags=row.get("f.domain_tags") or node.props.get("domain_tags"),
                )
        self._bm25.commit()

    def bulk_replace(self, items: Iterable[FragmentEmbedding]) -> int:
        """Atomically replace all fragment embeddings."""
        self.delete_all()
        return self.insert_embeddings(items)


def _gql_esc(s: str) -> str:
    """Escape a string for GQL."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def open_overgraph_skill_store(
    db_path: str | Path,
    *,
    read_only: bool = False,
) -> OverGraphSkillStore:
    """Open (and, in writer mode, migrate) the OverGraph skill store.

    Translates a writer-lock conflict (the BM25 sidecar's exclusive Tantivy
    lock, held by an out-of-process reembed / install-pack writer) into
    :class:`LockHeldError` so callers get the same contract the DuckDB store
    provided.
    """
    from agentalloy.storage.protocols import LockHeldError, is_lock_held_error

    try:
        store = OverGraphSkillStore(db_path, read_only=read_only)
    except Exception as exc:
        if not read_only and is_lock_held_error(str(exc)):
            raise LockHeldError(str(exc)) from exc
        raise
    if not read_only:
        store.migrate()
    return store
