"""DuckDB-backed SDD state store — replaces per-repo files with a session-aware store.

Holds the SDD lifecycle's runtime state (phase, cursor, announced, composed,
approved, banner-turns, free-reminded) as rows in a single ``sdd_state`` table
keyed by ``(repo, kind, session_key)``.  Session-scoped kinds (announced,
composed, banner-turns, free-reminded) carry a non-null ``session_key``;
repo-scoped kinds (phase, cursor, approved) have ``session_key IS NULL``.

The store is owned by the running per-repo service (single-writer DuckDB
file).  CLI verbs route through the service's HTTP API when it is running,
falling back to direct file writes when it is not.  After every store write
the mirror writer updates the corresponding ``.agentalloy/<kind>`` file so
legacy consumers (sidecar watcher, statusline) keep working.

Concurrency (decision D1 / D3): repo-scoped rows use lease-based ownership
with expiry.  When session B tries to write a row owned by session A, B
checks the lease — if valid, B receives a conflict signal; if expired, B
takes over.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sdd_state (
    repo               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    session_key        TEXT,
    value              TEXT NOT NULL,
    owner              TEXT,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_expires_at   TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sdd_state_repo_kind_session
    ON sdd_state (repo, kind, COALESCE(session_key, ''));

CREATE INDEX IF NOT EXISTS idx_sdd_state_kind_owner
    ON sdd_state (repo, kind, COALESCE(owner, ''));
"""

# State kinds and their properties
REPO_SCOPED_KINDS: frozenset[str] = frozenset({"phase", "cursor", "approved"})
SESSION_SCOPED_KINDS: frozenset[str] = frozenset(
    {"announced", "composed", "banner-turns", "free-reminded"}
)
LEASED_KINDS: frozenset[str] = frozenset({"phase", "approved"})


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseConflict:
    owner: str
    lease_expires_at: datetime
    message: str


@dataclass(frozen=True)
class StateWriteResult:
    success: bool
    kind: str
    value: str
    owner: str | None
    lease_expires_at: datetime | None
    conflict: LeaseConflict | None


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    owner: str | None
    lease_expires_at: datetime | None
    conflict: LeaseConflict | None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class StateStoreError(Exception):
    """Base for state-store errors."""


