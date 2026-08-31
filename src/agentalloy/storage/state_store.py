"""DuckDB-backed SDD state store — replaces per-repo files with a session-aware store.

Holds the SDD lifecycle's runtime state (phase, cursor, announced, composed,
approved, banner-turns, pause-reminded) as rows in a single ``sdd_state`` table
keyed by ``(repo, kind, session_key)``.  Session-scoped kinds (announced,
composed, banner-turns, pause-reminded) carry a non-null ``session_key``;
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

import copy
import fnmatch
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import duckdb

from agentalloy.storage.artifact_naming import (
    ARTIFACT_EXT,
    LEGACY_ARTIFACT_EXT,
    STORE_BACKED_PHASES,
)

logger = logging.getLogger(__name__)

# The bucket every row landed in before task 11.  ``_repo()`` used to return
# ``Path(db_path).stem`` and the service opens exactly one ``state.duck``, so
# every repo on the machine shared the literal key ``"state"``.  Kept as the
# unscoped default — deliberately *not* the filename, so a caller that forgets
# to scope lands in a bucket named for what it is instead of one that merely
# looks repo-shaped.  ``rekey_legacy_rows`` drains it.
LEGACY_REPO_KEY = "state"


class _TxnFlag:
    """Connection-wide BEGIN/COMMIT state, shared across scoped views.

    ``lock`` serialises transactions across threads. The service runs request
    handlers in a threadpool over a single connection, so two threads could
    otherwise both pass the ``active`` check and issue ``BEGIN`` on the same
    connection — DuckDB then fails mid-statement with an opaque
    ``IndexError``, and one of the two transitions is lost.

    It is an ``RLock`` deliberately: a *same-thread* re-entrant call must still
    hit the explicit "nested transaction" ``RuntimeError`` rather than
    deadlocking on itself.
    """

    __slots__ = ("active", "lock")

    def __init__(self) -> None:
        self.active = False
        self.lock = threading.RLock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SDD_CONTRACT_COLUMNS = (
    "repo, stream_id, contract_id, phase, slug, work_item, route, "
    "domain_tags, scope_touches, scope_avoids, success_criteria, "
    "status, supersedes, created_at, updated_at, body"
)

_SDD_CONTRACT_COLUMNS_DDL = """\
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    contract_id        TEXT NOT NULL,
    phase              TEXT NOT NULL,
    slug               TEXT NOT NULL,
    work_item          TEXT,
    route              TEXT,
    domain_tags        TEXT,
    scope_touches      TEXT,
    scope_avoids       TEXT,
    success_criteria   TEXT,
    status             TEXT NOT NULL DEFAULT 'active',
    supersedes         TEXT,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    body               TEXT,
    PRIMARY KEY (repo, stream_id, contract_id)
"""

# Used verbatim by migrate() to rebuild sdd_contract when its PRIMARY KEY is
# stale (DuckDB has no ALTER TABLE for constraints, only a full rebuild).
_SDD_CONTRACT_DDL = f"CREATE TABLE sdd_contract (\n{_SDD_CONTRACT_COLUMNS_DDL});"

_SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS sdd_state (
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL,
    session_key        TEXT,
    value              TEXT NOT NULL,
    owner              TEXT,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_expires_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sdd_contract (
{_SDD_CONTRACT_COLUMNS_DDL});

CREATE INDEX IF NOT EXISTS idx_sdd_contract_phase
    ON sdd_contract (phase);

CREATE INDEX IF NOT EXISTS idx_sdd_contract_slug
    ON sdd_contract (slug);

CREATE INDEX IF NOT EXISTS idx_sdd_contract_status
    ON sdd_contract (status);

CREATE TABLE IF NOT EXISTS sdd_artifact (
    repo               TEXT NOT NULL,
    phase              TEXT NOT NULL,
    slug               TEXT NOT NULL,
    name               TEXT NOT NULL,
    content            TEXT,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, phase, slug, name)
);

CREATE INDEX IF NOT EXISTS idx_sdd_artifact_phase
    ON sdd_artifact (repo, phase);

CREATE TABLE IF NOT EXISTS pr_narrative (
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    task_slug          TEXT NOT NULL,
    pr_url             TEXT,
    body               TEXT NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, stream_id, task_slug)
);

CREATE INDEX IF NOT EXISTS idx_pr_narrative_task
    ON pr_narrative (repo, stream_id, task_slug);

CREATE TABLE IF NOT EXISTS ci_telemetry (
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    pr_url             TEXT NOT NULL,
    check_name         TEXT NOT NULL,
    check_status       TEXT NOT NULL,
    check_conclusion   TEXT,
    started_at         TEXT,
    completed_at       TEXT,
    url                TEXT,
    captured_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ci_telemetry_pr
    ON ci_telemetry (repo, stream_id, pr_url);

CREATE TABLE IF NOT EXISTS pr_lifecycle (
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    task_slug          TEXT NOT NULL,
    pr_url             TEXT,
    auto_merge         INTEGER DEFAULT 0,
    merged_at          TEXT,
    watcher_started    TEXT,
    watcher_stopped    TEXT,
    ci_failures        INTEGER DEFAULT 0,
    paused             INTEGER DEFAULT 0,
    paused_reason      TEXT,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, stream_id, task_slug)
);

CREATE INDEX IF NOT EXISTS idx_pr_lifecycle_task
    ON pr_lifecycle (repo, stream_id, task_slug);

CREATE TABLE IF NOT EXISTS sdd_session (
    repo               TEXT NOT NULL,
    stream_id          TEXT NOT NULL DEFAULT '',
    session_key        TEXT NOT NULL,
    task_slug          TEXT,
    phase              TEXT,
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, stream_id, session_key)
);

CREATE INDEX IF NOT EXISTS idx_sdd_session_status
    ON sdd_session (repo, stream_id, status);
"""

# State kinds and their properties
REPO_SCOPED_KINDS: frozenset[str] = frozenset({"phase", "cursor", "approved"})
SESSION_SCOPED_KINDS: frozenset[str] = frozenset(
    {"announced", "composed", "banner-turns", "pause-reminded"},
)
LEASED_KINDS: frozenset[str] = frozenset({"phase", "approved"})


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseConflict:
    owner: str | None
    lease_expires_at: datetime | None
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
class PhaseState:
    """The ``phase`` row, decoded. The blob shape lives in this module alone.

    ``workflow`` is *derived* (``sdd-<phase>``), never stored authoritatively —
    see :meth:`DuckDBStateStore.write_phase`.
    """

    phase: str
    mode: str | None = None
    paused_since: str | None = None
    transitioned_by: str | None = None
    started_at: str | None = None
    phase_start_ref: str | None = None
    last_updated: str | None = None
    workflow: str = ""


def _parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse the flat ``key: value`` phase file without pulling in a YAML dep.

    The file was only ever written by ``_write_phase_atomic`` and the ``phase``
    CLI, both of which emit one unnested ``key: value`` per line with optional
    quoting on ISO timestamps. Splitting on the *first* colon matters: an
    unquoted timestamp value contains colons of its own.

    Unknown keys are kept — callers pick the fields they know and ignore the
    rest, so a file written by a newer version does not fail to import.
    """
    data: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _opt_str(value: Any) -> str | None:
    """Coerce a blob field to ``str``, mapping empty/absent to ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


class _Result:
    """Already-fetched rows, shaped like a DuckDB cursor.

    A DuckDB connection holds *one* pending result.  Handing the live cursor
    back to the caller means the rows are fetched after the lock is released,
    so a second thread's ``execute`` can replace the result in between — which
    surfaces as garbage rather than an error (``IndexError: tuple index out of
    range``, or a JSON blob column read back as an ``int``).  Fetching eagerly
    under the lock is what makes the shared connection safe.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _LockedConn:
    """Serialising façade over the raw DuckDB connection.

    The service serves every repo from one handle, across a request threadpool.
    Every statement therefore runs under the connection-wide lock, and results
    are materialised before it is dropped.  The lock is the same ``RLock`` that
    ``transaction()`` holds, so statements issued inside a transaction re-enter
    it on the owning thread instead of deadlocking.
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: duckdb.DuckDBPyConnection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, params: Any = None) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)
            try:
                return _Result(cur.fetchall())
            except Exception:
                # DDL and most DML produce no result set.
                return _Result([])


