"""Session management: stash/resume/archive/cancel for crash-resumable workflows (AC-11).

Sessions capture the full workflow state (phase, contracts, approvals, cursor)
so that a workflow can survive a process kill and resume from the exact position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentalloy.state_store import StateStore


@dataclass
class SessionInfo:
    """Summary of a session."""

    session_key: str
    status: str
    phase: str
    created_at: str | None = None
    updated_at: str | None = None


class SessionManager:
    """High-level session management over StateStore."""

    def __init__(self, state_store: StateStore) -> None:
        self.store = state_store

    def create(self, session_key: str) -> SessionInfo:
        """Create a new session."""
        self.store.create_session(session_key)
        return self._get_info(session_key)

    def stash(self, session_key: str) -> dict[str, Any] | None:
        """Stash a session — snapshot state for later resume.

        Returns the snapshot dict, or None if the session wasn't active.
        """
        return self.store.stash_session(session_key)

    def resume(self, session_key: str) -> dict[str, Any] | None:
        """Resume a stashed session — restore state and continue.

        Returns the restored state dict, or None if not found/not stashed.
        """
        return self.store.resume_session(session_key)

    def archive(self, session_key: str) -> bool:
        """Archive a completed session."""
        return self.store.archive_session(session_key)

    def cancel(self, session_key: str) -> bool:
        """Cancel a session."""
        return self.store.cancel_session(session_key)

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions."""
        sessions = self.store.list_sessions()
        return [
            SessionInfo(
                session_key=s["session_key"],
                status=s["status"],
                phase=s["phase"] or "spec",
                created_at=s.get("created_at"),
                updated_at=s.get("updated_at"),
            )
            for s in sessions
        ]

    def get(self, session_key: str) -> SessionInfo | None:
        """Get session details."""
        session = self.store.get_session(session_key)
        if session is None:
            return None
        return SessionInfo(
            session_key=session["session_key"],
            status=session["status"],
            phase=session["phase"] or "spec",
            created_at=session.get("created_at"),
            updated_at=session.get("updated_at"),
        )

    def status(self, session_key: str) -> dict[str, Any]:
        """Get full session status including snapshot and cursor."""
        session = self.store.get_session(session_key)
        if session is None:
            return {"error": f"session '{session_key}' not found"}

        work_items = self.store.get_work_items(session_key)
        return {
            "session_key": session["session_key"],
            "status": session["status"],
            "phase": session["phase"],
            "snapshot": session.get("snapshot"),
            "cursor": session.get("cursor"),
            "work_items": work_items,
        }

    def add_work_item(
        self,
        task_slug: str,
        session_key: str = "default",
        contract_slug: str | None = None,
    ) -> int:
        """Add a work item to a session's cursor."""
        return self.store.add_work_item(task_slug, session_key, contract_slug)

    def advance_cursor(self, item_id: int, status: str = "done") -> None:
        """Advance the work-item cursor (mark item as done/in_progress)."""
        self.store.update_work_item(item_id, status)

    def get_cursor(self, session_key: str = "default") -> list[dict[str, Any]]:
        """Get the work-item cursor for a session."""
        return self.store.get_work_items(session_key)

    def _get_info(self, session_key: str) -> SessionInfo:
        """Get session info (assumes session exists)."""
        sessions = self.store.list_sessions()
        for s in sessions:
            if s["session_key"] == session_key:
                return SessionInfo(
                    session_key=s["session_key"],
                    status=s["status"],
                    phase=s["phase"] or "spec",
                    created_at=s.get("created_at"),
                    updated_at=s.get("updated_at"),
                )
        return SessionInfo(session_key=session_key, status="unknown", phase="spec")