class DuckDBStateStore:
    """Thin wrapper over a DuckDB connection to the SDD state store.

    Single-process writer; multiple read-only processes are allowed only when
    no writer is open (DuckDB cross-process locking).  Use as a context
    manager to guarantee the handle is released.
    """

    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None:
        self._db_path = str(db_path)
        self._read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> DuckDBStateStore:
        if not self._read_only:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = duckdb.connect(self._db_path, read_only=self._read_only)
        except duckdb.Error as exc:
            raise StateStoreError(str(exc)) from exc
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.debug("failed to close DuckDB state connection", exc_info=True)
            self._conn = None

    def __enter__(self) -> DuckDBStateStore:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("StateStore is not open")
        return self._conn

    # -- query workhorse -----------------------------------------------------

    def execute(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
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

    def iter_rows(self, sql: str, params: Any = None) -> Any:
        yield from self.execute(sql, params)

    # -- repo slug (derived from db_path) ------------------------------------

    def _repo(self) -> str:
        """Return the repo slug for the current store."""
        return Path(self._db_path).stem

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema. Idempotent. Writer-mode only."""
        if self._read_only:
            raise RuntimeError("cannot migrate a read-only StateStore")
        self.conn.execute(_SCHEMA_DDL)
        logger.debug("sdd_state schema ensured")

    # -- read / write --------------------------------------------------------

    def read(self, kind: str, session_key: str | None = None) -> str | None:
        """Read the latest value for ``kind``.

        ``session_key=None`` for repo-scoped kinds; a session key for
        session-scoped kinds.
        """
        key_part = session_key or ""
        row = self.conn.execute(
            "SELECT value FROM sdd_state "
            "WHERE repo=? AND kind=? AND COALESCE(session_key, '')=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (self._repo(), kind, key_part),
        ).fetchone()
        return row[0] if row else None

    def write(
        self,
        kind: str,
        value: str,
        *,
        session_key: str | None = None,
        owner: str | None = None,
    ) -> StateWriteResult:
        """Write a state row.  Returns conflict info if a lease is held by another session."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        repo = self._repo()
        key_part = session_key or ""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        # Check for lease conflict
        existing = self.conn.execute(
            "SELECT owner, lease_expires_at FROM sdd_state "
            "WHERE repo=? AND kind=? AND COALESCE(session_key, '')=?",
            (repo, kind, key_part),
        ).fetchone()

        conflict = None
        if existing and existing[0] and existing[0] != owner:
            lease_expires = existing[1]
            if lease_expires:
                expires_dt = (
                    datetime.fromisoformat(lease_expires)
                    if isinstance(lease_expires, str)
                    else lease_expires
                )
                if expires_dt > now:
                    conflict = LeaseConflict(
                        owner=existing[0],
                        lease_expires_at=expires_dt,
                        message=f"Session {existing[0]} holds the {kind}. Take over?",
                    )

        if existing:
            self.conn.execute(
                "UPDATE sdd_state SET value=?, owner=?, updated_at=?, lease_expires_at=? "
                "WHERE repo=? AND kind=? AND COALESCE(session_key, '')=?",
                (value, owner, ts, None, repo, kind, key_part),
            )
        else:
            lease_expires = ts if owner else None
            self.conn.execute(
                "INSERT INTO sdd_state "
                "(repo, kind, session_key, value, owner, updated_at, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, kind, session_key, value, owner, ts, lease_expires),
            )

        return StateWriteResult(
            success=True,
            kind=kind,
            value=value,
            owner=owner,
            lease_expires_at=None,
            conflict=conflict,
        )

    # -- lease management ----------------------------------------------------

    def acquire_lease(
        self,
        kind: str,
        session_key: str,
        duration: timedelta = timedelta(minutes=5),
    ) -> LeaseResult:
        """Acquire or refresh a lease on a repo-scoped row.

        Returns a :class:`LeaseResult` indicating whether the lease was
        acquired or if there is a conflict with another session.
        """
        if self._read_only:
            raise RuntimeError("cannot acquire lease in read-only mode")
        if kind not in LEASED_KINDS:
            raise ValueError(f"cannot acquire lease on non-leased kind: {kind}")

        repo = self._repo()
        now = datetime.now()
        expires_ts = (now + duration).strftime("%Y-%m-%d %H:%M:%S")
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        row = self.conn.execute(
            "SELECT owner, lease_expires_at FROM sdd_state "
            "WHERE repo=? AND kind=? AND COALESCE(session_key, '')=?",
            (repo, kind, ""),
        ).fetchone()

        if not row:
            self.conn.execute(
                "INSERT INTO sdd_state "
                "(repo, kind, session_key, value, owner, updated_at, lease_expires_at) "
                "VALUES (?, ?, '', '', ?, ?, ?)",
                (repo, kind, session_key, ts, expires_ts),
            )
            return LeaseResult(
                acquired=True,
                owner=session_key,
                lease_expires_at=datetime.fromisoformat(expires_ts),
                conflict=None,
            )

        owner, lease_expires = row
        expires_dt = (
            datetime.fromisoformat(lease_expires)
            if isinstance(lease_expires, str)
            else lease_expires
        )

        if owner and owner != session_key and expires_dt > now:
            return LeaseResult(
                acquired=False,
                owner=owner,
                lease_expires_at=expires_dt,
                conflict=LeaseConflict(
                    owner=owner,
                    lease_expires_at=expires_dt,
                    message=f"Session {owner} holds the {kind}. Take over?",
                ),
            )

        # Acquire or refresh lease
        self.conn.execute(
            "UPDATE sdd_state SET owner=?, lease_expires_at=?, updated_at=? "
            "WHERE repo=? AND kind=? AND COALESCE(session_key, '')=?",
            (session_key, expires_ts, ts, repo, kind, ""),
        )
        if expires_dt is not None and expires_dt > now:
            new_expires = expires_dt
        else:
            new_expires = datetime.fromisoformat(expires_ts)
        return LeaseResult(
            acquired=True,
            owner=session_key,
            lease_expires_at=new_expires,
            conflict=None,
        )

    def release_lease(self, kind: str, session_key: str) -> None:
        """Release a lease on a repo-scoped row."""
        if self._read_only:
            raise RuntimeError("cannot release lease in read-only mode")
        self.conn.execute(
            "UPDATE sdd_state SET owner=NULL, lease_expires_at=NULL "
            "WHERE repo=? AND kind=? AND COALESCE(session_key, '')='' AND owner=?",
            (self._repo(), kind, session_key),
        )

    # -- file mirror ---------------------------------------------------------

    def import_from_files(self, agentalloy_dir: Path) -> dict[str, str]:
        """Import state from file mirror.  Returns dict of kind -> value imported.

        Idempotent: only imports kinds that have no row in the store yet.
        """
        imported: dict[str, str] = {}
        all_kinds = REPO_SCOPED_KINDS | SESSION_SCOPED_KINDS

        for kind in all_kinds:
            if self.read(kind) is not None:
                continue  # already in store

            filepath = agentalloy_dir / kind
            if not filepath.exists():
                continue

            value = filepath.read_text(encoding="utf-8").strip()
            if value:
                self.write(kind, value)
                imported[kind] = value

        return imported

    def mirror_to_files(self, kind: str, value: str, agentalloy_dir: Path) -> bool:
        """Write a value to the file mirror.  Returns False if the write fails."""
        try:
            agentalloy_dir.mkdir(parents=True, exist_ok=True)

            if kind == "approved":
                # approved/<phase> — value is the phase name
                approved_dir = agentalloy_dir / "approved"
                approved_dir.mkdir(parents=True, exist_ok=True)
                approved_file = approved_dir / value
                approved_file.write_text("", encoding="utf-8")
            else:
                filepath = agentalloy_dir / kind
                fd, tmp_path = tempfile.mkstemp(dir=str(agentalloy_dir), prefix=f".{kind}.")
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(value)
                    os.replace(tmp_path, str(filepath))
                except Exception:
                    with suppress(OSError):
                        os.unlink(tmp_path)
                    raise
            return True
        except OSError:
            logger.warning("mirror_to_files failed for kind=%s", kind, exc_info=True)
            return False


def open_state_store(db_path: str | Path, *, read_only: bool = False) -> DuckDBStateStore:
    """Open (and, in writer mode, migrate) the state store at ``db_path``."""
    store = DuckDBStateStore(db_path, read_only=read_only).open()
    if not read_only:
        store.migrate()
    return store