class DuckDBStateStore:
    """Thin wrapper over a DuckDB connection to the SDD state store.

    Single-process writer; multiple read-only processes are allowed only when
    no writer is open (DuckDB cross-process locking).  Use as a context
    manager to guarantee the handle is released.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        repo: str | None = None,
        stream_id: str = "",
    ) -> None:
        self._db_path = str(db_path)
        self._read_only = read_only
        self._repo_key = repo or LEGACY_REPO_KEY
        self._stream_id = stream_id
        self._conn: duckdb.DuckDBPyConnection | None = None
        # Built once at open() so scoped views share one façade object: views
        # are shallow copies, and `view.conn is store.conn` is the check that
        # proves they really are one handle rather than two.
        self._locked: _LockedConn | None = None
        # Shared across scoped views: two views over one connection must not
        # both issue BEGIN.  A plain bool would be copied per view and the
        # re-entrancy guard would stop guarding.
        self._txn = _TxnFlag()
        # Post-commit callback registry: kind -> list of callables.
        # Fired after every write to the store (outside the lease).
        # Harness-agnostic — knows only kinds and callables.
        self._on_write_callbacks: dict[str, list[Callable[[str, Any, str, str], None]]] = {}
        # Post-commit hooks fired only on a REAL phase transition (an
        # idempotent same-phase rewrite does not fire them).  A plain list
        # object, shared by reference across for_repo views like
        # _on_write_callbacks.
        self._on_phase_transition: list[Callable[[str | None, str, str | None, str], None]] = []

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> DuckDBStateStore:
        if not self._read_only:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = duckdb.connect(self._db_path, read_only=self._read_only)
        except duckdb.Error as exc:
            raise StateStoreError(str(exc)) from exc
        self._locked = _LockedConn(self._conn, self._txn.lock)
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.debug("failed to close DuckDB state connection", exc_info=True)
            self._conn = None
            self._locked = None

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
    def conn(self) -> _LockedConn:
        """The connection, wrapped so every statement is serialised.

        Deliberately not the raw handle: callers reach for ``store.conn`` as if
        it were private-to-them, and on the service it never is.
        """
        if self._locked is None:
            raise RuntimeError("StateStore is not open")
        return self._locked

    # -- query workhorse -----------------------------------------------------

    def execute(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Any = None) -> Any:
        rows = self.execute(sql, params)
        if not rows:
            return None
        return rows[0][0]

    def iter_rows(self, sql: str, params: Any = None) -> Any:
        yield from self.execute(sql, params)

    # -- repo scoping --------------------------------------------------------

    @property
    def repo(self) -> str:
        """The repo key every row read or written through this handle is under."""
        return self._repo_key

    def for_repo(
        self,
        repo: str,
        *,
        stream_id: str = "",
    ) -> DuckDBStateStore:
        """Return a view of this store scoped to ``(repo, stream_id)``.

        The service opens exactly one ``state.duck`` and serves every repo from
        it, so the repo key cannot be a property of the handle's lifetime — it
        has to be per-request.  A view shares the connection and the transaction
        flag; only the key differs.  Views must not be closed: closing one would
        close the connection out from under the store that produced it.

        ``stream_id`` isolates workflow state between concurrent worktrees of
        the same repo.  When empty (the default) the view uses the store's
        stored ``_stream_id`` (set on construction or via ``bind_stream``).
        """
        view = copy.copy(self)
        view._repo_key = repo
        if stream_id:
            view._stream_id = stream_id
        return view

    def bind_stream(self, stream_id: str) -> DuckDBStateStore:
        """Return a view that also carries the given ``stream_id``."""
        view = copy.copy(self)
        view._stream_id = stream_id
        return view

    def _repo(self) -> str:
        """Return the repo key for the current handle."""
        return self._repo_key

    def _sid(self) -> str:
        """Return the stream id for the current handle."""
        return getattr(self, "_stream_id", "")

    @property
    def _in_transaction(self) -> bool:
        return self._txn.active

    @_in_transaction.setter
    def _in_transaction(self, value: bool) -> None:
        self._txn.active = value

    # -- schema --------------------------------------------------------------

    def migrate(self) -> None:
        """Create the schema. Idempotent. Writer-mode only."""
        if self._read_only:
            raise RuntimeError("cannot migrate a read-only StateStore")
        self.conn.execute(_SCHEMA_DDL)
        logger.debug("sdd_state and sdd_contract schema ensured")

        # stream_id column on sdd_state / sdd_contract — added after DDL so it
        # backfills any table created before per-worktree stream isolation
        # existed. ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table
        # that already exists, so without this an upgrade-in-place keeps the
        # old columns and every stream_id-qualified query fails to bind.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'sdd_state' AND column_name = 'stream_id'",
        ).fetchone()
        if (row[0] if row else 0) == 0:
            self.conn.execute("ALTER TABLE sdd_state ADD COLUMN stream_id TEXT DEFAULT ''")
            self.conn.execute("UPDATE sdd_state SET stream_id = '' WHERE stream_id IS NULL")

        # sdd_contract's PRIMARY KEY changed from (repo, contract_id) to
        # (repo, stream_id, contract_id). DuckDB cannot ALTER a table's
        # constraints in place, so a plain ADD COLUMN would leave the old
        # 2-column PK on upgraded databases — the very first cross-stream
        # contract_id collision would then raise ConstraintException. Rebuild
        # the table instead: rename aside, recreate with the correct PK,
        # copy the data back in, drop the old one.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'sdd_contract' AND column_name = 'stream_id'",
        ).fetchone()
        if (row[0] if row else 0) == 0:
            # Indexes depend on the table and block ALTER TABLE ... RENAME;
            # drop them here, _SCHEMA_DDL recreates them on the new table.
            for idx in (
                "idx_sdd_contract_phase",
                "idx_sdd_contract_slug",
                "idx_sdd_contract_status",
            ):
                self.conn.execute(f"DROP INDEX IF EXISTS {idx}")
            self.conn.execute("ALTER TABLE sdd_contract RENAME TO sdd_contract_pre_stream_id")
            self.conn.execute(_SDD_CONTRACT_DDL)
            self.conn.execute(
                f"INSERT INTO sdd_contract ({_SDD_CONTRACT_COLUMNS}) "
                f"SELECT repo, '', contract_id, phase, slug, work_item, route, "
                f"domain_tags, scope_touches, scope_avoids, success_criteria, "
                f"status, supersedes, created_at, updated_at, body "
                f"FROM sdd_contract_pre_stream_id",
            )
            self.conn.execute("DROP TABLE sdd_contract_pre_stream_id")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sdd_contract_phase ON sdd_contract (phase)",
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sdd_contract_slug ON sdd_contract (slug)",
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sdd_contract_status ON sdd_contract (status)",
            )

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sdd_state_repo_stream_kind_session "
            "ON sdd_state (repo, stream_id, kind, COALESCE(session_key, ''))",
        )
        # Name unchanged from the pre-stream_id definition but the column
        # list changed — CREATE INDEX IF NOT EXISTS matches by name, not
        # definition, so without the DROP an upgraded database silently
        # keeps the stale 3-column index forever. Only drop/recreate when the
        # stored definition is actually stale: migrate() runs on every boot,
        # and rebuilding this index unconditionally would mean paying a full
        # sdd_state index rebuild on every startup once the table has data.
        idx_row = self.conn.execute(
            "SELECT sql FROM duckdb_indexes() WHERE index_name = 'idx_sdd_state_kind_owner'",
        ).fetchone()
        if idx_row is None or "stream_id" not in idx_row[0]:
            self.conn.execute("DROP INDEX IF EXISTS idx_sdd_state_kind_owner")
            self.conn.execute(
                "CREATE INDEX idx_sdd_state_kind_owner "
                "ON sdd_state (repo, stream_id, kind, COALESCE(owner, ''))",
            )
        logger.debug("sdd_state/sdd_contract stream_id column ensured")

        # Lifecycle column on sdd_artifact — added after DDL so it runs on
        # first boot after this change, and is idempotent for subsequent boots.
        # DuckDB does not support ADD COLUMN ... NOT NULL DEFAULT in one
        # statement, so we do it in three steps.
        # Check first to avoid a cascade of errors.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = 'sdd_artifact' AND column_name = 'status'",
        ).fetchone()
        col_exists = row[0] if row else 0
        if col_exists == 0:
            self.conn.execute("ALTER TABLE sdd_artifact ADD COLUMN status TEXT")
            self.conn.execute("UPDATE sdd_artifact SET status = 'active' WHERE status IS NULL")
            # NOT NULL enforcement is done at the code layer — set_artifact()
            # always writes 'active', so NULL is impossible in practice.
        with suppress(duckdb.Error):
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sdd_artifact_repo_phase_status "
                "ON sdd_artifact (repo, phase, status)",
            )
        logger.debug("sdd_artifact lifecycle column and index ensured")

        # Contract status consolidation (four-state model): the retired
        # ``superseded`` status folds into ``cancelled`` — a forked revision
        # never reached product, and the lineage lives in the new revision's
        # ``supersedes`` pointer.  Idempotent once no superseded rows remain.
        with suppress(duckdb.Error):
            self.conn.execute(
                "UPDATE sdd_contract SET status='cancelled' WHERE status='superseded'",
            )

        # Store-only artifact naming migration (#552 06a): lifecycle artifact
        # names in store-backed phases must end with ARTIFACT_EXT, not the
        # legacy ".md". Rename legacy rows in place (strip ".md", append
        # ".artifact"). Idempotent once no ".md"-suffixed rows remain.
        from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
            ARTIFACT_EXT,
            LEGACY_ARTIFACT_EXT,
        )

        phases_sql = ",".join(f"'{p}'" for p in sorted(STORE_BACKED_PHASES))
        # `name` is TEXT so REPLACE works portably in DuckDB: strip the legacy
        # ".md" suffix (if any) then append the canonical ".artifact".
        self.conn.execute(
            f"UPDATE sdd_artifact "
            f"SET name = REPLACE(name, '{LEGACY_ARTIFACT_EXT}', '') || '{ARTIFACT_EXT}' "
            f"WHERE name LIKE '%{LEGACY_ARTIFACT_EXT}' "
            f"AND phase IN ({phases_sql})",
        )
        # Approval digests (_artifact_digest) hash (name, content). The stored
        # approval was computed over ".md" names, so after this rename the
        # recomputed digest no longer matches → the phase re-evaluates as stale,
        # by design (the artifact changed names even if content didn't). No
        # approval rows are rewritten; re-approval is 06a's intended single
        # observable effect for any repo mid-flight.
        logger.debug("sdd_artifact legacy .md names migrated to .artifact")

        # Ship-phase tables: pr_narrative, ci_telemetry, pr_lifecycle
        # The DDL at the top of migrate() creates them via CREATE TABLE IF NOT EXISTS.
        # On first boot the tables are created with full schema + indexes.
        # On subsequent boots CREATE TABLE IF NOT EXISTS is a no-op.
        logger.debug("pr_narrative / ci_telemetry / pr_lifecycle schema ensured via DDL")

    # -- read / write --------------------------------------------------------

    def read(self, kind: str, session_key: str | None = None) -> str | None:
        """Read the latest value for ``kind``.

        ``session_key=None`` for repo-scoped kinds; a session key for
        session-scoped kinds.
        """
        repo = self._repo()
        sid = self._sid()
        key_part = session_key or ""
        row = self.conn.execute(
            "SELECT value FROM sdd_state "
            "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (repo, sid, kind, key_part),
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
        sid = self._sid()
        key_part = session_key or ""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        # Hold the store lock across probe + write so two concurrent writers
        # on the same key cannot both see "no row" and both INSERT.
        with nullcontext() if self._in_transaction else self.transaction():
            # Check for lease conflict
            existing = self.conn.execute(
                "SELECT owner, lease_expires_at FROM sdd_state "
                "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=?",
                (repo, sid, kind, key_part),
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
                    "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=?",
                    (value, owner, ts, None, repo, sid, kind, key_part),
                )
            else:
                lease_expires = ts if owner else None
                self.conn.execute(
                    "INSERT INTO sdd_state "
                    "(repo, stream_id, kind, session_key, value, owner, updated_at, lease_expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, sid, kind, session_key, value, owner, ts, lease_expires),
                )

        return StateWriteResult(
            success=True,
            kind=kind,
            value=value,
            owner=owner,
            lease_expires_at=None,
            conflict=conflict,
        )

    def clear(
        self,
        kind: str,
        session_key: str | None = None,
        *,
        all_sessions: bool = False,
    ) -> int:
        """Delete state rows for ``kind``. Returns how many rows were removed.

        The store equivalent of ``unlink()``ing a file-mirror kind: the reset
        paths (``wire``, ``add``) and ``run_phase_clear`` need a way to make a
        kind *absent*, not merely empty-valued — a row holding ``""`` still reads
        as present to every consumer that checks for ``None``.

        Scoping matches :meth:`read`/:meth:`write`: ``session_key=None`` targets
        the repo-scoped row. ``all_sessions=True`` sweeps every session's row for
        that kind, which is what a reset wants for session-scoped kinds
        (``announced``, ``composed``, …) where no single key identifies "all of
        them"; it is mutually exclusive with an explicit ``session_key``.

        Deleting the row also **releases any lease** on it — ``acquire_lease``
        requires an existing row, so a cleared kind cannot leave a session
        holding a lease on something that no longer exists.

        Clearing an absent kind returns ``0``; it is not an error, so reset paths
        stay idempotent.
        """
        if self._read_only:
            raise RuntimeError("cannot clear in read-only mode")
        if all_sessions and session_key is not None:
            raise ValueError("clear(all_sessions=True) cannot take a session_key")

        repo = self._repo()
        sid = self._sid()
        if all_sessions:
            sql = "DELETE FROM sdd_state WHERE repo=? AND stream_id=? AND kind=?"
            params: tuple[Any, ...] = (repo, sid, kind)
        else:
            sql = "DELETE FROM sdd_state WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=?"
            params = (repo, sid, kind, session_key or "")

        before = self.conn.execute(
            sql.replace("DELETE FROM", "SELECT COUNT(*) FROM"),
            params,
        ).fetchone()
        self.conn.execute(sql, params)
        return int(before[0]) if before else 0

    # -- lease management ----------------------------------------------------

    def acquire_lease(
        self,
        kind: str,
        session_key: str,
        duration: timedelta = timedelta(minutes=5),
    ) -> LeaseResult:
        """Acquire or refresh a lease on a stream-scoped row.

        Returns a :class:`LeaseResult` indicating whether the lease was
        acquired or if there is a conflict with another session.
        """
        if self._read_only:
            raise RuntimeError("cannot acquire lease in read-only mode")
        if kind not in LEASED_KINDS:
            raise ValueError(f"cannot acquire lease on non-leased kind: {kind}")

        repo = self._repo()
        sid = self._sid()
        now = datetime.now()
        expires_ts = (now + duration).strftime("%Y-%m-%d %H:%M:%S")
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        row = self.conn.execute(
            "SELECT owner, lease_expires_at FROM sdd_state "
            "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=?",
            (repo, sid, kind, ""),
        ).fetchone()

        if not row:
            # Lease semantics: the row must already exist (created by write()).
            # We claim ownership; we do not create rows.  Returning a conflict
            # signals "write the row first" rather than silently creating a
            # ghost row with value=''.
            return LeaseResult(
                acquired=False,
                owner=None,
                lease_expires_at=None,
                conflict=LeaseConflict(
                    owner=None,
                    lease_expires_at=None,
                    message=f"No row for {kind!r} — write it before leasing",
                ),
            )

        owner, lease_expires = row
        expires_dt = (
            datetime.fromisoformat(lease_expires)
            if isinstance(lease_expires, str)
            else lease_expires
        )

        # A NULL lease_expires_at means no active lease (e.g. the row was
        # written with an owner but the lease lapsed or was never set) —
        # symmetric with write()'s `if lease_expires:` guard.
        if owner and owner != session_key and expires_dt is not None and expires_dt > now:
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
            "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')=?",
            (session_key, expires_ts, ts, repo, sid, kind, ""),
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
        """Release a lease on a stream-scoped row."""
        if self._read_only:
            raise RuntimeError("cannot release lease in read-only mode")
        self.conn.execute(
            "UPDATE sdd_state SET owner=NULL, lease_expires_at=NULL "
            "WHERE repo=? AND stream_id=? AND kind=? AND COALESCE(session_key, '')='' AND owner=?",
            (self._repo(), self._sid(), kind, session_key),
        )

    # -- post-commit callbacks -----------------------------------------------

    def on_write(self, kind: str, fn: Callable[[str, Any, str, str], None]) -> None:
        """Register *fn* to be called after every write to *kind*, post-commit.

        The callback receives ``(kind, value, repo, stream)`` where *value* is
        the stored string (JSON for the phase blob, raw text for other kinds),
        *repo* is the key of the handle that performed the write, and *stream*
        is that handle's stream id.  One store serves every repo — and every
        worktree of a repo, via a shared ``repo`` key (see ``_repo_key_for``) —
        on the machine via :meth:`for_repo` views, so without *repo* a callback
        cannot tell whose row changed, and without *stream* it cannot tell
        which worktree of that repo changed.  Missing either would regenerate
        the wrong repo's, or the wrong worktree's, rules file.  Callbacks
        fire **outside** the lease — the write is already durable.  A callback
        that raises does **not** roll back the write or kill the writer; errors
        are logged and the next callback in the list still runs.

        Harness-agnostic: the registry knows only kinds and callables.  It does
        not reference Claude Code, Codex, or any single harness.  Per-harness
        output from ``wire_harness`` is unchanged and stays.
        """
        self._on_write_callbacks.setdefault(kind, []).append(fn)

    def off_write(self, kind: str, fn: Callable[[str, Any, str, str], None]) -> None:
        """Unregister a previously registered callback."""
        if kind in self._on_write_callbacks:
            with suppress(ValueError):
                self._on_write_callbacks[kind].remove(fn)

    def _fire_callbacks(self, kind: str, value: str) -> None:
        """Invoke all registered callbacks for *kind*, logging any errors.

        ``self`` may be a :meth:`for_repo` view, so ``_repo()``/``_sid()`` are
        the repo and stream that actually changed rather than the handle the
        callback was registered on.
        """
        for fn in list(self._on_write_callbacks.get(kind, [])):
            try:
                fn(kind, value, self._repo(), self._sid())
            except Exception:
                logger.exception("on_write callback for %r raised", kind)

    def on_phase_transition(self, fn: Callable[[str | None, str, str | None, str], None]) -> None:
        """Register *fn* to be called after a REAL phase transition commits.

        The callback receives ``(prev_phase, new_phase, actor, repo)`` —
        *prev_phase* is ``None`` on the first write, *actor* is the caller the
        row records, and *repo* is the key of the handle that wrote (``self``
        may be a :meth:`for_repo` view).  The firing predicate is the same one
        :meth:`write_phase` uses — ``prev is None or prev.phase != phase`` —
        so an idempotent same-phase rewrite never fires the hook.  Like
        :meth:`on_write`, hooks fire **outside** the lease (the write is
        already durable), and a hook that raises is logged, not propagated.
        """
        self._on_phase_transition.append(fn)

    def off_phase_transition(self, fn: Callable[[str | None, str, str | None, str], None]) -> None:
        """Unregister a previously registered transition hook."""
        with suppress(ValueError):
            self._on_phase_transition.remove(fn)

    def _fire_phase_transition_hooks(
        self, prev_phase: str | None, phase: str, actor: str | None
    ) -> None:
        for fn in list(self._on_phase_transition):
            try:
                fn(prev_phase, phase, actor, self._repo())
            except Exception:
                logger.exception("phase transition hook raised")

    # -- transaction ---------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[DuckDBStateStore]:
        """Context manager that issues BEGIN/COMMIT (or ROLLBACK on exception).

        Existing methods (``write``, ``execute``, ``acquire_lease``,
        ``release_lease``) are reused unchanged inside the block.  DuckDB is
        single-writer here so there is no isolation-level choice.

        Re-entrant calls raise :class:`RuntimeError` — silent nesting is
        explicitly rejected.

        Concurrent callers on *other* threads block until the transaction ends:
        one connection cannot carry two transactions, and the service serves
        every repo from a single handle.
        """
        if self._in_transaction:
            raise RuntimeError("nested transaction() calls are not supported")
        if self._read_only:
            raise RuntimeError("cannot begin transaction in read-only mode")

        with self._txn.lock:
            # Re-check under the lock: another thread may have been mid-BEGIN
            # when the fast-path check above ran.
            if self._in_transaction:
                raise RuntimeError("nested transaction() calls are not supported")
            self._in_transaction = True
            self.conn.execute("BEGIN")
            try:
                yield self
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._in_transaction = False

    # -- phase blob ----------------------------------------------------------

    def read_phase(self) -> PhaseState | None:
        """Read the ``phase`` row as a typed value, or ``None`` when unset.

        Tolerates a bare-string row (``"design"``) left by the pre-blob store or
        by ``import_from_files``: it reads as a :class:`PhaseState` carrying just
        the phase, with the derived ``workflow`` filled in. Nothing outside this
        module parses the stored blob.
        """
        raw = self.read("phase")
        if raw is None:
            return None
        data = self._from_json(raw)
        if isinstance(data, str):
            # Bare phase string (pre-blob row). Not an error — normalize it.
            data = {"phase": data}
        if not isinstance(data, dict):
            return None
        blob = cast("dict[str, Any]", data)
        if not blob.get("phase"):
            return None
        phase = str(blob["phase"])
        # Legacy: ``free_since`` was the old key name (renamed to ``paused_since``);
        # read it for backwards compatibility with existing rows.
        paused = _opt_str(blob.get("paused_since")) or _opt_str(blob.get("free_since"))
        return PhaseState(
            phase=phase,
            mode=_opt_str(blob.get("mode")),
            paused_since=paused,
            transitioned_by=_opt_str(blob.get("transitioned_by")),
            started_at=_opt_str(blob.get("started_at")),
            phase_start_ref=_opt_str(blob.get("phase_start_ref")),
            last_updated=_opt_str(blob.get("last_updated")),
            workflow=f"sdd-{phase}",
        )

    def set_phase_start_ref(self, ref: str) -> None:
        """Stamp *ref* (a git SHA) as the phase-entry marker on the phase row.

        Used by the build→qa ``scope_touched_in_diff`` gate to diff what changed
        *during* the phase. A targeted in-place update of the blob's
        ``phase_start_ref`` key — preserves every other field (``mode``,
        ``started_at``, ``transitioned_by``, …) untouched, mirroring ``write_phase``
        semantics without re-deriving them. Fail-soft is the caller's job; this
        always writes when the row exists.
        """
        with nullcontext() if self._in_transaction else self.transaction():
            raw = self.read("phase")
            if raw is None:
                return
            data = self._from_json(raw)
            if isinstance(data, str):
                data = {"phase": data}
            if not isinstance(data, dict):
                return
            blob: dict[str, Any] = cast("dict[str, Any]", data)
            if not blob.get("phase"):
                return
            blob["phase_start_ref"] = ref
            self.write("phase", json.dumps({k: v for k, v in blob.items() if v is not None}))

    def write_phase(
        self,
        phase: str,
        *,
        actor: str | None = None,
        mode: str | None = None,
        paused_since: str | None = None,
        owner: str | None = None,
        phase_start_ref: str | None = None,
    ) -> StateWriteResult:
        """Write the ``phase`` row as a blob, preserving what the caller didn't set.

        Read-modify-write inside a transaction so a concurrent writer can never
        observe a half-updated blob. This is the store-side replacement for
        ``signals.skill_loader._write_phase_atomic``'s file semantics:

        * ``mode`` / ``paused_since`` are **carried forward** when not passed. An
          auto-transition must never silently drop a repo out of (or into)
          pause — only ``agentalloy workflow pause/resume`` sets them.
        * ``transitioned_by`` is set to ``actor`` only on a *real* transition
          (``prev != phase``). An idempotent same-phase write preserves the prior
          actor, so a different session can still tell the phase moved and that
          it wasn't the one that moved it.
        * ``started_at`` is preserved across writes; only the first write sets it.
        * ``phase_start_ref`` is carried forward from ``prev`` on an idempotent
          same-phase write, so the phase-start HEAD stamp set on the real
          transition survives rewrites — only a genuine transition lets a
          caller overwrite it (the proxy uses that to seed it from HEAD).
        * ``workflow`` is **derived** here as ``sdd-<phase>`` and is never taken
          from a caller — a caller cannot poison the row with a bogus workflow.

        Passing ``mode=""`` (or ``paused_since=""``) explicitly clears the field,
        which is how ``workflow resume`` drops pause; ``None`` means "leave it".

        Callers already inside a :meth:`transaction` reuse it rather than
        nesting (which is rejected outright).  ``POST /state/phase`` writes the
        phase and a contract in one BEGIN/COMMIT, and it must get blob
        semantics too — a phase advance that dropped pause mode purely
        because a contract rode along would be a silent, phase-shaped bug.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        outer: AbstractContextManager[object] = (
            nullcontext() if self._in_transaction else self.transaction()
        )
        with outer:
            prev = self.read_phase()
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

            resolved_mode = prev.mode if (mode is None and prev) else mode
            resolved_since = prev.paused_since if (paused_since is None and prev) else paused_since
            is_transition = prev is None or prev.phase != phase
            resolved_actor = actor if is_transition else prev.transitioned_by  # type: ignore[union-attr]
            # ``phase_start_ref`` is carried forward on an idempotent same-phase
            # write; only a real transition accepts a caller-supplied stamp so a
            # stale marker from a prior phase can't be re-armed by a rewrite.
            if phase_start_ref is not None and is_transition:
                resolved_phase_start_ref = phase_start_ref
            elif prev is not None and not is_transition:
                resolved_phase_start_ref = prev.phase_start_ref  # type: ignore[union-attr]
            else:
                resolved_phase_start_ref = phase_start_ref or None

            blob: dict[str, Any] = {
                "phase": phase,
                "mode": resolved_mode or None,
                "paused_since": resolved_since or None,
                "transitioned_by": resolved_actor or None,
                "started_at": (prev.started_at if prev and prev.started_at else now),
                "phase_start_ref": resolved_phase_start_ref,
                "last_updated": now,
                "workflow": f"sdd-{phase}",
            }
            payload = json.dumps({k: v for k, v in blob.items() if v is not None})
            result = self.write("phase", payload, owner=owner)
            # Fire post-commit callbacks outside the lease so the write is
            # already durable.  Callbacks that raise are logged but do not
            # roll back or kill the writer.
            self._fire_callbacks("phase", payload)
            if is_transition:
                self._fire_phase_transition_hooks(
                    prev.phase if prev is not None else None, phase, resolved_actor
                )
            return result

    # -- file mirror ---------------------------------------------------------

    def import_from_files(self, agentalloy_dir: Path) -> dict[str, str]:
        """One-shot migration of the file mirror into the store.

        Returns a dict of kind -> the value now in the store for kinds this call
        imported. Idempotent: a kind that already has a row is skipped, and a
        repo with no files at all is a no-op.

        ``phase`` holds flat YAML across several lines — storing its raw text as
        the value (what this method did until 2026-07-28) yields a row no reader
        can parse. It is parsed into the blob shape via :meth:`write_phase`.

        The rest of the mirror is *not* uniformly a bare token, so it cannot be
        imported by reading text: ``approved`` is a directory of per-phase marker
        files, and the session-scoped kinds hold TSV lines keyed by session id.
        Session-scoped state is skipped outright — it is per-session ephemera
        that regenerates on the next request, and a row imported without its
        session key matches no reader. Repo-scoped kinds are imported only when
        the path is a regular file; a directory-shaped kind is left for the
        contract that gives it a store shape.

        The phase file is deleted once its content has been *carried into* the
        store. A repo that already has a store row is diverged: the store value
        wins, but the file is left in place for task 08 to remove — until the
        readers have moved, that file may be the only record of a phase set
        while the service was down.

        A phase file that cannot be parsed raises :class:`StateStoreError` and is
        **not** deleted, so a bad file can be inspected rather than destroyed.
        """
        imported: dict[str, str] = {}

        for kind in sorted(REPO_SCOPED_KINDS):
            filepath = agentalloy_dir / kind
            if kind == "phase":
                value = self._import_phase_file(filepath)
                if value:
                    imported[kind] = value
                continue

            if self.read(kind) is not None:
                continue  # already in store
            if not filepath.is_file():
                continue  # absent, or a directory-shaped kind
            value = filepath.read_text(encoding="utf-8").strip()
            if value:
                self.write(kind, value)
                imported[kind] = value

        return imported

    def _import_phase_file(self, filepath: Path) -> str | None:
        """Migrate ``.agentalloy/phase`` into the blob row, then remove the file.

        Returns the phase imported, or ``None`` when there was nothing to import
        (no file, or the store already holds a row and the file was merely
        cleaned up).
        """
        if not filepath.exists():
            return None

        raw = filepath.read_text(encoding="utf-8")
        data = _parse_flat_yaml(raw)
        phase = _opt_str(data.get("phase"))
        if phase is None:
            raise StateStoreError(
                f"{filepath} is not a readable phase file (no 'phase' key); "
                "refusing to import or delete it",
            )

        if self.read("phase") is None:
            self.write_phase(
                phase,
                actor=_opt_str(data.get("transitioned_by")),
                mode=_opt_str(data.get("mode")),
                paused_since=_opt_str(data.get("free_since")),
            )
            filepath.unlink()
            return phase

        # Diverged: the store already has a phase and it wins. The file is
        # *kept* rather than deleted for one reason — the readers have not moved
        # yet (task 08), so this file may still be the only record of a phase
        # set while the service was down. Deleting it is task 08's job.
        return None

    # -- contract helpers ----------------------------------------------------

    @staticmethod
    def _to_json(value: Any) -> str | None:
        """Serialize a value to JSON text, or None."""
        if value is None:
            return None
        return json.dumps(value)

    @staticmethod
    def _from_json(value: str | None) -> Any:
        """Deserialize JSON text, or return the value as-is."""
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @staticmethod
    def _row_to_contract(row: tuple[Any, ...]) -> dict[str, Any]:
        """Convert a sdd_contract row tuple to a dict."""
        return {
            "contract_id": row[0],
            "phase": row[1],
            "slug": row[2],
            "work_item": row[3],
            "route": row[4],
            "domain_tags": DuckDBStateStore._from_json(row[5]),
            "scope_touches": DuckDBStateStore._from_json(row[6]),
            "scope_avoids": DuckDBStateStore._from_json(row[7]),
            "success_criteria": DuckDBStateStore._from_json(row[8]),
            "status": row[9],
            "supersedes": row[10],
            "created_at": row[11],
            "updated_at": row[12],
            "body": row[13],
            "stream_id": row[14],
        }

    # -- contract CRUD -------------------------------------------------------
    # Three entry points are intentional (not redundant):
    #   put_contract      — upsert: create or replace a contract by id
    #   update_contract   — partial in-place correction (no revision fork)
    #   supersede_contract — revision fork: new row + retire the old one
    # Merging any two would conflate distinct semantics (replace vs correct
    # vs fork) and force callers to guess the intended operation.
    #
    # Contract lifecycle statuses (four states, one in-flight + three outcomes):
    #   active    — in flight; the only status workflow reads match
    #   stashed   — parked, waiting to resume (session stash)
    #   archived  — reached product (terminal)
    #   cancelled — never reached product: abandoned, or retired by a
    #               revision fork (lineage lives in the new row's supersedes)

    def put_contract(
        self,
        contract_id: str,
        *,
        phase: str,
        slug: str,
        work_item: str | None = None,
        route: str | None = None,
        domain_tags: list[str] | None = None,
        scope_touches: list[str] | None = None,
        scope_avoids: list[str] | None = None,
        success_criteria: list[str | dict[str, Any]] | None = None,
        body: str | None = None,
        status: str = "active",
        supersedes: str | None = None,
    ) -> str:
        """Insert or update a contract row.  Returns the contract_id."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        repo = self._repo()
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        domain_tags_json = self._to_json(domain_tags)
        scope_touches_json = self._to_json(scope_touches)
        scope_avoids_json = self._to_json(scope_avoids)
        success_criteria_json = self._to_json(success_criteria)

        # Check if the contract already exists (for upsert logic)
        existing = self.conn.execute(
            "SELECT created_at, supersedes FROM sdd_contract "
            "WHERE repo=? AND stream_id=? AND contract_id=?",
            (repo, self._stream_id, contract_id),
        ).fetchone()

        if existing:
            self.conn.execute(
                "UPDATE sdd_contract SET "
                "phase=?, slug=?, work_item=?, route=?, "
                "domain_tags=?, scope_touches=?, scope_avoids=?, success_criteria=?, "
                "status=?, body=?, updated_at=? "
                "WHERE repo=? AND stream_id=? AND contract_id=?",
                (
                    phase,
                    slug,
                    work_item,
                    route,
                    domain_tags_json,
                    scope_touches_json,
                    scope_avoids_json,
                    success_criteria_json,
                    status,
                    body,
                    ts,
                    repo,
                    self._stream_id,
                    contract_id,
                ),
            )
        else:
            self.conn.execute(
                "INSERT INTO sdd_contract "
                "(repo, stream_id, contract_id, phase, slug, work_item, route, "
                "domain_tags, scope_touches, scope_avoids, success_criteria, "
                "status, supersedes, created_at, updated_at, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    self._stream_id,
                    contract_id,
                    phase,
                    slug,
                    work_item,
                    route,
                    domain_tags_json,
                    scope_touches_json,
                    scope_avoids_json,
                    success_criteria_json,
                    status,
                    supersedes,
                    ts,
                    ts,
                    body,
                ),
            )

        return contract_id

    def get_contract(self, contract_id: str) -> dict[str, Any] | None:
        """Retrieve a contract by contract_id.  Returns a dict or None."""
        row = self.conn.execute(
            "SELECT contract_id, phase, slug, work_item, route, "
            "domain_tags, scope_touches, scope_avoids, success_criteria, "
            "status, supersedes, created_at, updated_at, body, stream_id "
            "FROM sdd_contract WHERE repo=? AND stream_id=? AND contract_id=?",
            (self._repo(), self._stream_id, contract_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_contract(row)

    def list_contracts(
        self,
        *,
        phase: str | None = None,
        slug: str | None = None,
        work_item: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List contracts with optional filters."""
        conditions: list[str] = ["repo=? AND stream_id=?"]
        params: list[Any] = [self._repo(), self._stream_id]

        if phase is not None:
            conditions.append("phase=?")
            params.append(phase)
        if slug is not None:
            conditions.append("slug=?")
            params.append(slug)
        if work_item is not None:
            conditions.append("work_item=?")
            params.append(work_item)
        if status is not None:
            conditions.append("status=?")
            params.append(status)

        where = "WHERE " + " AND ".join(conditions)

        sql = (
            "SELECT contract_id, phase, slug, work_item, route, "
            "domain_tags, scope_touches, scope_avoids, success_criteria, "
            "status, supersedes, created_at, updated_at, body, stream_id "
            f"FROM sdd_contract {where} "
            "ORDER BY created_at DESC"
        )

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_contract(row) for row in rows]

    def archive_contract(self, contract_id: str) -> bool:
        """Archive a contract by flipping its status to 'archived'.

        Returns True if a row was updated, False if the contract was not found.
        The row remains fetchable by contract_id.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        result = self.conn.execute(
            "UPDATE sdd_contract SET status='archived', updated_at=? "
            "WHERE repo=? AND stream_id=? AND contract_id=? AND status != 'archived'",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self._repo(),
                self._stream_id,
                contract_id,
            ),
        )
        count = result.fetchall()
        return bool(count) and count[0][0] > 0

    def supersede_contract(
        self,
        old_contract_id: str,
        *,
        new_contract_id: str,
        phase: str,
        slug: str,
        work_item: str | None = None,
        route: str | None = None,
        domain_tags: list[str] | None = None,
        scope_touches: list[str] | None = None,
        scope_avoids: list[str] | None = None,
        success_criteria: list[str | dict[str, Any]] | None = None,
        body: str | None = None,
    ) -> str:
        """Supersede a contract: write a new row and retire the prior one.

        The retired revision is flipped to ``cancelled`` — it never reached
        product; the fork lineage survives in the new row's ``supersedes``
        pointer.  Returns the new contract_id.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        repo = self._repo()
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        # Verify the old contract exists and can be retired by a fork.
        # ``cancelled`` is accepted so a revision chain can keep forking
        # (each retired revision is itself cancelled-by-fork).
        old_row = self.conn.execute(
            "SELECT status FROM sdd_contract WHERE repo=? AND stream_id=? AND contract_id=?",
            (repo, self._stream_id, old_contract_id),
        ).fetchone()
        if old_row is None:
            raise StateStoreError(f"Contract {old_contract_id!r} not found")
        if old_row[0] not in ("active", "cancelled"):
            raise StateStoreError(f"Cannot supersede contract with status {old_row[0]!r}")

        # Retire the old revision
        self.conn.execute(
            "UPDATE sdd_contract SET status='cancelled', updated_at=? "
            "WHERE repo=? AND stream_id=? AND contract_id=?",
            (ts, repo, self._stream_id, old_contract_id),
        )

        # Write the new contract with supersedes set
        return self.put_contract(
            new_contract_id,
            phase=phase,
            slug=slug,
            work_item=work_item,
            route=route,
            domain_tags=domain_tags,
            scope_touches=scope_touches,
            scope_avoids=scope_avoids,
            success_criteria=success_criteria,
            body=body,
            status="active",
            supersedes=old_contract_id,
        )

    def update_contract(
        self,
        contract_id: str,
        *,
        body: str | None = None,
        domain_tags: list[str] | None = None,
        scope_touches: list[str] | None = None,
        scope_avoids: list[str] | None = None,
        success_criteria: list[str | dict[str, Any]] | None = None,
    ) -> bool:
        """In-place correction: update specified fields and bump updated_at.

        Returns True if a row was updated, False if the contract was not found.
        Unlike supersede, this does not fork the revision chain.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [datetime.now().strftime("%Y-%m-%d %H:%M:%S")]

        if body is not None:
            sets.append("body=?")
            params.append(body)
        if domain_tags is not None:
            sets.append("domain_tags=?")
            params.append(self._to_json(domain_tags))
        if scope_touches is not None:
            sets.append("scope_touches=?")
            params.append(self._to_json(scope_touches))
        if scope_avoids is not None:
            sets.append("scope_avoids=?")
            params.append(self._to_json(scope_avoids))
        if success_criteria is not None:
            sets.append("success_criteria=?")
            params.append(self._to_json(success_criteria))

        params.extend([self._repo(), self._stream_id, contract_id])

        sql = (
            f"UPDATE sdd_contract SET {', '.join(sets)} "
            "WHERE repo=? AND stream_id=? AND contract_id=?"
        )
        result = self.conn.execute(sql, params)
        count = result.fetchall()
        return bool(count) and count[0][0] > 0

    # -- artifact CRUD ---------------------------------------------------------
    # Deliverable bodies (docs/spec/<slug>.md, docs/design/<slug>/{approach,
    # tasks,test-plan}.md) live here, keyed by (phase, slug, name) rather than
    # folded into sdd_contract: design has three named bodies per slug, spec
    # has one — a single `body` column (as sdd_contract has, for the contract
    # itself) can't hold that shape.

    def set_artifact(
        self,
        phase: str,
        slug: str,
        name: str,
        content: str,
        *,
        status: str = "active",
    ) -> dict[str, Any]:
        """Upsert an artifact body. Returns the stored row as a dict.

        New inserts default to ``status='active'``; updates preserve the
        existing status so an archived artifact stays archived when rewritten.

        For store-backed phases (spec/design/plan/qa/ship/sdd-fast) the
        ``name`` is canonicalized to the ``.artifact`` suffix on write (see
        :func:`artifact_naming.canonicalize_artifact_name`), so a gate's
        ``name: "*.artifact"`` glob always matches the stored row. Legacy
        ``.md`` names from pre-migration callers are repaired here rather than
        rejected; disk-deliverable phases (``src/**``, ``tests/**``) are
        untouched.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
            canonicalize_artifact_name,
        )

        name = canonicalize_artifact_name(phase, name)
        repo = self._repo()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = self.conn.execute(
            "SELECT status FROM sdd_artifact WHERE repo=? AND phase=? AND slug=? AND name=?",
            (repo, phase, slug, name),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE sdd_artifact SET content=?, updated_at=? "
                "WHERE repo=? AND phase=? AND slug=? AND name=?",
                (content, ts, repo, phase, slug, name),
            )
            existing_status = existing[0]
        else:
            self.conn.execute(
                "INSERT INTO sdd_artifact "
                "(repo, phase, slug, name, content, updated_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, phase, slug, name, content, ts, status),
            )
            existing_status = status
        return {
            "phase": phase,
            "slug": slug,
            "name": name,
            "content": content,
            "updated_at": ts,
            "status": existing_status,
        }

    def get_artifact(
        self,
        phase: str,
        slug: str,
        name: str,
        *,
        status: str = "active",
    ) -> dict[str, Any] | None:
        from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
            canonicalize_artifact_name,
        )

        name = canonicalize_artifact_name(phase, name)
        conditions = [
            "repo=?",
            "phase=?",
            "slug=?",
            "name=?",
        ]
        from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
            canonicalize_artifact_name,
        )

        name = canonicalize_artifact_name(phase, name)
        params: list[Any] = [self._repo(), phase, slug, name]
        if status != "all":
            conditions.append("status=?")
            params.append(status)
        row = self.conn.execute(
            "SELECT phase, slug, name, content, updated_at, status FROM sdd_artifact "
            "WHERE " + " AND ".join(conditions),
            params,
        ).fetchone()
        if row is None:
            return None
        return {
            "phase": row[0],
            "slug": row[1],
            "name": row[2],
            "content": row[3],
            "updated_at": row[4],
            "status": row[5],
        }

    def list_artifacts(
        self,
        phase: str,
        *,
        slug: str | None = None,
        name_glob: str | None = None,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """List artifacts for a phase, optionally filtered by slug and a
        ``fnmatch``-style ``name_glob`` (the store-side equivalent of globbing
        ``docs/<phase>/**/<name_glob>`` on disk).

        By default returns only active artifacts; pass ``status='all'`` to
        include archived rows.
        """
        conditions = ["repo=?", "phase=?"]
        params: list[Any] = [self._repo(), phase]
        if slug is not None:
            conditions.append("slug=?")
            params.append(slug)
        if status != "all":
            conditions.append("status=?")
            params.append(status)
        where = " AND ".join(conditions)
        rows = self.conn.execute(
            "SELECT phase, slug, name, content, updated_at, status FROM sdd_artifact "
            f"WHERE {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        results = [
            {
                "phase": r[0],
                "slug": r[1],
                "name": r[2],
                "content": r[3],
                "updated_at": r[4],
                "status": r[5],
            }
            for r in rows
        ]
        if name_glob is not None:
            from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
                is_legacy_artifact_name,
            )

            if is_legacy_artifact_name(name_glob):  # legacy '*.md' glob -> '*.artifact'
                name_glob = name_glob[: -len(LEGACY_ARTIFACT_EXT)] + ARTIFACT_EXT
                globs = [name_glob]
            elif "*" in name_glob or "?" in name_glob or name_glob.endswith(ARTIFACT_EXT):
                globs = [name_glob]
            else:
                # A bare logical name ("delivery", "solution") matches both the
                # canonical row (delivery.artifact) and the bare row the lesson
                # artifact is deliberately stored under (see canonicalize_artifact_name).
                globs = [name_glob, name_glob + ARTIFACT_EXT]
            results = [r for r in results if any(fnmatch.fnmatch(r["name"], g) for g in globs)]
        return results

    def archive_artifact(self, phase: str, slug: str, name: str) -> bool:
        """Archive a single artifact by flipping status to 'archived'.

        Returns True if a row was updated, False if not found or already archived.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        from agentalloy.storage.artifact_naming import (  # noqa: PLC0415
            canonicalize_artifact_name,
        )

        name = canonicalize_artifact_name(phase, name)
        repo = self._repo()
        result = self.conn.execute(
            "UPDATE sdd_artifact SET status='archived', updated_at=? "
            "WHERE repo=? AND phase=? AND slug=? AND name=? AND status != 'archived'",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), repo, phase, slug, name),
        )
        count = result.fetchall()
        return bool(count) and count[0][0] > 0

    def archive_all(self, outcome: str = "archived") -> dict[str, Any]:
        """End the current work cycle: retire all in-flight contracts and artifacts.

        ``outcome`` is the terminal status the in-flight contracts receive —
        ``archived`` when the cycle reached product (reset from ship),
        ``cancelled`` when it was abandoned mid-flight.  ``stashed`` contracts
        are deliberately untouched: parked work waits to resume and is not
        part of the cycle being closed.  Artifacts always go to ``archived``
        regardless — they are historical record, not lifecycle participants.

        Returns ``{"contracts": int, "artifacts": int, "outcome": str}``.
        """
        if outcome not in ("archived", "cancelled"):
            raise StateStoreError(f"Invalid archive_all outcome: {outcome!r}")
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        repo = self._repo_key
        stream_id = self._sid()
        conn = self.conn

        # transaction() (not manual BEGIN/COMMIT) so the store lock is held
        # across both UPDATEs and _txn.active stays consistent.
        with self.transaction():
            contracts_result = conn.execute(
                "UPDATE sdd_contract SET status=?, updated_at=? "
                "WHERE repo=? AND stream_id=? AND status='active'",
                (outcome, ts, repo, stream_id),
            )
            contracts_count = contracts_result.fetchall()
            contracts_count = contracts_count[0][0] if contracts_count else 0

            # Stashed work keeps its artifacts active: the resume path reads
            # them back, and the default artifact reads filter to active.
            artifacts_result = conn.execute(
                "UPDATE sdd_artifact SET status='archived', updated_at=? "
                "WHERE repo=? AND status != 'archived' "
                "AND slug NOT IN ("
                "  SELECT slug FROM sdd_contract "
                "  WHERE repo=? AND stream_id=? AND status='stashed'"
                ")",
                (ts, repo, repo, stream_id),
            )
            artifacts_count = artifacts_result.fetchall()
            artifacts_count = artifacts_count[0][0] if artifacts_count else 0

        return {
            "contracts": contracts_count,
            "artifacts": artifacts_count,
            "outcome": outcome,
        }

    # -- session management ----------------------------------------------------

    def create_session(
        self,
        session_key: str,
        *,
        task_slug: str | None = None,
        phase: str | None = None,
    ) -> None:
        """Create a new session record. Idempotent — if the session already
        exists, update last_active_at.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo = self._repo()
        stream_id = self._sid()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT INTO sdd_session (repo, stream_id, session_key, task_slug, phase, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repo, stream_id, session_key) DO UPDATE SET last_active_at=excluded.last_active_at",
            (repo, stream_id, session_key, task_slug, phase, ts, ts),
        )

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        """Get a session by session_key. Returns None if not found."""
        repo = self._repo()
        stream_id = self._sid()
        row = self.conn.execute(
            "SELECT session_key, task_slug, phase, status, created_at, last_active_at "
            "FROM sdd_session WHERE repo=? AND stream_id=? AND session_key=?",
            (repo, stream_id, session_key),
        ).fetchone()
        if not row:
            return None
        return {
            "session_key": row[0],
            "task_slug": row[1],
            "phase": row[2],
            "status": row[3],
            "created_at": row[4],
            "last_active_at": row[5],
        }

    def list_active_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions for this repo+stream, ordered by last_active_at desc."""
        repo = self._repo()
        stream_id = self._sid()
        rows = self.conn.execute(
            "SELECT session_key, task_slug, phase, status, created_at, last_active_at "
            "FROM sdd_session WHERE repo=? AND stream_id=? AND status='active' "
            "ORDER BY last_active_at DESC",
            (repo, stream_id),
        ).fetchall()
        return [
            {
                "session_key": r[0],
                "task_slug": r[1],
                "phase": r[2],
                "status": r[3],
                "created_at": r[4],
                "last_active_at": r[5],
            }
            for r in rows
        ]

    def _flip_contract_status_for_slug(
        self,
        slug: str | None,
        from_status: str,
        to_status: str,
    ) -> int:
        """Flip every contract of *slug* from one status to another.

        Contracts are per-work-item (slug), not per-session — when a session
        parks or closes its work item, the work item's contracts move with it.
        Returns how many rows moved.
        """
        if not slug:
            return 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = self.conn.execute(
            "UPDATE sdd_contract SET status=?, updated_at=? "
            "WHERE repo=? AND stream_id=? AND slug=? AND status=?",
            (to_status, ts, self._repo(), self._sid(), slug, from_status),
        )
        count = result.fetchall()
        return count[0][0] if count else 0

    def _park_or_close_session(
        self,
        session_key: str,
        session_to: str,
        contract_to: str,
    ) -> bool:
        """Move an active session to a parked/terminal state, propagating to
        its work item's contracts.  Returns True when a session actually moved.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo = self._repo()
        stream_id = self._sid()
        row = self.conn.execute(
            "SELECT task_slug, status FROM sdd_session "
            "WHERE repo=? AND stream_id=? AND session_key=?",
            (repo, stream_id, session_key),
        ).fetchone()
        if row is None or row[1] != "active":
            return False
        task_slug = row[0]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE sdd_session SET status=?, last_active_at=? "
            "WHERE repo=? AND stream_id=? AND session_key=?",
            (session_to, ts, repo, stream_id, session_key),
        )
        self._flip_contract_status_for_slug(task_slug, "active", contract_to)
        return True

    def stash_session(self, session_key: str) -> bool:
        """Stash a session (active → stashed): parked, waiting to resume.

        The session's work-item contracts go to ``stashed`` with it, so they
        stop matching workflow reads but stay restorable via resume.
        """
        return self._park_or_close_session(session_key, "stashed", "stashed")

    def cancel_session(self, session_key: str) -> bool:
        """Cancel a session (active → cancelled): abandoned, never reached product."""
        return self._park_or_close_session(session_key, "cancelled", "cancelled")

    def archive_session(self, session_key: str) -> bool:
        """Archive a session (active → archived): the work item reached product."""
        return self._park_or_close_session(session_key, "archived", "archived")

    def update_session_activity(
        self,
        session_key: str,
        *,
        phase: str | None = None,
    ) -> None:
        """Update a session's last_active_at and optionally its phase."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo = self._repo()
        stream_id = self._sid()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if phase is not None:
            self.conn.execute(
                "UPDATE sdd_session SET last_active_at=?, phase=? "
                "WHERE repo=? AND stream_id=? AND session_key=?",
                (ts, phase, repo, stream_id, session_key),
            )
        else:
            self.conn.execute(
                "UPDATE sdd_session SET last_active_at=? "
                "WHERE repo=? AND stream_id=? AND session_key=?",
                (ts, repo, stream_id, session_key),
            )

    def resume_session(self, session_key: str) -> bool:
        """Re-activate a stashed session (and its contracts), refreshing activity.

        Idempotent for an already-active session (just refreshes activity).
        Returns True when the session is known for this repo+stream; False
        when no such session exists.

        Legacy note: rows parked as ``archived`` before the four-state model
        are also resumed — pre-v10 the archive verb *was* the stash, so those
        rows are parked work, not completed work.  ``cancelled`` stays
        terminal.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo = self._repo()
        stream_id = self._sid()
        row = self.conn.execute(
            "SELECT task_slug, status FROM sdd_session "
            "WHERE repo=? AND stream_id=? AND session_key=?",
            (repo, stream_id, session_key),
        ).fetchone()
        if not row or row[1] == "cancelled":
            return False
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE sdd_session SET status='active', last_active_at=? "
            "WHERE repo=? AND stream_id=? AND session_key=? AND status != 'cancelled'",
            (ts, repo, stream_id, session_key),
        )
        self._flip_contract_status_for_slug(row[0], "stashed", "active")
        return True

    # -- approval marker -------------------------------------------------------
    # Thin wrappers over the generic sdd_state read/write, using the already
    # -registered "approved" kind (REPO_SCOPED_KINDS/LEASED_KINDS above) which
    # predates this migration but had no reader/writer until now.

    def set_approval(
        self,
        phase: str,
        artifact_digest: str,
        *,
        approver: str | None = None,
        owner: str | None = None,
    ) -> None:
        value = json.dumps(
            {
                "artifact_digest": artifact_digest,
                "approver": approver,
                "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.write("approved", value, session_key=phase, owner=owner)

    def get_approval(self, phase: str) -> dict[str, Any] | None:
        raw = self.read("approved", session_key=phase)
        if raw is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(raw))
        except json.JSONDecodeError:
            return None

    # -- repo management -----------------------------------------------------

    def list_repos(self) -> list[str]:
        """Return all distinct repo slugs recorded in the store."""
        rows = self.conn.execute("SELECT DISTINCT repo FROM sdd_state").fetchall()
        state_repos = {r[0] for r in rows}

        rows = self.conn.execute("SELECT DISTINCT repo FROM sdd_contract").fetchall()
        contract_repos = {r[0] for r in rows}

        return sorted(state_repos | contract_repos)

    def rekey_legacy_rows(self, repo: str) -> int:
        """Move rows still under :data:`LEGACY_REPO_KEY` to ``repo``.

        Before task 11 the repo column held ``"state"`` for every repo that ever
        talked to the service, because the key was derived from the database
        filename.  Those rows have exactly one plausible owner — the repo the
        service was deployed against — so they are re-keyed to it wholesale.

        Legacy rows predate stream isolation and always carry ``stream_id=''``.
        Where the target repo already has a row for the same ``(stream_id,
        kind, session_key)`` or ``(stream_id, contract_id)``, the target wins
        and the legacy row is dropped: a row written deliberately under the
        real key is the better record than one from a bucket shared by every
        caller. A target that exists only under a different, non-empty
        ``stream_id`` does not block the move — the legacy row is promoted
        into the target repo's unscoped (``stream_id=''``) stream instead.

        Idempotent — it runs on every service start.  Returns rows moved.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        if repo == LEGACY_REPO_KEY:
            return 0

        moved = 0
        with self.transaction():
            self.conn.execute(
                """
                DELETE FROM sdd_state AS legacy
                 WHERE legacy.repo = ?
                   AND EXISTS (
                       SELECT 1 FROM sdd_state AS target
                        WHERE target.repo = ?
                          AND target.stream_id IS NOT DISTINCT FROM legacy.stream_id
                          AND target.kind = legacy.kind
                          AND target.session_key IS NOT DISTINCT FROM legacy.session_key
                   )
                """,
                (LEGACY_REPO_KEY, repo),
            )
            self.conn.execute(
                """
                DELETE FROM sdd_contract AS legacy
                 WHERE legacy.repo = ?
                   AND EXISTS (
                       SELECT 1 FROM sdd_contract AS target
                        WHERE target.repo = ?
                          AND target.stream_id IS NOT DISTINCT FROM legacy.stream_id
                          AND target.contract_id = legacy.contract_id
                   )
                """,
                (LEGACY_REPO_KEY, repo),
            )
            for table in ("sdd_state", "sdd_contract"):
                rows = self.conn.execute(
                    f"SELECT count(*) FROM {table} WHERE repo = ?",  # noqa: S608 — fixed literals
                    (LEGACY_REPO_KEY,),
                ).fetchall()
                moved += rows[0][0] if rows else 0
                self.conn.execute(
                    f"UPDATE {table} SET repo = ? WHERE repo = ?",  # noqa: S608 — fixed literals
                    (repo, LEGACY_REPO_KEY),
                )

        if moved:
            logger.info("state store: re-keyed %d legacy row(s) to repo %r", moved, repo)
        return moved

    def delete_repo_rows(self, repo: str) -> int:
        """Delete all rows for a repo from both sdd_state and sdd_contract.

        Returns the total number of deleted rows.
        """
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")

        result = self.conn.execute(
            "DELETE FROM sdd_state WHERE repo=?",
            (repo,),
        )
        state_count = result.fetchall()
        state_deleted = state_count[0][0] if state_count else 0

        result = self.conn.execute(
            "DELETE FROM sdd_contract WHERE repo=?",
            (repo,),
        )
        contract_count = result.fetchall()
        contract_deleted = contract_count[0][0] if contract_count else 0

        return state_deleted + contract_deleted

    # -- ship-phase tables ---------------------------------------------------

    def upsert_pr_narrative(
        self,
        task_slug: str,
        body: str,
        pr_url: str | None = None,
    ) -> dict[str, Any]:
        """Upsert a PR narrative body for a shipped task."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo, sid = self._repo(), self._sid()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT INTO pr_narrative (repo, stream_id, task_slug, body, pr_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repo, stream_id, task_slug) "
            "DO UPDATE SET body=EXCLUDED.body, pr_url=EXCLUDED.pr_url, updated_at=EXCLUDED.updated_at",
            (repo, sid, task_slug, body, pr_url, now),
        )
        return {"repo": repo, "task_slug": task_slug, "updated_at": now}

    def get_pr_narrative(self, task_slug: str) -> dict[str, Any] | None:
        """Get the PR narrative for a task, or None if not found."""
        repo, sid = self._repo(), self._sid()
        row = self.conn.execute(
            "SELECT repo, task_slug, pr_url, body, created_at, updated_at "
            "FROM pr_narrative WHERE repo=? AND stream_id=? AND task_slug=?",
            (repo, sid, task_slug),
        ).fetchone()
        if row is None:
            return None
        return {
            "repo": row[0],
            "task_slug": row[1],
            "pr_url": row[2],
            "body": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def record_ci_check(
        self,
        pr_url: str,
        check_name: str,
        check_status: str,
        check_conclusion: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        url: str | None = None,
    ) -> None:
        """Record a single CI check result for telemetry."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo, sid = self._repo(), self._sid()
        self.conn.execute(
            "INSERT INTO ci_telemetry (repo, stream_id, pr_url, check_name, "
            "check_status, check_conclusion, started_at, completed_at, url, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo,
                sid,
                pr_url,
                check_name,
                check_status,
                check_conclusion,
                started_at,
                completed_at,
                url,
                datetime.now(),
            ),
        )

    def list_ci_telemetry(self, pr_url: str) -> list[dict[str, Any]]:
        """Get all CI telemetry for a PR URL, ordered by capture time."""
        repo, sid = self._repo(), self._sid()
        rows = self.conn.execute(
            "SELECT pr_url, check_name, check_status, check_conclusion, "
            "started_at, completed_at, url, captured_at "
            "FROM ci_telemetry WHERE repo=? AND stream_id=? AND pr_url=? "
            "ORDER BY captured_at DESC",
            (repo, sid, pr_url),
        ).fetchall()
        return [
            {
                "pr_url": r[0],
                "check_name": r[1],
                "check_status": r[2],
                "check_conclusion": r[3],
                "started_at": r[4],
                "completed_at": r[5],
                "url": r[6],
                "captured_at": r[7],
            }
            for r in rows
        ]

    def upsert_pr_lifecycle(
        self,
        task_slug: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Upsert PR lifecycle state. Sets fields that are non-None, leaves others unchanged."""
        if self._read_only:
            raise RuntimeError("cannot write in read-only mode")
        repo, sid = self._repo(), self._sid()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build the UPSERT: always upsert with new values, but use COALESCE
        # logic to preserve existing values for fields not being updated.
        # We do this by reading the current row first, merging, then writing.
        row = self.conn.execute(
            "SELECT repo, task_slug, pr_url, auto_merge, merged_at, "
            "watcher_started, watcher_stopped, ci_failures, paused, paused_reason "
            "FROM pr_lifecycle WHERE repo=? AND stream_id=? AND task_slug=?",
            (repo, sid, task_slug),
        ).fetchone()

        if row is None:
            # New row — insert all columns explicitly so ON CONFLICT works
            # with DuckDB's PK constraint.
            defaults = {
                "pr_url": None,
                "auto_merge": 0,
                "merged_at": None,
                "watcher_started": None,
                "watcher_stopped": None,
                "ci_failures": 0,
                "paused": 0,
                "paused_reason": None,
            }
            merged = {**defaults, **kwargs}
            self.conn.execute(
                "INSERT INTO pr_lifecycle "
                "(repo, stream_id, task_slug, pr_url, auto_merge, merged_at, "
                "watcher_started, watcher_stopped, ci_failures, paused, paused_reason, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo, stream_id, task_slug) DO UPDATE SET "
                "pr_url=EXCLUDED.pr_url, "
                "auto_merge=EXCLUDED.auto_merge, "
                "merged_at=EXCLUDED.merged_at, "
                "watcher_started=EXCLUDED.watcher_started, "
                "watcher_stopped=EXCLUDED.watcher_stopped, "
                "ci_failures=EXCLUDED.ci_failures, "
                "paused=EXCLUDED.paused, "
                "paused_reason=EXCLUDED.paused_reason, "
                "updated_at=EXCLUDED.updated_at",
                (
                    repo,
                    sid,
                    task_slug,
                    merged["pr_url"],
                    merged["auto_merge"],
                    merged["merged_at"],
                    merged["watcher_started"],
                    merged["watcher_stopped"],
                    merged["ci_failures"],
                    merged["paused"],
                    merged["paused_reason"],
                    now,
                    now,
                ),
            )
        else:
            # Existing row — merge in new values, only for non-None kwargs
            sets: list[str] = []
            vals: list[Any] = []

            for field in (
                "pr_url",
                "auto_merge",
                "merged_at",
                "watcher_started",
                "watcher_stopped",
                "ci_failures",
                "paused",
                "paused_reason",
            ):
                val = kwargs.get(field)
                if val is not None:
                    sets.append(f"{field}=?")
                    vals.append(val)

            sets.append("updated_at=?")
            vals.append(now)
            vals.extend([repo, sid, task_slug])

            sql = (
                f"UPDATE pr_lifecycle SET {', '.join(sets)} "
                "WHERE repo=? AND stream_id=? AND task_slug=?"
            )
            self.conn.execute(sql, vals)

        return {
            "repo": repo,
            "task_slug": task_slug,
            "updated_at": now,
        }

    def get_pr_lifecycle(self, task_slug: str) -> dict[str, Any] | None:
        """Get PR lifecycle state for a task, or None if not found."""
        repo, sid = self._repo(), self._sid()
        row = self.conn.execute(
            "SELECT repo, task_slug, pr_url, auto_merge, merged_at, "
            "watcher_started, watcher_stopped, ci_failures, paused, paused_reason, "
            "created_at, updated_at "
            "FROM pr_lifecycle WHERE repo=? AND stream_id=? AND task_slug=?",
            (repo, sid, task_slug),
        ).fetchone()
        if row is None:
            return None
        return {
            "repo": row[0],
            "task_slug": row[1],
            "pr_url": row[2],
            "auto_merge": row[3],
            "merged_at": row[4],
            "watcher_started": row[5],
            "watcher_stopped": row[6],
            "ci_failures": row[7],
            "paused": row[8],
            "paused_reason": row[9],
            "created_at": row[10],
            "updated_at": row[11],
        }

    # -- scoped cursor helpers -------------------------------------------------
    # The "cursor" kind already supports a ``session_key`` parameter on
    # ``read`` / ``write`` / ``delete``.  These thin wrappers expose the
    # per-session cursor semantics used by the task / phase / proxy layers.

    def get_scoped_cursor(self, session_key: str) -> str | None:
        """Return the per-session cursor value, or ``None`` when unset."""
        return self.read("cursor", session_key=session_key)

    def set_scoped_cursor(self, session_key: str, value: str) -> StateWriteResult:
        """Store a per-session cursor value."""
        return self.write("cursor", value, session_key=session_key)

    def _delete_scoped_cursor(self, session_key: str) -> bool:
        """Remove a per-session cursor.  Returns ``True`` when a row existed."""
        repo = self._repo()
        sid = self._sid()
        cur = self.conn.execute(
            "DELETE FROM sdd_state WHERE repo=? AND stream_id=? AND kind='cursor' AND session_key=?",
            (repo, sid, session_key),
        )
        rows = cur.fetchall()
        return bool(rows) and rows[0][0] > 0

    # -- contract helpers ------------------------------------------------------

    def list_contracts_for_phase(self, phase: str) -> list[dict[str, Any]]:
        """List active contracts for a phase, ordered by ``contract_id`` (filename order)."""
        params: list[Any] = [self._repo(), self._sid(), phase]
        sql = (
            "SELECT contract_id, phase, slug, work_item, route, "
            "domain_tags, scope_touches, scope_avoids, success_criteria, "
            "status, supersedes, created_at, updated_at, body, stream_id "
            "FROM sdd_contract WHERE repo=? AND stream_id=? AND phase=? AND status='active' "
            "ORDER BY contract_id ASC"
        )
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_contract(row) for row in rows]

    def migrate_disk_contracts(self, roots: list[str]) -> dict[str, Any]:
        """Migrate legacy disk-based contract files into the store, then delete them.

        .. note:: Contracts are now store-backed; ``.md`` contract files on disk
           are sunset.  This method exists only to upgrade repos that still carry
           pre-migration contract files.

        Scans each root for ``contracts/active/<phase>/`` and
        ``contracts/archive/<phase>/`` directories.  Each file's YAML front-matter
        is parsed and written into the store via :meth:`put_contract`.
        All migrated files and their empty parent directories are deleted.

        Returns ``{migrated: int, errors: int, details: [str]}``.
        """
        import glob as glob_mod
        import os

        import yaml

        migrated = 0
        errors = 0
        details: list[str] = []

        for root in roots:
            for location in ("active", "archive"):
                pattern = os.path.join(root, ".agentalloy", "contracts", location, "*", "*.md")
                files = sorted(glob_mod.glob(pattern))
                for fpath in files:
                    try:
                        with open(fpath, encoding="utf-8") as fh:
                            raw = fh.read()
                        front = raw.split("---", 2)
                        meta = yaml.safe_load(front[1]) or {} if len(front) >= 3 else {}  # type: ignore[assignment]

                        # pyright: ignore[reportUnknownMemberType] — yaml.safe_load returns Any
                        contract_id: str = (
                            meta.get("contract_id") or meta.get("id") or meta.get("slug", "")
                        ) or ""
                        slug: str = meta.get("slug") or contract_id
                        phase: str = (
                            meta.get("phase")
                            or self._get_phase_from_path(fpath, location)
                            or "active"
                        )
                        work_item: str = meta.get("work_item") or meta.get("description", "")
                        body = front[2].strip() if len(front) >= 3 else raw.strip()

                        if not contract_id:
                            # Generate one from filename
                            contract_id = os.path.splitext(os.path.basename(fpath))[0]

                        self.put_contract(  # type: ignore[reportUnknownArgumentType]
                            str(contract_id),
                            phase=str(phase),
                            slug=str(slug or contract_id),
                            work_item=str(work_item),
                            body=body,
                        )
                        os.remove(fpath)
                        migrated += 1
                        details.append(
                            f"migrated {contract_id} ({location}/{os.path.basename(fpath)})"
                        )
                    except Exception as exc:
                        errors += 1
                        details.append(f"error migrating {fpath}: {exc}")

            # Prune empty directories left behind
            for location in ("active", "archive"):
                base = os.path.join(root, ".agentalloy", "contracts", location)
                self._prune_empty_dirs(base)
            contracts_base = os.path.join(root, ".agentalloy", "contracts")
            self._prune_empty_dirs(contracts_base)

        return {"migrated": migrated, "errors": errors, "details": details}

    @staticmethod
    def _get_phase_from_path(fpath: str, location: str) -> str:
        """Derive phase name from ``contracts/<location>/<phase>/*.md``."""
        parts = Path(fpath).parts
        try:
            idx = parts.index("contracts")
            return parts[idx + 2]  # contracts/<location>/<phase>
        except (ValueError, IndexError):
            return "active"

    @staticmethod
    def _prune_empty_dirs(dirpath: str) -> None:
        """Walk up from *dirpath* removing empty directories."""
        if not os.path.isdir(dirpath):
            return
        d = dirpath
        while True:
            if not os.path.isdir(d) or os.listdir(d):
                break
            parent = os.path.dirname(d)
            if parent == d:
                break  # reached root
            os.rmdir(d)
            d = parent


def open_state_store(
    db_path: str | Path,
    *,
    read_only: bool = False,
    repo: str | None = None,
    stream_id: str = "",
) -> DuckDBStateStore:
    """Open (and, in writer mode, migrate) the state store at ``db_path``.

    ``repo`` and ``stream_id`` are the default keys for handles that are not
    re-scoped with :meth:`DuckDBStateStore.for_repo`.  The service passes its
    own repo and then scopes per request; single-repo, single-worktree callers
    can rely on the default.
    """
    store = DuckDBStateStore(db_path, read_only=read_only, repo=repo, stream_id=stream_id).open()
    if not read_only:
        store.migrate()
    return store


# ---------------------------------------------------------------------------
# Process-wide handle
# ---------------------------------------------------------------------------
#
# DuckDB is single-writer: exactly one process may hold the state store open
# for writing, and that process is the service.  In-process callers outside
# the FastAPI request path — ``signals.skill_loader``, the watcher — cannot
# take a ``Depends(get_state_store)`` and must not open a second handle, so
# the service publishes the one it already owns here during its lifespan.
#
# Unbound is a *distinct* condition from "no phase recorded": a caller that
# treats the two alike turns a store outage into a repo that looks like it
# never had a phase.  ``process_store()`` returns ``None`` and every caller
# has to say out loud what it does with that.

_process_store: DuckDBStateStore | None = None


def bind_process_store(store: DuckDBStateStore | None) -> None:
    """Publish (or, with ``None``, retract) the process-wide store handle."""
    global _process_store
    _process_store = store


def process_store() -> DuckDBStateStore | None:
    """The store this process owns, or ``None`` when nothing has bound one.

    ``None`` means *the store is out of reach from here* — a CLI process, a
    test that did not bind one, or a service still in startup.  It never means
    the store is empty.
    """
    return _process_store
