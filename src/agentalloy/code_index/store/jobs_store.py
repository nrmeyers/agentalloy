"""SQLite jobs store for the code-index module (``jobs.sqlite``).

Adopted from codebase-indexer's ``app/services/jobs_store.py`` DAO, rewritten
for agentalloy: class-based instead of module-global state, actor/S3/GitHub
concerns dropped, ``indexed_repos`` reshaped around per-slug data dirs (for
unwire cleanup). Keeps the design that made the original robust:

- WAL mode + ``busy_timeout`` so readers never block on the single writer.
- One long-lived connection (``check_same_thread=False``) guarded by an
  instance ``threading.Lock`` — workload is far below anything justifying a
  pool.
- ``CodeIndexJob`` is a frozen snapshot; mutate through DAO methods only.
- ``worker_token`` set per-process at create time; :meth:`sweep_interrupted`
  retires active rows owned by a previous (now-dead) process at startup.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('index','embed','watch_partial')),
  slug TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('queued','running','done','failed','cancelled','interrupted')),
  phase TEXT,
  progress_pct REAL NOT NULL DEFAULT 0.0,
  files_total INTEGER NOT NULL DEFAULT 0,
  files_done INTEGER NOT NULL DEFAULT 0,
  current_file TEXT,
  symbol_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  embedding_count INTEGER NOT NULL DEFAULT 0,
  force_reindex INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_token TEXT,
  started_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_slug ON jobs(slug);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_started_at ON jobs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_slug_active
  ON jobs(slug) WHERE status IN ('queued','running');

CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('info','warn','error')),
  message TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id);

CREATE TABLE IF NOT EXISTS indexed_repos (
  slug TEXT PRIMARY KEY,
  repo_path TEXT NOT NULL,
  data_dir TEXT NOT NULL,
  last_indexed_at INTEGER,
  head_sha TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""

_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled", "interrupted"})
_ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running"})


@dataclass(frozen=True)
class CodeIndexJob:
    """Immutable snapshot of one ``jobs`` row."""

    job_id: str
    kind: str
    slug: str
    repo_path: str
    status: str
    phase: str | None
    progress_pct: float
    files_total: int
    files_done: int
    current_file: str | None
    symbol_count: int
    edge_count: int
    embedding_count: int
    force_reindex: bool
    error: str | None
    cancel_requested: bool
    worker_token: str | None
    started_at: float
    updated_at: float
    finished_at: float | None


@dataclass(frozen=True)
class IndexedRepo:
    """One row in the ``indexed_repos`` registry.

    ``data_dir`` records the per-slug storage directory so ``unwire`` can
    remove exactly what an index run created.
    """

    slug: str
    repo_path: str
    data_dir: str
    last_indexed_at: int | None
    head_sha: str | None
    created_at: int
    updated_at: int


def _row_to_job(row: sqlite3.Row) -> CodeIndexJob:
    return CodeIndexJob(
        job_id=str(row["job_id"]),
        kind=str(row["kind"]),
        slug=str(row["slug"]),
        repo_path=str(row["repo_path"]),
        status=str(row["status"]),
        phase=None if row["phase"] is None else str(row["phase"]),
        progress_pct=float(row["progress_pct"]),
        files_total=int(row["files_total"]),
        files_done=int(row["files_done"]),
        current_file=None if row["current_file"] is None else str(row["current_file"]),
        symbol_count=int(row["symbol_count"]),
        edge_count=int(row["edge_count"]),
        embedding_count=int(row["embedding_count"]),
        force_reindex=bool(row["force_reindex"]),
        error=None if row["error"] is None else str(row["error"]),
        cancel_requested=bool(row["cancel_requested"]),
        worker_token=None if row["worker_token"] is None else str(row["worker_token"]),
        started_at=float(row["started_at"]),
        updated_at=float(row["updated_at"]),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
    )


def _row_to_repo(row: sqlite3.Row) -> IndexedRepo:
    return IndexedRepo(
        slug=str(row["slug"]),
        repo_path=str(row["repo_path"]),
        data_dir=str(row["data_dir"]),
        last_indexed_at=(None if row["last_indexed_at"] is None else int(row["last_indexed_at"])),
        head_sha=None if row["head_sha"] is None else str(row["head_sha"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class CodeIndexJobsStore:
    """Thin DAO over one WAL-mode SQLite database shared by all repos."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        conn = sqlite3.connect(self._db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_DDL)
        self._conn: sqlite3.Connection | None = conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("CodeIndexJobsStore is closed")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("failed to close jobs sqlite connection", exc_info=True)
            self._conn = None

    # -- jobs: create / read ---------------------------------------------------

    def create_job(
        self,
        *,
        slug: str,
        repo_path: str,
        kind: str = "index",
        force_reindex: bool = False,
        worker_token: str | None = None,
        job_id: str | None = None,
        initial_status: str = "running",
        initial_phase: str = "queued",
    ) -> CodeIndexJob:
        """Insert a new job row and return its snapshot."""
        job_id = job_id or str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO jobs (
                  job_id, kind, slug, repo_path, status, phase,
                  force_reindex, worker_token, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    slug,
                    repo_path,
                    initial_status,
                    initial_phase,
                    1 if force_reindex else 0,
                    worker_token,
                    now,
                    now,
                ),
            )
            row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None  # just inserted
        return _row_to_job(row)

    def get_job(self, job_id: str) -> CodeIndexJob | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        slug: str | None = None,
        status: set[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CodeIndexJob]:
        """Newest-first paged history; filters compose with AND."""
        where: list[str] = []
        params: list[object] = []
        if slug is not None:
            where.append("slug = ?")
            params.append(slug)
        if status:
            placeholders = ",".join("?" for _ in status)
            where.append(f"status IN ({placeholders})")
            params.extend(sorted(status))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_job(r) for r in rows]

    def find_active(self, slug: str) -> CodeIndexJob | None:
        """The most recent queued/running job for ``slug``, if any (used to
        fail-fast on duplicate concurrent index requests)."""
        row = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE slug = ? AND status IN ('queued','running')
            ORDER BY started_at DESC LIMIT 1
            """,
            (slug,),
        ).fetchone()
        return _row_to_job(row) if row is not None else None

    # -- jobs: progress ----------------------------------------------------------

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        progress_pct: float | None = None,
        files_total: int | None = None,
        files_done: int | None = None,
        current_file: str | None = None,
        symbol_count: int | None = None,
        edge_count: int | None = None,
        embedding_count: int | None = None,
    ) -> None:
        """Partial-update progress fields; unset args are left unchanged."""
        fields: list[str] = []
        params: list[object] = []
        for col, val in (
            ("phase", phase),
            ("progress_pct", progress_pct),
            ("files_total", files_total),
            ("files_done", files_done),
            ("current_file", current_file),
            ("symbol_count", symbol_count),
            ("edge_count", edge_count),
            ("embedding_count", embedding_count),
        ):
            if val is not None:
                fields.append(f"{col} = ?")
                params.append(val)
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(time.time())
        params.append(job_id)
        sql = f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?"
        with self._lock:
            self.conn.execute(sql, params)

    def touch_heartbeat(self, job_id: str) -> None:
        """Advance only the ``updated_at`` liveness clock of a running job —
        proves a long, callback-silent write phase is still alive without
        mutating phase/progress. No-op for terminal/absent rows."""
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ? AND status = 'running'",
                (time.time(), job_id),
            )

    # -- jobs: terminal transitions -------------------------------------------------

    def mark_done(
        self, job_id: str, *, symbol_count: int, edge_count: int, embedding_count: int
    ) -> None:
        """Idempotent transition to ``status='done'``, ``progress_pct=100``."""
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs SET
                  status = 'done', phase = 'done', progress_pct = 100.0,
                  symbol_count = ?, edge_count = ?, embedding_count = ?,
                  error = NULL, updated_at = ?,
                  finished_at = COALESCE(finished_at, ?)
                WHERE job_id = ?
                """,
                (int(symbol_count), int(edge_count), int(embedding_count), now, now, job_id),
            )
        self._record_event_quiet(
            job_id,
            "info",
            f"done: symbols={int(symbol_count)} edges={int(edge_count)} "
            f"embeddings={int(embedding_count)}",
        )

    def mark_failed(self, job_id: str, *, error: str, terminal_status: str = "failed") -> None:
        """Idempotent transition to a terminal failure status
        (``failed`` | ``cancelled`` | ``interrupted``)."""
        now = time.time()
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs SET
                  status = ?, error = ?, updated_at = ?,
                  finished_at = COALESCE(finished_at, ?)
                WHERE job_id = ?
                """,
                (terminal_status, error, now, now, job_id),
            )
        level = "error" if terminal_status == "failed" else "warn"
        self._record_event_quiet(job_id, level, f"{terminal_status}: {error[:500]}")

    def request_cancel(self, job_id: str) -> bool:
        """Set ``cancel_requested=1``. True iff the row exists and was active."""
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE jobs SET cancel_requested = 1, updated_at = ?
                WHERE job_id = ? AND status IN ('queued','running')
                """,
                (time.time(), job_id),
            )
            return cur.rowcount > 0

    def is_cancel_requested(self, job_id: str) -> bool:
        """Lock-free read used by the worker between phases."""
        row = self.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return bool(row[0]) if row is not None else False

    def sweep_interrupted(self, worker_token: str) -> int:
        """Flag active rows from a previous process as ``interrupted``.

        Called at service startup with a fresh per-process token; rows whose
        stored token differs (or is NULL) are owned by a now-dead process.
        """
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE jobs SET
                  status = 'interrupted',
                  error = COALESCE(error, 'service restart'),
                  updated_at = ?, finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('queued','running')
                  AND (worker_token IS NULL OR worker_token != ?)
                """,
                (now, now, worker_token),
            )
            return int(cur.rowcount)

    # -- job events --------------------------------------------------------------

    def record_event(self, job_id: str, level: str, message: str) -> int:
        """Append a row to ``job_events``. Returns the new event id."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO job_events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (job_id, int(time.time()), level, message),
            )
            return int(cur.lastrowid or 0)

    def list_job_events(self, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 1000))
        rows = self.conn.execute(
            """
            SELECT id, job_id, ts, level, message FROM job_events
            WHERE job_id = ? ORDER BY ts ASC, id ASC LIMIT ?
            """,
            (job_id, capped),
        ).fetchall()
        return [dict(r) for r in rows]

    def _record_event_quiet(self, job_id: str, level: str, message: str) -> None:
        """Event write that can never bubble out of a terminal transition."""
        try:
            self.record_event(job_id, level, message)
        except Exception:  # noqa: BLE001
            logger.debug("record_event(%s, %s) failed (non-fatal)", job_id, level)

    # -- indexed_repos registry -----------------------------------------------------

    def upsert_repo(
        self,
        *,
        slug: str,
        repo_path: str,
        data_dir: str,
        head_sha: str | None = None,
    ) -> None:
        """Insert or update the registry row for ``slug``.

        Preserves ``created_at`` and ``last_indexed_at`` on update; the latter
        advances via :meth:`mark_indexed` after a successful index run.
        """
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO indexed_repos (
                  slug, repo_path, data_dir, last_indexed_at, head_sha,
                  created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                  repo_path = excluded.repo_path,
                  data_dir = excluded.data_dir,
                  head_sha = COALESCE(excluded.head_sha, indexed_repos.head_sha),
                  updated_at = excluded.updated_at
                """,
                (slug, repo_path, data_dir, head_sha, now, now),
            )

    def mark_indexed(self, slug: str, *, head_sha: str | None = None) -> bool:
        """Advance ``last_indexed_at`` (and optionally ``head_sha``)."""
        now = int(time.time())
        with self._lock:
            if head_sha is None:
                cur = self.conn.execute(
                    "UPDATE indexed_repos SET last_indexed_at = ?, updated_at = ? WHERE slug = ?",
                    (now, now, slug),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE indexed_repos SET last_indexed_at = ?, head_sha = ?, "
                    "updated_at = ? WHERE slug = ?",
                    (now, head_sha, now, slug),
                )
            return cur.rowcount > 0

    def get_repo(self, slug: str) -> IndexedRepo | None:
        row = self.conn.execute("SELECT * FROM indexed_repos WHERE slug = ?", (slug,)).fetchone()
        return _row_to_repo(row) if row is not None else None

    def list_repos(self) -> list[IndexedRepo]:
        rows = self.conn.execute("SELECT * FROM indexed_repos ORDER BY updated_at DESC").fetchall()
        return [_row_to_repo(r) for r in rows]

    def delete_repo(self, slug: str) -> bool:
        """Drop a registry row (unwire). True iff one was deleted."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM indexed_repos WHERE slug = ?", (slug,))
            return cur.rowcount > 0

    # -- diagnostics ---------------------------------------------------------------

    def journal_mode(self) -> str:
        """Current journal mode (tests verify WAL)."""
        row = self.conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else ""
