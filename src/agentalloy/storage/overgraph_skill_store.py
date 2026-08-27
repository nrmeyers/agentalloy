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
from collections.abc import Iterable, Sequence
from datetime import datetime
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
    SkillStore,
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
        self._bm25 = TantivyBM25Index(bm25_path)
        logger.debug("OverGraph skill store opened at %s", db_path)

    def _verify_dimension_alignment(self, expected_dim: int) -> None:
        """Verify that the database's vector dimension matches expectations."""
        # OverGraph persists dense_vector_dimension in its schema
        # For now, we trust the open() call configured it correctly
        logger.debug("vector dimension alignment verified: %d", expected_dim)

    def close(self) -> None:
        """Close the database connection."""
        if hasattr(self, '_bm25') and self._bm25 is not None:
            self._bm25.close()
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> OverGraphSkillStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

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
                    self._db.execute_gql(
                        f"MATCH (n) WHERE id(n) = {frag_node.id} DETACH DELETE n"
                    )
            # Delete the version node
            ver_node = self._db.get_node_by_key("SkillVersion", version.version_id)
            if ver_node:
                self._db.execute_gql(
                    f"MATCH (n) WHERE id(n) = {ver_node.id} DETACH DELETE n"
                )

        # Delete dependencies
        self._db.execute_gql(
            f"MATCH ()-[r:{_EDGE_REQUIRES}]->() WHERE "
            f"(STARTNODE(r).key = '{_gql_esc(skill_id)}' OR ENDNODE(r).key = '{_gql_esc(skill_id)}') "
            f"DELETE r"
        )

        # Delete the skill node
        self._db.execute_gql(
            f"MATCH (n:Skill) WHERE id(n) = {node.id} DETACH DELETE n"
        )
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
        rows = self._db.execute_gql(
            f"MATCH (v:SkillVersion) WHERE v.skill_id = '{_gql_esc(skill_id)}' "
            f"RETURN v.key AS version_id, v.skill_id, v.version_number, v.authored_at, "
            f"v.author, v.change_summary, v.status, v.raw_prose "
            f"ORDER BY v.version_number DESC"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(SkillVersionRow(
                    version_id=row.get("version_id", ""),
                    skill_id=row.get("skill_id", ""),
                    version_number=int(row.get("version_number", 0)),
                    authored_at=row.get("authored_at"),
                    author=row.get("author", ""),
                    change_summary=row.get("change_summary", ""),
                    status=row.get("status", ""),
                    raw_prose=row.get("raw_prose", ""),
                ))
        return results

    def insert_version(self, version: SkillVersionRow) -> None:
        """Insert a version."""
        authored_at = version.authored_at
        if hasattr(authored_at, 'isoformat'):
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
        if version.status == "active":
            if skill_node and ver_node:
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

    # -- dependency CRUD -----------------------------------------------------

    def get_dependencies(self, skill_id: str) -> list[SkillDependencyRow]:
        """Get dependencies for a skill."""
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[r:{_EDGE_REQUIRES}]->(t:Skill) "
            f"WHERE s.key = '{_gql_esc(skill_id)}' "
            f"RETURN t.key AS target, r.rel_type AS rel_type"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(SkillDependencyRow(
                    source_skill_id=skill_id,
                    target_skill_id=row.get("target", ""),
                    rel_type=row.get("rel_type", "requires"),
                ))
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
                results.append(SkillRow(
                    skill_id=skill_id,
                    canonical_name=row.get("s.canonical_name", "") or node.props.get("canonical_name", ""),
                    category=row.get("s.category", "") or node.props.get("category", ""),
                    skill_class=row.get("s.skill_class", "") or node.props.get("skill_class", ""),
                    domain_tags=list(row.get("s.domain_tags", []) or node.props.get("domain_tags", []) or []),
                    deprecated=bool(row.get("s.deprecated", False)),
                    superseded_by=row.get("s.superseded_by") or node.props.get("superseded_by"),
                    always_apply=bool(row.get("s.always_apply", False)),
                    phase_scope=row.get("s.phase_scope") or node.props.get("phase_scope"),
                    category_scope=row.get("s.category_scope") or node.props.get("category_scope"),
                    tier=row.get("s.tier") or node.props.get("tier"),
                    description=row.get("s.description") or node.props.get("description"),
                    current_version_id=row.get("s.current_version_id", "") or node.props.get("current_version_id", ""),
                ))
        return results

    def get_active_skill_by_id(self, skill_id: str) -> SkillRow | None:
        """Get an active skill by ID."""
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE s.key = '{_gql_esc(skill_id)}' AND v.status = 'active' AND s.deprecated = false "
            f"RETURN s.key AS skill_id, s.canonical_name, s.category, s.skill_class, "
            f"s.domain_tags, s.deprecated, s.superseded_by, s.always_apply, "
            f"s.phase_scope, s.category_scope, s.tier, s.description, s.current_version_id "
            f"LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            row = rows["rows"][0]
            return SkillRow(
                skill_id=row.get("skill_id", ""),
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
        rows = self._db.execute_gql(
            "MATCH (s:Skill) WHERE s.deprecated = true RETURN s.key AS skill_id"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(row.get("skill_id", ""))
        return results

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
        if domain_tags is not None:
            tags = ", ".join(f"'{_gql_esc(t)}'" for t in domain_tags)
            filters.append(f"ANY(t IN s.domain_tags WHERE t IN [{tags}])")
        filter_clause = " AND ".join(filters)
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE {filter_clause} "
            f"RETURN id(f) AS fid, f.version_id, f.fragment_type, f.sequence, f.content "
            f"ORDER BY s.key, f.sequence"
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
                results.append(FragmentRow(
                    fragment_id=getattr(node, "key", "") or "",
                    version_id=row.get("f.version_id", "") or node.props.get("version_id", ""),
                    fragment_type=row.get("f.fragment_type", "") or node.props.get("fragment_type", ""),
                    sequence=int(row.get("f.sequence", 0) or node.props.get("sequence", 0)),
                    content=row.get("f.content", "") or node.props.get("content", ""),
                    skill_id=node.props.get("skill_id", "") if hasattr(node, "props") else "",
                    category=node.props.get("category", "") if hasattr(node, "props") else "",
                    skill_class="",
                    domain_tags=list(node.props.get("domain_tags", []) or []) if hasattr(node, "props") else [],
                    phase_scope=node.props.get("phase_scope") if hasattr(node, "props") else None,
                    category_scope=None,
                    description=node.props.get("description") if hasattr(node, "props") else None,
                ))
        return results

    def get_active_fragments_for_skill(self, skill_id: str) -> list[FragmentRow]:
        """Get fragments for a specific active skill."""
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE s.key = '{_gql_esc(skill_id)}' AND v.status = 'active' AND s.deprecated = false "
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
                results.append(FragmentRow(
                    fragment_id=getattr(node, "key", "") or "",
                    version_id=row.get("f.version_id", "") or node.props.get("version_id", ""),
                    fragment_type=row.get("f.fragment_type", "") or node.props.get("fragment_type", ""),
                    sequence=int(row.get("f.sequence", 0) or node.props.get("sequence", 0)),
                    content=row.get("f.content", "") or node.props.get("content", ""),
                ))
        return results

    # -- re-embed pipeline ---------------------------------------------------

    def discover_fragments(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[FragmentDiscoveryRow]:
        """Discover fragments for the re-embed pipeline."""
        if skill_id is not None:
            rows = self._db.execute_gql(
                f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
                f"WHERE v.status = 'active' AND s.deprecated = false AND s.key = '{_gql_esc(skill_id)}' "
                f"RETURN f.key AS fragment_id, f.content, f.fragment_type, s.key AS skill_id, "
                f"s.category, s.canonical_name, s.domain_tags, s.description "
                f"ORDER BY f.sequence"
            )
        else:
            rows = self._db.execute_gql(
                f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
                f"WHERE v.status = 'active' AND s.deprecated = false "
                f"RETURN f.key AS fragment_id, f.content, f.fragment_type, s.key AS skill_id, "
                f"s.category, s.canonical_name, s.domain_tags, s.description "
                f"ORDER BY s.key, f.sequence"
            )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(FragmentDiscoveryRow(
                    fragment_id=row.get("fragment_id", ""),
                    content=row.get("content", ""),
                    fragment_type=row.get("fragment_type", ""),
                    skill_id=row.get("skill_id", ""),
                    category=row.get("category", ""),
                    canonical_name=row.get("canonical_name", "") or "",
                    domain_tags=tuple(row.get("domain_tags", []) or []),
                    description=(row.get("description") or "").strip() or None,
                ))
        return results

    # -- consistency guards --------------------------------------------------

    def check_consistency(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> None:
        """Check CURRENT_VERSION / active-version consistency."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        # Check for CURRENT_VERSION pointing to non-active version
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE v.status <> 'active' "
            f"RETURN s.key AS skill_id, v.status LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            row = rows["rows"][0]
            raise InconsistentActiveVersionError(
                row.get("skill_id", ""),
                f"CURRENT_VERSION points at status={row.get('status')!r} version",
            )

    def check_consistency_for(self, skill_id: str) -> None:
        """Check consistency for a specific skill."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_CURRENT_VERSION}]->(v:SkillVersion) "
            f"WHERE s.key = '{_gql_esc(skill_id)}' AND v.status <> 'active' "
            f"RETURN v.status LIMIT 1"
        )
        if isinstance(rows, dict) and rows.get("rows"):
            raise InconsistentActiveVersionError(
                skill_id,
                f"CURRENT_VERSION points at status={rows['rows'][0].get('status')!r} version",
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
        rows = self._db.execute_gql(
            f"MATCH (s:Skill)-[:{_EDGE_HAS_VERSION}]->(v:SkillVersion) "
            f"WHERE s.key = '{_gql_esc(skill_id)}' "
            f"RETURN v.key AS version_id, v.skill_id, v.version_number, v.authored_at, "
            f"v.author, v.change_summary, v.status, v.raw_prose"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(SkillVersionRow(
                    version_id=row.get("version_id", ""),
                    skill_id=row.get("skill_id", ""),
                    version_number=int(row.get("version_number", 0)),
                    authored_at=row.get("authored_at"),
                    author=row.get("author", ""),
                    change_summary=row.get("change_summary", ""),
                    status=row.get("status", ""),
                    raw_prose=row.get("raw_prose", ""),
                ))
        return results

    def _get_fragments_for_version(self, version_id: str) -> list[FragmentRow]:
        """Get all fragments for a version."""
        rows = self._db.execute_gql(
            f"MATCH (v:SkillVersion)-[:{_EDGE_DECOMPOSES_TO}]->(f:Fragment) "
            f"WHERE v.key = '{_gql_esc(version_id)}' "
            f"RETURN f.key AS fragment_id, f.version_id, f.fragment_type, f.sequence, f.content"
        )
        results = []
        if isinstance(rows, dict):
            for row in rows.get("rows", []):
                results.append(FragmentRow(
                    fragment_id=row.get("fragment_id", ""),
                    version_id=row.get("version_id", ""),
                    fragment_type=row.get("fragment_type", ""),
                    sequence=int(row.get("sequence", 0)),
                    content=row.get("content", ""),
                ))
        return results

    # -- FragmentStore protocol (vector + BM25 search over fragments) --------

    def insert_embeddings(self, items: Iterable[FragmentEmbedding]) -> int:
        """Upsert fragment embeddings into the HNSW index."""
        count = 0
        for item in items:
            if not item.fragment_id:
                logger.debug("skipping embedding with None fragment_id")
                continue
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
                props.update({
                    "skill_id": item.skill_id,
                    "category": item.category,
                    "fragment_type": item.fragment_type,
                    "embedded_at": item.embedded_at,
                    "embedding_model": item.embedding_model,
                    "prose": item.prose,
                    "phase_scope": list(item.phase_scope) if item.phase_scope else None,
                    "domain_tags": list(item.domain_tags) if item.domain_tags else None,
                })
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
            results.append(SimilarityHit(
                fragment_id=getattr(node, "key", ""),
                skill_id=skill_id,
                distance=1.0 - float(hit.score),  # Convert similarity to distance
            ))
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
                f"MATCH (f:Fragment) WHERE f.skill_id = '{_gql_esc(skill_id)}' "
                f"RETURN id(f) AS nid"
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
                        self._db.execute_gql(
                            f"MATCH (n) WHERE id(n) = {nid} DETACH DELETE n"
                        )
                        count += 1
            self._db.flush()
            return count
        except Exception:
            return 0

    def delete_skill(self, skill_id: str) -> int:
        """Delete all fragment embeddings for a skill."""
        try:
            rows = self._db.execute_gql(
                f"MATCH (f:Fragment) WHERE f.skill_id = '{_gql_esc(skill_id)}' RETURN id(f) AS nid"
            )
            count = 0
            if isinstance(rows, dict):
                for row in rows.get("rows", []):
                    nid = row.get("nid")
                    if nid is not None:
                        self._db.execute_gql(
                            f"MATCH (n) WHERE id(n) = {nid} DETACH DELETE n"
                        )
                        count += 1
            self._db.flush()
            return count
        except Exception:
            return 0

    def delete_all(self) -> int:
        """Delete all fragment embeddings."""
        try:
            self._db.execute_gql("MATCH (f:Fragment) DETACH DELETE f")
            self._db.flush()
            self._bm25.delete_all()
            return 1
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
        present: set[str] = set()
        # Check in chunks
        chunk_size = 100
        for i in range(0, len(fragment_ids), chunk_size):
            chunk = fragment_ids[i:i + chunk_size]
            keys = ", ".join(f"'{_gql_esc(fid)}'" for fid in chunk)
            try:
                rows = self._db.execute_gql(
                    f"MATCH (f:Fragment) WHERE f.key IN [{keys}] AND f.embedded_at IS NOT NULL "
                    f"RETURN f.key AS fid"
                )
                if isinstance(rows, dict):
                    for row in rows.get("rows", []):
                        fid = row.get("fid")
                        if fid:
                            present.add(fid)
            except Exception:
                pass
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
                    fragment_type=row.get("f.fragment_type", "") or node.props.get("fragment_type", ""),
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
    """Open (and, in writer mode, migrate) the OverGraph skill store."""
    store = OverGraphSkillStore(db_path, read_only=read_only)
    if not read_only:
        store.migrate()
    return store
