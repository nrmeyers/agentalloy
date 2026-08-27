"""DuckDB-backed skill store (``agentalloy.duck``) — replaces the legacy graph store.

Holds the skill graph folded into relational tables (skills / skill_versions /
fragments / skill_dependencies) plus the ``corpus_meta`` kv. This is the
SQL-canonical source of truth for fragment content + metadata (decision D7);
the Lance ``fragments`` dataset is a derived index built from it.

Concurrency (decisions D4 / OQ#4): DuckDB is single-writer across processes,
and a writer can only attach while NO other process holds the file — read-only
handles included. The serving process holds this store read-only for its whole
lifetime (live inspection reads come from it, not just the boot-time
``RuntimeCache`` load), so out-of-process writers (the ingest / reembed CLIs)
must stop the service first — ``agentalloy reembed`` does that automatically —
and in-process writers (the web UI's reembed / pack install) wrap the write in
:meth:`DuckDBSkillStore.released`, which closes the handle for the duration
and reconnects afterwards.

The public surface mirrors the legacy skill-store surface (``execute`` / ``scalar`` /
``migrate`` / ``delete_skill`` / ``rollback_skill`` / ``rollback_batch``) so the
Cypher→SQL port at call sites changes only the query language, plus the
``set_meta`` / ``get_meta`` kv that moved here from the old ``VectorStore``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb

from agentalloy.storage.protocols import (
    FragmentDiscoveryRow,
    FragmentRow,
    SkillDependencyRow,
    SkillRow,
    SkillVersionRow,
)

logger = logging.getLogger(__name__)


class SkillStoreError(Exception):
    """Base exception for skill store errors."""


class LockHeldError(SkillStoreError):
    """Raised when the DB file is locked by another writer."""


# Single owned schema. Idempotent CREATE IF NOT EXISTS, run once per writer open —
# no per-open ALTER probes (the old 16-ALTER churn is gone; columns below already
# represent the post-migration state).
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id           TEXT PRIMARY KEY,
    canonical_name     TEXT NOT NULL,
    category           TEXT,
    skill_class        TEXT,
    domain_tags        TEXT[],
    deprecated         BOOLEAN DEFAULT false,
    superseded_by      TEXT,
    always_apply       BOOLEAN DEFAULT false,
    phase_scope        TEXT[],
    category_scope     TEXT[],
    tier               TEXT,
    description        TEXT,
    current_version_id TEXT
);

CREATE TABLE IF NOT EXISTS skill_versions (
    version_id     TEXT PRIMARY KEY,
    skill_id       TEXT NOT NULL,
    version_number BIGINT,
    authored_at    TIMESTAMP,
    author         TEXT,
    change_summary TEXT,
    status         TEXT,
    raw_prose      TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id);

CREATE TABLE IF NOT EXISTS fragments (
    fragment_id   TEXT PRIMARY KEY,
    version_id    TEXT NOT NULL,
    fragment_type TEXT,
    sequence      BIGINT,
    content       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fragments_version ON fragments(version_id);

CREATE TABLE IF NOT EXISTS skill_dependencies (
    source_skill_id TEXT NOT NULL,
    target_skill_id TEXT NOT NULL,
    rel_type        TEXT NOT NULL DEFAULT 'requires',
    PRIMARY KEY (source_skill_id, target_skill_id, rel_type)
);

CREATE TABLE IF NOT EXISTS corpus_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
"""


def is_lock_held_error(text: str) -> bool:
    """True if ``text`` looks like a DuckDB file write-lock conflict."""
    t = text.lower()
    return "could not set lock" in t or "conflicting lock" in t or "lock on file" in t


