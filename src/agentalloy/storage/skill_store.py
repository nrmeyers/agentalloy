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

logger = logging.getLogger(__name__)


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


class SkillStoreError(Exception):
    """Base for skill-store errors."""


class LockHeldError(SkillStoreError):
    """Raised when the DuckDB file write-lock is held by another process.

    In v5 this is benign and transient (a reembed/ingest holds the writer for a
    short window); callers retry rather than stop the service. Distinct from the
    legacy LOCK_HELD_REMEDIATION which told users to stop the service.
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


def open_skill_store(db_path: str | Path, *, read_only: bool = False) -> DuckDBSkillStore:
    """Open (and, in writer mode, migrate) the skill store at ``db_path``."""
    store = DuckDBSkillStore(str(db_path), read_only=read_only).open()
    if not read_only:
        store.migrate()
    return store
