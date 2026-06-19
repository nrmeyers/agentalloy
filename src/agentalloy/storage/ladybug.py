"""LadybugDB (Kuzu) adapter with schema migration.

LadybugDB stores graph structure only (Skill, SkillVersion, Fragment nodes
plus their relationships). Fragment embeddings live in DuckDB — see
``agentalloy.storage.vector_store``. The Kùzu VECTOR extension is NOT
loaded; per v5.3 directive its load-time circular dependency is
incompatible with restartable FastAPI service lifecycle.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import ladybug

from agentalloy.storage.schema_cypher import ALTER_TABLES, NODE_TABLES, REL_TABLES

logger = logging.getLogger(__name__)

# Remediation for LadybugDB's single-writer lock failure. Surfaced by
# install-packs / reembed when an ingest or DB open trips the lock
# (issue #84: the raw error never told users to stop the service).
LOCK_HELD_REMEDIATION = (
    "Another process holds the corpus DB lock (usually the running agentalloy "
    "service). Stop it first, then retry: `systemctl --user stop agentalloy` "
    "(systemd install) or `agentalloy server-stop` (manual launch). A plain kill "
    "won't stick for a systemd unit — systemd respawns it."
)


def is_lock_held_error(text: str) -> bool:
    """True if ``text`` looks like LadybugDB's single-writer lock failure."""
    return "Could not set lock on file" in text or "Lock is held by PID" in text


class LadybugStore:
    """Thin wrapper around ``ladybug.Database`` + ``ladybug.Connection``.

    Owns the connection lifecycle. Safe to use as a context manager. Single-process
    service — one store instance is created at app startup and shared across requests.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: ladybug.Database | None = None
        self._conn: ladybug.Connection | None = None

    def open(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = ladybug.Database(self._db_path)
        self._conn = ladybug.Connection(self._db)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover — defensive; Kùzu may already be closed
                logger.debug("failed to close Ladybug connection", exc_info=True)
            self._conn = None
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # pragma: no cover — defensive
                logger.debug("failed to close Ladybug database", exc_info=True)
            self._db = None

    def __enter__(self) -> LadybugStore:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        """Execute a Cypher statement and materialize rows eagerly."""
        if self._conn is None:
            raise RuntimeError("LadybugStore is not open")
        result = self._conn.execute(cypher, parameters=params or {})
        # ladybug returns QueryResult or list; normalize.
        results: list[ladybug.QueryResult]
        results = result if isinstance(result, list) else [result]
        out: list[list[Any]] = []
        for r in results:
            while r.has_next():
                out.append(cast("list[Any]", r.get_next()))
        return out

    def scalar(self, cypher: str, params: dict[str, Any] | None = None) -> Any:
        rows = self.execute(cypher, params)
        if not rows:
            return None
        return rows[0][0]

    def iter_rows(self, cypher: str, params: dict[str, Any] | None = None) -> Iterator[list[Any]]:
        yield from self.execute(cypher, params)

    def delete_skill(self, skill_id: str) -> int:
        """Delete a skill and all its versions/fragments. Returns number of nodes deleted."""
        if self._conn is None:
            raise RuntimeError("LadybugStore is not open")
        self._conn.execute(
            """
            MATCH (s:Skill {skill_id: $id})
            OPTIONAL MATCH (s)-[:HAS_VERSION]->(v:SkillVersion)
            OPTIONAL MATCH (v)-[:DECOMPOSES_TO]->(f:Fragment)
            DETACH DELETE s, v, f
            """,
            {"id": skill_id},
        )
        return 1  # DETACH DELETE returns rows deleted; we just need a truthy count

    def rollback_skill(self, skill_id: str) -> None:
        """Roll back a single skill insertion (delete skill + versions + fragments)."""
        try:
            self.delete_skill(skill_id)
        except Exception as exc:
            logger.error("rollback_skill failed for %s: %s", skill_id, exc)

    def rollback_batch(self, skill_ids: list[str]) -> None:
        """Roll back all skills in a failed batch. Safe to call on partial list."""
        for sid in skill_ids:
            self.rollback_skill(sid)

    def migrate(self) -> None:
        """Create node tables, rel tables, and apply ALTER TABLE migrations. Idempotent.

        No vector index — embeddings live in DuckDB's ``fragment_embeddings``
        table. See ``agentalloy.storage.vector_store``.
        """
        if self._conn is None:
            raise RuntimeError("LadybugStore is not open")
        created_tables: list[str] = []
        for ddl in NODE_TABLES:
            self._conn.execute(ddl)
            created_tables.append(_first_identifier_after(ddl, "TABLE"))
        for ddl in REL_TABLES:
            self._conn.execute(ddl)
            created_tables.append(_first_identifier_after(ddl, "TABLE"))
        # Apply ALTER TABLE migrations for columns added after initial schema.
        # Fresh DBs already have these columns from CREATE TABLE — that one
        # error is expected and skipped. Anything else (parser errors, lock
        # contention) must surface: a blanket suppress here hid years of
        # silently-failing ALTERs behind a wrong `ALTER NODE TABLE` spelling.
        for ddl in ALTER_TABLES:
            try:
                self._conn.execute(ddl)
            except RuntimeError as exc:
                # LadybugDB phrases the benign case "Skill table already has
                # property <name>."; older builds say "already exists".
                msg = str(exc).lower()
                if "already has property" in msg or "already exists" in msg:
                    continue
                raise RuntimeError(f"ladybug migration failed: {ddl!r}: {exc}") from exc
        logger.debug("ladybug_migrate ok tables=%s", created_tables)


def _first_identifier_after(ddl: str, keyword: str) -> str:
    """Extract the identifier following ``keyword`` in a DDL string."""
    tokens = ddl.replace("(", " ").split()
    for i, tok in enumerate(tokens):
        if tok.upper() == keyword and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            # Skip `IF NOT EXISTS` phrase
            if nxt.upper() == "IF" and i + 4 < len(tokens):
                return tokens[i + 4]
            return nxt
    return "?"
