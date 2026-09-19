"""State store: DuckDB-backed persistence for contracts, artifacts, phases.

Python owns the single RW handle; Rust opens RO only (AC-7 boundary).
"""

import hashlib
import threading
from pathlib import Path
from typing import Any

import duckdb


class _Result:
    """Materialized query result (rows fetched under the lock)."""

    __slots__ = ("_rows",)

    def __init__(self, rows: list[tuple[Any, ...]]):
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _LockedConn:
    """Serializes DuckDB access across threads.

    FastAPI runs sync endpoints on a threadpool, so store methods execute
    concurrently — but a DuckDB Python connection attaches its pending
    result to the connection, so interleaved execute/fetch pairs from two
    threads corrupt each other. Every execute here runs AND fetches under
    one lock, returning a materialized result.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params: list[Any] | None = None) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, params) if params is not None else self._conn.execute(sql)
            try:
                rows = cur.fetchall()
            except duckdb.Error:
                rows = []
            return _Result(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# The SDD lifecycle, in order. Single source of truth — phase_machine and
# executors validate against this, so an unknown phase can never be persisted.
PHASE_ORDER: tuple[str, ...] = ("intake", "spec", "design", "plan", "build", "qa", "ship")
LIFECYCLE_START: str = PHASE_ORDER[0]


class PhaseAdvanceError(ValueError):
    """A lifecycle advance the state leg refuses to persist.

    Subclass of ValueError: callers that guard store writes with
    ``except ValueError`` (artifact/contract bodies, phase advances) keep
    working unchanged.
    """


class StateStore:
    """DuckDB state store for contracts, artifacts, phases.

    read_only=True is for secondary consumers (the steering proxy): the
    service owns the single RW handle, the proxy opens RO for phase reads.

    Lifecycle state (phase, contracts, artifacts, approvals) is scoped by a
    project key — the empty string is the legacy/global scope. Use
    ``scoped(project)`` to get a view bound to one project; views share the
    underlying connection (single-writer invariant holds).
    """

    def __init__(self, db_path: str, read_only: bool = False, project: str = ""):
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.project = project
        self.conn = _LockedConn(duckdb.connect(str(self.db_path), read_only=read_only))
        if not read_only:
            self._init_schema()

    def scoped(self, project: str) -> "StateStore":
        """A view of this store bound to a project key. Shares the
        connection — do NOT close() a scoped view independently."""
        clone = object.__new__(StateStore)
        clone.db_path = self.db_path
        clone.read_only = self.read_only
        clone.project = project or ""
        clone.conn = self.conn
        return clone

    def _init_schema(self) -> None:
        """Initialize tables (and migrate pre-project-scope databases)."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                project TEXT DEFAULT '',
                slug TEXT,
                domain_tags TEXT,  -- JSON array
                touches TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, slug)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS artifacts (
                project TEXT DEFAULT '',
                phase TEXT,
                name TEXT,
                body TEXT,
                digest TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, phase, name)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS phases (
                project TEXT PRIMARY KEY DEFAULT '',
                current_phase TEXT DEFAULT 'intake',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Approval gates table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                project TEXT DEFAULT '',
                phase_transition TEXT,
                artifact_digest TEXT,
                approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, phase_transition)
            )
        """)
        # Sessions table (AC-11: crash-resumable sessions). session_key is a
        # globally unique conversation hash, so it stays the sole PK; project
        # records which scope the session belongs to.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                project TEXT DEFAULT '',
                status TEXT DEFAULT 'active',  -- active, stashed, archived, cancelled
                phase TEXT,
                cursor_json TEXT,  -- work-item cursor state
                snapshot_json TEXT,  -- full state snapshot (contracts, approvals, etc.)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._migrate_project_scope()
        # Work-item cursor table
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS work_item_seq
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS work_items (
                id INTEGER DEFAULT nextval('work_item_seq'),
                session_key TEXT DEFAULT 'default',
                task_slug TEXT,
                status TEXT DEFAULT 'pending',  -- pending, in_progress, done, blocked
                contract_slug TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Initialize the legacy/global phase row — new scopes start at
        # intake lazily (get_current_phase falls back, advance upserts).
        self.conn.execute(
            "INSERT OR IGNORE INTO phases (project, current_phase) VALUES ('', ?)",
            [LIFECYCLE_START],
        )

    def _migrate_project_scope(self) -> None:
        """Migrate a pre-project-scope database in place.

        Old tables lack the project column (and phases was a single row
        keyed id=1). Existing rows land in the legacy '' scope, preserving
        prior behavior for un-scoped callers.
        """
        cols = self.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
        ).fetchall()
        by_table: dict[str, set[str]] = {}
        for t, c in cols:
            by_table.setdefault(str(t), set()).add(str(c))

        def migrate(table: str, copy_sql: str) -> None:
            if "project" in by_table.get(table, {"project"}):
                return
            legacy = f"{table}_preproject"
            self.conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
            # Recreate with the new schema (CREATE IF NOT EXISTS above was a
            # no-op while the old table occupied the name).
            self._init_schema_table(table)
            self.conn.execute(copy_sql.format(table=table, legacy=legacy))
            self.conn.execute(f"DROP TABLE {legacy}")

        migrate(
            "contracts",
            "INSERT INTO {table} (project, slug, domain_tags, touches, created_at, updated_at) "
            "SELECT '', slug, domain_tags, touches, created_at, updated_at FROM {legacy}",
        )
        migrate(
            "artifacts",
            "INSERT INTO {table} (project, phase, name, body, digest, created_at) "
            "SELECT '', phase, name, body, digest, created_at FROM {legacy}",
        )
        migrate(
            "phases",
            "INSERT INTO {table} (project, current_phase, updated_at) "
            "SELECT '', current_phase, updated_at FROM {legacy} WHERE id = 1",
        )
        migrate(
            "approvals",
            "INSERT INTO {table} (project, phase_transition, artifact_digest, approved_at) "
            "SELECT '', phase_transition, artifact_digest, approved_at FROM {legacy}",
        )
        migrate(
            "sessions",
            "INSERT INTO {table} (session_key, project, status, phase, cursor_json, "
            "snapshot_json, created_at, updated_at) "
            "SELECT session_key, '', status, phase, cursor_json, snapshot_json, "
            "created_at, updated_at FROM {legacy}",
        )

    def _init_schema_table(self, table: str) -> None:
        """Recreate one table with the current schema (migration helper)."""
        ddl = {
            "contracts": """
                CREATE TABLE contracts (
                    project TEXT DEFAULT '',
                    slug TEXT,
                    domain_tags TEXT,
                    touches TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project, slug)
                )""",
            "artifacts": """
                CREATE TABLE artifacts (
                    project TEXT DEFAULT '',
                    phase TEXT,
                    name TEXT,
                    body TEXT,
                    digest TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project, phase, name)
                )""",
            "phases": """
                CREATE TABLE phases (
                    project TEXT PRIMARY KEY DEFAULT '',
                    current_phase TEXT DEFAULT 'intake',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            "approvals": """
                CREATE TABLE approvals (
                    project TEXT DEFAULT '',
                    phase_transition TEXT,
                    artifact_digest TEXT,
                    approved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project, phase_transition)
                )""",
            "sessions": """
                CREATE TABLE sessions (
                    session_key TEXT PRIMARY KEY,
                    project TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    phase TEXT,
                    cursor_json TEXT,
                    snapshot_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
        }
        self.conn.execute(ddl[table])

    def add_contract(self, slug: str, domain_tags: list[str], touches: str) -> None:
        """Add or update a contract."""
        import json
        from datetime import datetime

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO contracts (project, slug, domain_tags, touches, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, slug) DO UPDATE SET
                domain_tags = excluded.domain_tags,
                touches = excluded.touches,
                updated_at = excluded.updated_at
            """,
            [self.project, slug, json.dumps(domain_tags), touches, now],
        )

    def get_contract(self, slug: str) -> dict[str, Any] | None:
        """Get contract by slug."""
        import json

        result = self.conn.execute(
            "SELECT slug, domain_tags, touches FROM contracts WHERE project = ? AND slug = ?",
            [self.project, slug],
        ).fetchone()
        if result:
            return {
                "slug": result[0],
                "domain_tags": json.loads(result[1]),
                "touches": result[2],
            }
        return None

    def list_contracts(self) -> list[dict[str, Any]]:
        """All contracts as ``{"slug", "domain_tags", "touches"}`` dicts (SDD
        projection source)."""
        import json

        results = self.conn.execute(
            "SELECT slug, domain_tags, touches FROM contracts WHERE project = ? ORDER BY slug",
            [self.project],
        ).fetchall()
        return [
            {
                "slug": r[0],
                "domain_tags": json.loads(r[1]) if r[1] else [],
                "touches": r[2] or "",
            }
            for r in results
        ]

    def record_artifact(self, phase: str, name: str, body: str) -> str:
        """Record a phase artifact. Returns the digest.

        An empty or whitespace-only body is rejected: the artifact is the
        phase's evidence (the advance gate reads the {phase}-exit row), and
        a placeholder row must not exist to be mistaken for it.
        """
        if not body or not body.strip():
            raise ValueError(f"artifact body must not be empty (phase={phase!r}, name={name!r})")
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        self.conn.execute(
            """
            INSERT INTO artifacts (project, phase, name, body, digest)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, phase, name) DO UPDATE SET
                body = excluded.body,
                digest = excluded.digest
            """,
            [self.project, phase, name, body, digest],
        )
        # Invalidate any approval for transitions out of this phase
        self._invalidate_approval(phase)
        return digest

    def get_artifact(self, phase: str, name: str) -> str | None:
        """Get artifact body."""
        result = self.conn.execute(
            "SELECT body FROM artifacts WHERE project = ? AND phase = ? AND name = ?",
            [self.project, phase, name],
        ).fetchone()
        return result[0] if result else None

    def get_artifact_digest(self, phase: str, name: str) -> str | None:
        """Get artifact digest."""
        result = self.conn.execute(
            "SELECT digest FROM artifacts WHERE project = ? AND phase = ? AND name = ?",
            [self.project, phase, name],
        ).fetchone()
        return result[0] if result else None

    def list_artifacts(self) -> list[dict[str, Any]]:
        """All artifacts as ``{"phase", "name", "body", "digest"}`` dicts
        (SDD projection source)."""
        results = self.conn.execute(
            "SELECT phase, name, body, digest FROM artifacts WHERE project = ? "
            "ORDER BY phase, name",
            [self.project],
        ).fetchall()
        return [
            {"phase": r[0], "name": r[1], "body": r[2] or "", "digest": r[3] or ""} for r in results
        ]

    def get_current_phase(self) -> str:
        """Get current phase (scopes without a row are at lifecycle start)."""
        result = self.conn.execute(
            "SELECT current_phase FROM phases WHERE project = ?", [self.project]
        ).fetchone()
        return result[0] if result else LIFECYCLE_START

    def advance_phase(self, target: str) -> None:
        """Advance the lifecycle to *target* — the state leg's hard gate.

        Enforced at the write, so no caller (LLM tool, graph node, session
        restore, hand-rolled SQL via the tool layer) can persist an
        illegitimate jump:

        - unknown phase               → rejected
        - corrupt current phase       → rejected (operator reset required)
        - same phase                  → no-op
        - backward move               → allowed (retry/rollback; never exit-gated)
        - non-adjacent forward jump   → rejected (phases are walked one at a time)
        - adjacent forward move       → requires a substantive exit artifact
        """
        if target not in PHASE_ORDER:
            raise PhaseAdvanceError(
                f"unknown phase {target!r}; legal phases: {' → '.join(PHASE_ORDER)}"
            )
        current = self.get_current_phase()
        if current not in PHASE_ORDER:
            raise PhaseAdvanceError(
                f"current phase {current!r} is not part of the lifecycle; "
                "use reset_phase() to recover"
            )
        if current == target:
            return
        cur_idx = PHASE_ORDER.index(current)
        tgt_idx = PHASE_ORDER.index(target)
        if tgt_idx < cur_idx:
            # Backward move — the exit gate never guards retries.
            self._set_current_phase(target)
            return
        if tgt_idx > cur_idx + 1:
            raise PhaseAdvanceError(
                f"cannot advance {current} → {target}: the lifecycle is walked one phase at a time"
            )
        if not self.has_exit_artifact(current):
            raise PhaseAdvanceError(
                f"no substantive exit artifact for phase {current!r} — record "
                f"{current!r}-exit with the phase's evidence before advancing"
            )
        self._set_current_phase(target)

    def _set_current_phase(self, target: str) -> None:
        """Raw phase upsert. No validation — internal use only (advance_phase
        is the public gated path; reset/restore are the two other callers)."""
        self.conn.execute(
            """
            INSERT INTO phases (project, current_phase, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(project) DO UPDATE SET
                current_phase = excluded.current_phase,
                updated_at = excluded.updated_at
            """,
            [self.project, target],
        )

    def has_exit_artifact(self, phase: str) -> bool:
        """True when a substantive {phase}-exit artifact exists.

        The advance gate's predicate: the row must exist AND carry a
        non-empty body — a placeholder row is not evidence.
        """
        body = self.get_artifact(phase, f"{phase}-exit")
        return bool(body is not None and body.strip())

    def reset_phase(self) -> str:
        """Reset the lifecycle to its start (intake) and clear approvals.

        Approvals pin to artifact digests of a finished round of the lifecycle;
        after a reset they are stale, so they go. Contracts and artifacts are
        kept — they are project knowledge, not lifecycle state.
        Returns the new phase.
        """
        self.conn.execute("DELETE FROM approvals WHERE project = ?", [self.project])
        self._set_current_phase(LIFECYCLE_START)
        return LIFECYCLE_START

    # --- Approval gates (AC-9) ---

    def record_approval(self, phase_transition: str, artifact_digest: str) -> None:
        """Record approval for a phase transition."""
        from datetime import datetime

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO approvals (project, phase_transition, artifact_digest, approved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project, phase_transition) DO UPDATE SET
                artifact_digest = excluded.artifact_digest,
                approved_at = excluded.approved_at
            """,
            [self.project, phase_transition, artifact_digest, now],
        )

    def is_approved(self, phase_transition: str, current_digest: str) -> bool:
        """Check if a phase transition is approved and digest matches."""
        result = self.conn.execute(
            "SELECT artifact_digest FROM approvals WHERE project = ? AND phase_transition = ?",
            [self.project, phase_transition],
        ).fetchone()
        if result is None:
            return False
        return str(result[0]) == current_digest

    def _invalidate_approval(self, phase: str) -> None:
        """Invalidate approvals for transitions out of a phase."""
        # Any transition starting with this phase is invalidated
        self.conn.execute(
            "DELETE FROM approvals WHERE project = ? AND phase_transition LIKE ?",
            [self.project, f"{phase}→%"],
        )

    def get_exit_artifact_digest(self, phase: str) -> str | None:
        """Get the digest of the exit artifact for a phase."""
        return self.get_artifact_digest(phase, f"{phase}-exit")

    # --- Sessions (AC-11) ---

    def create_session(self, session_key: str) -> None:
        """Create a new session."""
        from datetime import datetime

        now = datetime.now().isoformat()
        phase = self.get_current_phase()
        self.conn.execute(
            """
            INSERT INTO sessions (session_key, project, status, phase, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            [session_key, self.project, phase, now],
        )

    def stash_session(self, session_key: str) -> dict[str, Any] | None:
        """Stash a session — snapshot state and mark as stashed."""
        import json
        from datetime import datetime

        snapshot = self._build_snapshot()
        cursor = self._get_cursor_state(session_key)
        now = datetime.now().isoformat()

        self.conn.execute(
            """
            UPDATE sessions
            SET status = 'stashed', phase = ?, snapshot_json = ?,
                cursor_json = ?, updated_at = ?
            WHERE session_key = ? AND status = 'active'
            """,
            [
                snapshot["phase"],
                json.dumps(snapshot),
                json.dumps(cursor),
                now,
                session_key,
            ],
        )
        # Check if the update took effect
        row = self.conn.execute(
            "SELECT status FROM sessions WHERE session_key = ?", [session_key]
        ).fetchone()
        if row and str(row[0]) == "stashed":
            return snapshot
        return None

    def resume_session(self, session_key: str) -> dict[str, Any] | None:
        """Resume a stashed session — restore state and mark as active."""
        import json
        from datetime import datetime

        result = self.conn.execute(
            """
            SELECT snapshot_json, cursor_json, phase
            FROM sessions WHERE session_key = ? AND status = 'stashed'
            """,
            [session_key],
        ).fetchone()

        if result is None:
            return None

        snapshot = json.loads(result[0]) if result[0] else {}
        cursor = json.loads(result[1]) if result[1] else {}
        phase = result[2] or LIFECYCLE_START

        # Restore state. Phase restore uses the raw setter: a snapshot was
        # captured at a phase that was legitimate when taken, so recovery
        # must never fail on exit-gate state (crash recovery, stash/resume).
        self._restore_snapshot(snapshot)
        if phase and phase in PHASE_ORDER:
            self._set_current_phase(phase)

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            UPDATE sessions SET status = 'active', updated_at = ?
            WHERE session_key = ?
            """,
            [now, session_key],
        )
        return {"snapshot": snapshot, "cursor": cursor, "phase": phase}

    def archive_session(self, session_key: str) -> bool:
        """Archive a completed session."""
        from datetime import datetime

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            UPDATE sessions SET status = 'archived', updated_at = ?
            WHERE session_key = ? AND status IN ('active', 'stashed')
            """,
            [now, session_key],
        )
        row = self.conn.execute(
            "SELECT status FROM sessions WHERE session_key = ?", [session_key]
        ).fetchone()
        return row is not None and str(row[0]) == "archived"

    def cancel_session(self, session_key: str) -> bool:
        """Cancel a session."""
        from datetime import datetime

        # Check current status first
        row = self.conn.execute(
            "SELECT status FROM sessions WHERE session_key = ?", [session_key]
        ).fetchone()
        if row is None or str(row[0]) == "cancelled":
            return False

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            UPDATE sessions SET status = 'cancelled', updated_at = ?
            WHERE session_key = ?
            """,
            [now, session_key],
        )
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions."""
        results = self.conn.execute(
            """
            SELECT session_key, status, phase, created_at, updated_at, project
            FROM sessions ORDER BY updated_at DESC
            """
        ).fetchall()
        return [
            {
                "session_key": r[0],
                "status": r[1],
                "phase": r[2],
                "created_at": str(r[3]) if r[3] else None,
                "updated_at": str(r[4]) if r[4] else None,
                "project": r[5] or "",
            }
            for r in results
        ]

    def get_session(self, session_key: str) -> dict[str, Any] | None:
        """Get session details."""
        result = self.conn.execute(
            """
            SELECT session_key, status, phase, snapshot_json, cursor_json, created_at, updated_at
            FROM sessions WHERE session_key = ?
            """,
            [session_key],
        ).fetchone()
        if result is None:
            return None
        import json

        return {
            "session_key": result[0],
            "status": result[1],
            "phase": result[2],
            "snapshot": json.loads(result[3]) if result[3] else None,
            "cursor": json.loads(result[4]) if result[4] else None,
            "created_at": str(result[5]) if result[5] else None,
            "updated_at": str(result[6]) if result[6] else None,
        }

    def _build_snapshot(self) -> dict[str, Any]:
        """Build a state snapshot for stashing."""
        import json

        contracts = self.conn.execute(
            "SELECT slug, domain_tags, touches FROM contracts WHERE project = ?", [self.project]
        ).fetchall()
        artifacts = self.conn.execute(
            "SELECT phase, name, digest FROM artifacts WHERE project = ?", [self.project]
        ).fetchall()
        approvals = self.conn.execute(
            "SELECT phase_transition, artifact_digest FROM approvals WHERE project = ?",
            [self.project],
        ).fetchall()

        return {
            "phase": self.get_current_phase(),
            "contracts": [
                {"slug": r[0], "domain_tags": json.loads(r[1]), "touches": r[2]} for r in contracts
            ],
            "artifacts": [{"phase": r[0], "name": r[1], "digest": r[2]} for r in artifacts],
            "approvals": [{"transition": r[0], "digest": r[1]} for r in approvals],
        }

    def _restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Restore state from a snapshot."""
        import json
        from datetime import datetime

        now = datetime.now().isoformat()

        # Restore contracts
        for c in snapshot.get("contracts", []):
            self.conn.execute(
                """
                INSERT INTO contracts (project, slug, domain_tags, touches, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project, slug) DO UPDATE SET
                    domain_tags = excluded.domain_tags,
                    touches = excluded.touches,
                    updated_at = excluded.updated_at
                """,
                [self.project, c["slug"], json.dumps(c["domain_tags"]), c["touches"], now],
            )

        # Restore approvals
        for a in snapshot.get("approvals", []):
            self.conn.execute(
                """
                INSERT INTO approvals (project, phase_transition, artifact_digest, approved_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project, phase_transition) DO UPDATE SET
                    artifact_digest = excluded.artifact_digest,
                    approved_at = excluded.approved_at
                """,
                [self.project, a["transition"], a["digest"], now],
            )

    def _get_cursor_state(self, session_key: str) -> dict[str, Any]:
        """Get work-item cursor state for a session."""
        items = self.conn.execute(
            """
            SELECT task_slug, status, contract_slug
            FROM work_items WHERE session_key = ?
            ORDER BY id
            """,
            [session_key],
        ).fetchall()
        return {
            "items": [{"task_slug": r[0], "status": r[1], "contract_slug": r[2]} for r in items]
        }

    # --- Work-item cursor ---

    def add_work_item(
        self,
        task_slug: str,
        session_key: str = "default",
        contract_slug: str | None = None,
    ) -> int:
        """Add a work item and return its ID."""
        # RETURNING id, not a follow-up currval(): two concurrent inserts
        # would otherwise hand each caller the other's id.
        result = self.conn.execute(
            """
            INSERT INTO work_items (session_key, task_slug, contract_slug)
            VALUES (?, ?, ?) RETURNING id
            """,
            [session_key, task_slug, contract_slug],
        ).fetchone()
        return int(result[0]) if result else 0

    def update_work_item(self, item_id: int, status: str) -> None:
        """Update work item status."""
        from datetime import datetime

        now = datetime.now().isoformat()
        self.conn.execute(
            """
            UPDATE work_items SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            [status, now, item_id],
        )

    def get_work_items(self, session_key: str = "default") -> list[dict[str, Any]]:
        """Get work items for a session."""
        results = self.conn.execute(
            """
            SELECT id, task_slug, status, contract_slug
            FROM work_items WHERE session_key = ?
            ORDER BY id
            """,
            [session_key],
        ).fetchall()
        return [
            {
                "id": r[0],
                "task_slug": r[1],
                "status": r[2],
                "contract_slug": r[3],
            }
            for r in results
        ]

    def close(self) -> None:
        """Close the connection."""
        self.conn.close()