class DuckDBSkillStore:
    """Thin wrapper over a DuckDB connection to ``agentalloy.duck``.

    Single-process writer; multiple read-only processes are allowed only when no
    writer is open (DuckDB cross-process locking). Use as a context manager to
    guarantee the handle is released.
    """

    def __init__(self, db_path: str, *, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> DuckDBSkillStore:
        if not self._read_only:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = duckdb.connect(self._db_path, read_only=self._read_only)
        except duckdb.Error as exc:  # pragma: no cover - lock contention path
            if is_lock_held_error(str(exc)):
                raise LockHeldError(str(exc)) from exc
            raise
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("failed to close DuckDB skill connection", exc_info=True)
            self._conn = None

    def __enter__(self) -> DuckDBSkillStore:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def released(self) -> Iterator[None]:
        """Temporarily release the DuckDB file handle; reconnect on exit.

        A writer can only attach to ``agentalloy.duck`` while no other
        connection — same process or not, read-only or not — holds the file.
        The long-lived service keeps this store open read-only, so in-process
        writers (the web UI's reembed / pack install) wrap their write in this
        context manager. The object stays valid for everyone holding a
        reference; operations *during* the window raise ``RuntimeError``
        ("not open").
        """
        self.close()
        try:
            yield
        finally:
            self.open()

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("SkillStore is not open")
        return self._conn

    # -- query workhorse (mirrors the legacy store's execute/scalar/iter_rows) --

    def execute(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        """Execute SQL and materialize result rows eagerly.

        ``params`` may be a dict (named ``$name`` parameters — the style the
        ported Cypher reads use) or a sequence (positional ``?``). Returns []
        for statements without a result set.
        """
        cur = self.conn.execute(sql, params) if params is not None else self.conn.execute(sql)
        try:
            return cur.fetchall()
        except Exception:
            return []

    def scalar(self, sql: str, params: Any = None) -> Any:
        rows = self.execute(sql, params)
        if not rows:
            return None
        return rows[0][0]

    def iter_rows(self, sql: str, params: Any = None) -> Iterator[tuple[Any, ...]]:
        yield from self.execute(sql, params)

    # -- transactions --------------------------------------------------------

    def begin(self) -> None:
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema. Idempotent. Writer-mode only (RO can't create)."""
        if self._read_only:
            raise RuntimeError("cannot migrate a read-only SkillStore")
        self.conn.execute(_SCHEMA_DDL)
        logger.debug("agentalloy.duck schema ensured")

    # -- skill lifecycle (deletes / rollback) --------------------------------

    def delete_skill(self, skill_id: str) -> int:
        """Delete a skill and all its versions/fragments/deps. Returns skills removed.

        Cascade order respects the FK direction (fragments -> versions -> deps ->
        skill); ports the legacy graph ``DETACH DELETE`` (E1 in the port table).
        """
        n = self.scalar("SELECT count(*) FROM skills WHERE skill_id = ?", [skill_id])
        self.conn.execute(
            "DELETE FROM fragments WHERE version_id IN "
            "(SELECT version_id FROM skill_versions WHERE skill_id = ?)",
            [skill_id],
        )
        self.conn.execute("DELETE FROM skill_versions WHERE skill_id = ?", [skill_id])
        self.conn.execute(
            "DELETE FROM skill_dependencies WHERE source_skill_id = ? OR target_skill_id = ?",
            [skill_id, skill_id],
        )
        self.conn.execute("DELETE FROM skills WHERE skill_id = ?", [skill_id])
        return int(n or 0)

    def rollback_skill(self, skill_id: str) -> None:
        """Roll back a single skill insertion. Soft-fails (logs) like the original."""
        try:
            self.delete_skill(skill_id)
        except Exception as exc:
            logger.error("rollback_skill failed for %s: %s", skill_id, exc)

    def rollback_batch(self, skill_ids: Sequence[str]) -> None:
        for sid in skill_ids:
            self.rollback_skill(sid)

    # -- corpus_meta kv (moved here from VectorStore; lives in agentalloy.duck) --

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a corpus_meta key/value with an updated_at stamp.

        Writer-mode only. Called by reembed (which holds the write lock) to record
        e.g. ``card_index`` mode and ``schema_version``.
        """
        self.conn.execute(
            """
            INSERT INTO corpus_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                            updated_at = excluded.updated_at
            """,
            [key, value, int(time.time())],
        )

    def get_meta(self, key: str) -> str | None:
        """Return the corpus_meta value for ``key``, or None if unset/absent."""
        try:
            row = self.conn.execute("SELECT value FROM corpus_meta WHERE key = ?", [key]).fetchone()
        except Exception:  # noqa: BLE001 - table absent on a not-yet-migrated corpus
            return None
        return str(row[0]) if row else None

    # -- higher-level interface (new protocol) --------------------------------

    def get_skill(self, skill_id: str) -> SkillRow | None:
        """Get a skill by ID."""
        rows = self.execute(
            "SELECT skill_id, canonical_name, category, skill_class, domain_tags, "
            "deprecated, superseded_by, always_apply, phase_scope, category_scope, "
            "tier, description, current_version_id "
            "FROM skills WHERE skill_id = ?",
            [skill_id],
        )
        if not rows:
            return None
        r = rows[0]
        return SkillRow(
            skill_id=str(r[0]),
            canonical_name=str(r[1]),
            category=str(r[2]),
            skill_class=str(r[3]),
            domain_tags=list(r[4] or []),
            deprecated=bool(r[5]),
            superseded_by=str(r[6]) if r[6] else None,
            always_apply=bool(r[7]),
            phase_scope=list(r[8]) if r[8] else None,
            category_scope=list(r[9]) if r[9] else None,
            tier=str(r[10]) if r[10] else None,
            description=str(r[11]) if r[11] else None,
            current_version_id=str(r[12]),
        )

    def get_skill_id_by_name(self, canonical_name: str) -> str | None:
        """Get skill_id by canonical_name."""
        return self.scalar(
            "SELECT skill_id FROM skills WHERE canonical_name = ?",
            [canonical_name],
        )

    def insert_skill(self, skill: SkillRow) -> None:
        """Insert or replace a skill."""
        self.execute(
            "INSERT OR REPLACE INTO skills (skill_id, canonical_name, category, skill_class, domain_tags, "
            "deprecated, superseded_by, always_apply, phase_scope, category_scope, tier, "
            "description, current_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                skill.skill_id,
                skill.canonical_name,
                skill.category,
                skill.skill_class,
                skill.domain_tags,
                skill.deprecated,
                skill.superseded_by,
                skill.always_apply,
                skill.phase_scope,
                skill.category_scope,
                skill.tier,
                skill.description,
                skill.current_version_id,
            ],
        )

    def get_version(self, version_id: str) -> SkillVersionRow | None:
        """Get a version by ID."""
        rows = self.execute(
            "SELECT version_id, skill_id, version_number, authored_at, author, "
            "change_summary, status, raw_prose FROM skill_versions WHERE version_id = ?",
            [version_id],
        )
        if not rows:
            return None
        r = rows[0]
        return SkillVersionRow(
            version_id=str(r[0]),
            skill_id=str(r[1]),
            version_number=int(r[2]),
            authored_at=r[3],
            author=str(r[4]),
            change_summary=str(r[5]),
            status=str(r[6]),
            raw_prose=str(r[7]),
        )

    def get_versions_by_skill(self, skill_id: str) -> list[SkillVersionRow]:
        """Get all versions for a skill, ordered by version_number DESC."""
        rows = self.execute(
            "SELECT version_id, skill_id, version_number, authored_at, author, "
            "change_summary, status, raw_prose FROM skill_versions "
            "WHERE skill_id = ? ORDER BY version_number DESC",
            [skill_id],
        )
        return [
            SkillVersionRow(
                version_id=str(r[0]),
                skill_id=str(r[1]),
                version_number=int(r[2]),
                authored_at=r[3],
                author=str(r[4]),
                change_summary=str(r[5]),
                status=str(r[6]),
                raw_prose=str(r[7]),
            )
            for r in rows
        ]

    def insert_version(self, version: SkillVersionRow) -> None:
        """Insert or replace a version."""
        self.execute(
            "INSERT OR REPLACE INTO skill_versions (version_id, skill_id, version_number, authored_at, "
            "author, change_summary, status, raw_prose) VALUES (?,?,?,?,?,?,?,?)",
            [
                version.version_id,
                version.skill_id,
                version.version_number,
                version.authored_at,
                version.author,
                version.change_summary,
                version.status,
                version.raw_prose,
            ],
        )

    def get_fragment(self, fragment_id: str) -> FragmentRow | None:
        """Get a fragment by ID."""
        rows = self.execute(
            "SELECT fragment_id, version_id, fragment_type, sequence, content "
            "FROM fragments WHERE fragment_id = ?",
            [fragment_id],
        )
        if not rows:
            return None
        r = rows[0]
        return FragmentRow(
            fragment_id=str(r[0]),
            version_id=str(r[1]),
            fragment_type=str(r[2]),
            sequence=int(r[3]),
            content=str(r[4]),
        )

    def insert_fragment(self, fragment: FragmentRow) -> None:
        """Insert or replace a fragment."""
        self.execute(
            "INSERT OR REPLACE INTO fragments (fragment_id, version_id, fragment_type, sequence, content) "
            "VALUES (?,?,?,?,?)",
            [
                fragment.fragment_id,
                fragment.version_id,
                fragment.fragment_type,
                fragment.sequence,
                fragment.content,
            ],
        )

    def count_fragments(self) -> int:
        """Count all fragment rows (any version/skill status)."""
        return int(self.scalar("SELECT count(*) FROM fragments") or 0)

    def get_dependencies(self, skill_id: str) -> list[SkillDependencyRow]:
        """Get dependencies for a skill."""
        rows = self.execute(
            "SELECT source_skill_id, target_skill_id, rel_type "
            "FROM skill_dependencies WHERE source_skill_id = ?",
            [skill_id],
        )
        return [
            SkillDependencyRow(
                source_skill_id=str(r[0]),
                target_skill_id=str(r[1]),
                rel_type=str(r[2]),
            )
            for r in rows
        ]

    def insert_dependency(self, dep: SkillDependencyRow) -> None:
        """Insert a dependency (idempotent — an existing edge is kept)."""
        self.execute(
            "INSERT INTO skill_dependencies (source_skill_id, target_skill_id, rel_type) "
            "VALUES (?,?,?) ON CONFLICT DO NOTHING",
            [dep.source_skill_id, dep.target_skill_id, dep.rel_type],
        )

    def delete_dependencies(self, skill_id: str, rel_type: str | None = None) -> int:
        """Delete outgoing dependency edges for a skill. Returns rows removed.

        With ``rel_type`` given, only edges of that type are removed; otherwise
        all outgoing edges go (the re-ingest idempotency path).
        """
        if rel_type is None:
            where, params = "source_skill_id = ?", [skill_id]
        else:
            where, params = "source_skill_id = ? AND rel_type = ?", [skill_id, rel_type]
        n = self.scalar(f"SELECT count(*) FROM skill_dependencies WHERE {where}", params)
        self.execute(f"DELETE FROM skill_dependencies WHERE {where}", params)
        return int(n or 0)

    def get_active_skills(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> list[SkillRow]:
        """Get all active skills, optionally filtered by class."""
        filters = ["v.status = 'active'", "s.deprecated = false"]
        params: list[object] = []
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                filters.append("s.skill_class IN ?")
                params.append(list(skill_class))
            else:
                filters.append("s.skill_class = ?")
                params.append(skill_class)
        sql = (
            "SELECT s.skill_id, s.canonical_name, s.category, s.skill_class, s.domain_tags, "
            "s.deprecated, s.superseded_by, s.always_apply, s.phase_scope, s.category_scope, "
            "s.tier, s.description, s.current_version_id "
            "FROM skills s JOIN skill_versions v ON v.version_id = s.current_version_id "
            f"WHERE {' AND '.join(filters)} ORDER BY s.skill_id"
        )
        rows = self.execute(sql, params)
        return [
            SkillRow(
                skill_id=str(r[0]),
                canonical_name=str(r[1]),
                category=str(r[2]),
                skill_class=str(r[3]),
                domain_tags=list(r[4] or []),
                deprecated=bool(r[5]),
                superseded_by=str(r[6]) if r[6] else None,
                always_apply=bool(r[7]),
                phase_scope=list(r[8]) if r[8] else None,
                category_scope=list(r[9]) if r[9] else None,
                tier=str(r[10]) if r[10] else None,
                description=str(r[11]) if r[11] else None,
                current_version_id=str(r[12]),
            )
            for r in rows
        ]

    def get_active_skill_by_id(self, skill_id: str) -> SkillRow | None:
        """Get an active skill by ID."""
        rows = self.execute(
            "SELECT s.skill_id, s.canonical_name, s.category, s.skill_class, s.domain_tags, "
            "s.deprecated, s.superseded_by, s.always_apply, s.phase_scope, s.category_scope, "
            "s.tier, s.description, s.current_version_id "
            "FROM skills s JOIN skill_versions v ON v.version_id = s.current_version_id "
            "WHERE s.skill_id = ? AND v.status = 'active' AND s.deprecated = false",
            [skill_id],
        )
        if not rows:
            return None
        r = rows[0]
        return SkillRow(
            skill_id=str(r[0]),
            canonical_name=str(r[1]),
            category=str(r[2]),
            skill_class=str(r[3]),
            domain_tags=list(r[4] or []),
            deprecated=bool(r[5]),
            superseded_by=str(r[6]) if r[6] else None,
            always_apply=bool(r[7]),
            phase_scope=list(r[8]) if r[8] else None,
            category_scope=list(r[9]) if r[9] else None,
            tier=str(r[10]) if r[10] else None,
            description=str(r[11]) if r[11] else None,
            current_version_id=str(r[12]),
        )

    def get_deprecated_skill_ids(self) -> list[str]:
        """Get IDs of all deprecated skills."""
        rows = self.execute("SELECT skill_id FROM skills WHERE deprecated = true")
        return [str(r[0]) for r in rows]

    def count_skills(self) -> int:
        """Count all skill rows (any deprecation/version status)."""
        return int(self.scalar("SELECT count(*) FROM skills") or 0)

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
        params: list[object] = []
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                filters.append("s.skill_class IN ?")
                params.append(list(skill_class))
            else:
                filters.append("s.skill_class = ?")
                params.append(skill_class)
        if categories is not None and phases:
            filters.append(
                "(s.category IN ? OR (s.phase_scope IS NOT NULL AND s.phase_scope && ?))"
            )
            params.extend([categories, phases])
        elif categories is not None:
            filters.append("s.category IN ?")
            params.append(categories)
        elif phases:
            filters.append("(s.phase_scope IS NOT NULL AND s.phase_scope && ?)")
            params.append(phases)
        if domain_tags is not None:
            filters.append("(s.domain_tags IS NOT NULL AND s.domain_tags && ?)")
            params.append(domain_tags)
        sql = (
            "SELECT f.fragment_id, f.version_id, f.fragment_type, f.sequence, f.content, "
            "s.skill_id, s.category, s.skill_class, s.domain_tags, s.phase_scope, s.category_scope, s.description "
            "FROM fragments f "
            "JOIN skill_versions v ON f.version_id = v.version_id "
            "JOIN skills s ON v.version_id = s.current_version_id "
            f"WHERE {' AND '.join(filters)} ORDER BY s.skill_id, f.sequence"
        )
        rows = self.execute(sql, params)
        return [
            FragmentRow(
                fragment_id=str(r[0]),
                version_id=str(r[1]),
                fragment_type=str(r[2]),
                sequence=int(r[3]),
                content=str(r[4]),
                skill_id=str(r[5]),
                category=str(r[6]) if r[6] else "",
                skill_class=str(r[7]) if r[7] else "",
                domain_tags=list(r[8] or []),
                phase_scope=list(r[9]) if r[9] else None,
                category_scope=list(r[10]) if r[10] else None,
                description=str(r[11]) if r[11] else None,
            )
            for r in rows
        ]

    def get_active_fragments_for_skill(self, skill_id: str) -> list[FragmentRow]:
        """Get fragments for a specific active skill."""
        rows = self.execute(
            "SELECT f.fragment_id, f.version_id, f.fragment_type, f.sequence, f.content, "
            "s.skill_id, s.category, s.skill_class, s.domain_tags, s.phase_scope, s.category_scope, s.description "
            "FROM fragments f "
            "JOIN skill_versions v ON f.version_id = v.version_id "
            "JOIN skills s ON v.version_id = s.current_version_id "
            "WHERE s.skill_id = ? AND v.status = 'active' AND s.deprecated = false "
            "ORDER BY f.sequence",
            [skill_id],
        )
        return [
            FragmentRow(
                fragment_id=str(r[0]),
                version_id=str(r[1]),
                fragment_type=str(r[2]),
                sequence=int(r[3]),
                content=str(r[4]),
                skill_id=str(r[5]),
                category=str(r[6]) if r[6] else "",
                skill_class=str(r[7]) if r[7] else "",
                domain_tags=list(r[8] or []),
                phase_scope=list(r[9]) if r[9] else None,
                category_scope=list(r[10]) if r[10] else None,
                description=str(r[11]) if r[11] else None,
            )
            for r in rows
        ]

    def discover_fragments(
        self,
        *,
        skill_id: str | None = None,
    ) -> list[FragmentDiscoveryRow]:
        """Discover fragments for the re-embed pipeline."""
        if skill_id is not None:
            rows = self.execute(
                "SELECT f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category, "
                "s.canonical_name, s.domain_tags, s.description "
                "FROM skills s "
                "JOIN skill_versions v ON v.version_id = s.current_version_id "
                "JOIN fragments f ON f.version_id = v.version_id "
                "WHERE v.status = 'active' AND s.deprecated = false AND s.skill_id = ? "
                "ORDER BY f.sequence",
                [skill_id],
            )
        else:
            rows = self.execute(
                "SELECT f.fragment_id, f.content, f.fragment_type, s.skill_id, s.category, "
                "s.canonical_name, s.domain_tags, s.description "
                "FROM skills s "
                "JOIN skill_versions v ON v.version_id = s.current_version_id "
                "JOIN fragments f ON f.version_id = v.version_id "
                "WHERE v.status = 'active' AND s.deprecated = false "
                "ORDER BY s.skill_id, f.sequence"
            )
        return [
            FragmentDiscoveryRow(
                fragment_id=str(r[0]),
                content=str(r[1]),
                fragment_type=str(r[2]),
                skill_id=str(r[3]),
                category=str(r[4]),
                canonical_name=str(r[5]) if r[5] else "",
                domain_tags=tuple(r[6]) if r[6] else (),
                description=str(r[7]).strip() or None if r[7] else None,
            )
            for r in rows
        ]

    def check_consistency(
        self,
        *,
        skill_class: str | tuple[str, ...] | None = None,
    ) -> None:
        """Check CURRENT_VERSION / active-version consistency. Raises on mismatch."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        class_filter = ""
        params: list[object] = []
        if skill_class is not None:
            if isinstance(skill_class, tuple):
                class_filter = " AND s.skill_class IN ?"
                params.extend([list(skill_class)])
            else:
                class_filter = " AND s.skill_class = ?"
                params.append(skill_class)

        # (a) CURRENT_VERSION points at a non-active version
        rows = self.execute(
            "SELECT s.skill_id, v.status FROM skills s "
            "JOIN skill_versions v ON v.version_id = s.current_version_id "
            f"WHERE v.status <> 'active'{class_filter} LIMIT 1",
            params,
        )
        if rows:
            raise InconsistentActiveVersionError(
                str(rows[0][0]),
                f"CURRENT_VERSION points at status={rows[0][1]!r} version",
            )

        # (b) Active version exists but no CURRENT_VERSION
        rows = self.execute(
            "SELECT s.skill_id FROM skills s "
            "JOIN skill_versions av ON av.skill_id = s.skill_id AND av.status = 'active' "
            "LEFT JOIN skill_versions cur ON cur.version_id = s.current_version_id "
            f"WHERE cur.version_id IS NULL{class_filter} LIMIT 1",
            params,
        )
        if rows:
            raise InconsistentActiveVersionError(
                str(rows[0][0]),
                "active SkillVersion exists but no CURRENT_VERSION edge",
            )

    def check_consistency_for(self, skill_id: str) -> None:
        """Check consistency for a specific skill."""
        from agentalloy.reads.active import InconsistentActiveVersionError

        rows = self.execute(
            "SELECT v.status FROM skills s "
            "JOIN skill_versions v ON v.version_id = s.current_version_id "
            "WHERE s.skill_id = ? AND v.status <> 'active' LIMIT 1",
            [skill_id],
        )
        if rows:
            raise InconsistentActiveVersionError(
                skill_id,
                f"CURRENT_VERSION points at status={rows[0][0]!r} version",
            )

        rows = self.execute(
            "SELECT s.skill_id FROM skills s "
            "JOIN skill_versions av ON av.skill_id = s.skill_id AND av.status = 'active' "
            "LEFT JOIN skill_versions cur ON cur.version_id = s.current_version_id "
            "WHERE s.skill_id = ? AND cur.version_id IS NULL LIMIT 1",
            [skill_id],
        )
        if rows:
            raise InconsistentActiveVersionError(
                skill_id,
                "active SkillVersion exists but no CURRENT_VERSION edge",
            )

    def clear_all(self) -> None:
        """Clear all data (for fixtures/tests)."""
        self.execute("DELETE FROM fragments")
        self.execute("DELETE FROM skill_dependencies")
        self.execute("DELETE FROM skill_versions")
        self.execute("DELETE FROM skills")
        self.execute("DELETE FROM corpus_meta")


def open_skill_store(db_path: str | Path, *, read_only: bool = False) -> DuckDBSkillStore:
    """Open (and, in writer mode, migrate) the skill store at ``db_path``."""
    store = DuckDBSkillStore(str(db_path), read_only=read_only).open()
    if not read_only:
        store.migrate()
    return store
