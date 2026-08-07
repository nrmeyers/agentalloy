"""Tests for ``src/agentalloy/api/state_client.py`` and CLI routing.

Verifies the transport seam and the CLI's fail-loud posture:
  - ``StateClient`` itself: reachability, reads, and writes over HTTP.
  - Phase verbs persist to the state store and write no repo file — the
    file-mirror fallback these tests used to assert is gone (slice 06).
  - The cursor is still a repo file, so ``task`` keeps its own HTTP routing.
  - Clear error messages instead of a silent second source of truth.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.api.state_client import StateClient, StateClientError

# ---------------------------------------------------------------------------
# Helpers: a minimal HTTP server that mimics the state service
# ---------------------------------------------------------------------------


def _start_fake_service(
    port: int,
    health_handler: str = "200\n",
    phase_handler: str = '{"phase": "build"}',
    approved_handler: str = '{"ok": true}',
    cursor_handler: str = "active/build/01-task.md",
) -> tuple[threading.Thread, list[str]]:
    """Start a tiny HTTP server in a background thread that responds to /health
    and /state/* paths.  Returns (thread, request_log)."""
    request_log: list[str] = []

    def _handler(conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        # Parse method + path from headers
        first_line = buf.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first_line.split(" ")
        if len(parts) >= 2:
            request_log.append(f"{parts[0]} {parts[1]}")

        path = parts[1] if len(parts) >= 2 else ""

        # Read the POST body if present (Content-Length header)
        content_length = 0
        for header_line in buf.split(b"\r\n"):
            header_lower = header_line.lower()
            if header_lower.startswith(b"content-length:"):
                with contextlib.suppress(ValueError, IndexError):
                    content_length = int(header_lower.split(b":")[1].strip())
                break
        if content_length > 0:
            body_start = buf.find(b"\r\n\r\n")
            remaining = content_length - (body_start + 4) if body_start >= 0 else content_length
            while remaining > 0:
                chunk = conn.recv(min(remaining, 4096))
                if not chunk:
                    break
                remaining -= len(chunk)

        if "/health" in path:
            body = health_handler
        elif "/state/phase" in path:
            body = phase_handler
        elif "/state/approve" in path:
            body = approved_handler
        elif "/state/cursor" in path:
            body = cursor_handler
        elif "/state/" in path:
            body = "phase: build\n"
        else:
            body = "ok"

        body_bytes = body.encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body_bytes)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body_bytes
        )
        conn.sendall(response)
        conn.close()

    def _serve() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(5)
        srv.settimeout(5.0)
        try:
            while True:
                try:
                    conn, _ = srv.accept()
                    t = threading.Thread(target=_handler, args=(conn,), daemon=True)
                    t.start()
                except TimeoutError:
                    continue
        finally:
            srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    # Wait until the server actually accepts connections
    for _ in range(20):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            break
        except OSError:
            time.sleep(0.1)
    return t, request_log


# ---------------------------------------------------------------------------
# StateClient: is_running
# ---------------------------------------------------------------------------


class TestStateClientIsRunning:
    """is_running() returns False when no service is listening."""

    def test_returns_false_when_no_service(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19999")
        assert client.is_running() is False

    def test_returns_true_when_service_is_up(self) -> None:
        thread, _ = _start_fake_service(19998)
        try:
            client = StateClient(base_url="http://127.0.0.1:19998")
            assert client.is_running() is True
        finally:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# StateClient: write operations
# ---------------------------------------------------------------------------


class TestStateClientWrites:
    """set_phase, approve, set_cursor raise StateClientError when down."""

    def test_set_phase_raises_when_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19997")
        with pytest.raises(StateClientError):
            client.set_phase("build")

    def test_approve_raises_when_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19996")
        with pytest.raises(StateClientError):
            client.approve("spec")

    def test_set_cursor_raises_when_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19995")
        with pytest.raises(StateClientError):
            client.set_cursor("active/build/01.md")

    def test_set_phase_success_when_up(self) -> None:
        thread, log = _start_fake_service(19994)
        try:
            client = StateClient(base_url="http://127.0.0.1:19994")
            result = client.set_phase("design")
            assert result == {"phase": "build"}
            assert any("POST /state/phase" in r for r in log)
        finally:
            thread.join(timeout=2.0)

    def test_approve_success_when_up(self) -> None:
        thread, log = _start_fake_service(19993, approved_handler='{"ok": true, "phase": "design"}')
        try:
            client = StateClient(base_url="http://127.0.0.1:19993")
            result = client.approve("spec")
            assert result == {"ok": True, "phase": "design"}
            assert any("POST /state/approve" in r for r in log)
        finally:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# StateClient: read operations
# ---------------------------------------------------------------------------


class TestStateClientReads:
    def test_get_state_returns_none_when_down(self) -> None:
        client = StateClient(base_url="http://127.0.0.1:19992")
        assert client.get_state("phase") is None

    def test_get_state_returns_value_when_up(self) -> None:
        thread, log = _start_fake_service(19991, cursor_handler="active/build/01.md")
        try:
            client = StateClient(base_url="http://127.0.0.1:19991")
            result = client.get_state("cursor")
            assert result == "active/build/01.md"
            assert any("GET /state/cursor" in r for r in log)
        finally:
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# CLI routing: phase set
# ---------------------------------------------------------------------------


class TestPhaseSetPersistence:
    """``phase set`` has exactly one destination: the state store.

    These were the file-mirror tests.  They asserted that a phase set without a
    reachable service wrote ``.agentalloy/phase`` — the behaviour that let the
    CLI and the service disagree about which phase a repo was in.  They now
    assert the opposite: the row is the record, and the repo stays clean.
    """

    def test_writes_the_row_and_no_file(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands._state import phase_access
        from agentalloy.install.subcommands.phase import run_phase_set

        result = run_phase_set("build", root=tmp_path)

        assert result["phase"] == "build"
        assert result["blocked"] is False
        state = phase_access(tmp_path).read()
        assert state is not None and state.phase == "build"
        assert not (tmp_path / ".agentalloy" / "phase").exists()

    def test_repo_scoping_keeps_two_repos_apart(self, tmp_path: Path) -> None:
        """The row carries repo identity, so one store serves many repos."""
        from agentalloy.install.subcommands._state import phase_access
        from agentalloy.install.subcommands.phase import run_phase_set

        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        run_phase_set("build", root=a)
        run_phase_set("qa", root=b)

        assert phase_access(a).read().phase == "build"  # pyright: ignore[reportOptionalMemberAccess]
        assert phase_access(b).read().phase == "qa"  # pyright: ignore[reportOptionalMemberAccess]

    def test_clear_removes_the_row(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands._state import phase_access
        from agentalloy.install.subcommands.phase import run_phase_clear, run_phase_set

        run_phase_set("build", root=tmp_path)
        result = run_phase_clear(root=tmp_path)
        assert result["message"] == "Phase cleared"
        assert phase_access(tmp_path).read() is None


# ---------------------------------------------------------------------------
# CLI routing: approve
# ---------------------------------------------------------------------------


class TestApproveRouting:
    def test_records_the_marker_in_the_store(self, tmp_path: Path) -> None:
        """The approval marker is a store row now (spec/design migrated); only
        the transport moved, not the fact that something durable is recorded."""
        from agentalloy.install.subcommands._state import phase_access
        from agentalloy.install.subcommands.approve import run_approve
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("spec", root=tmp_path)
        handle = phase_access(tmp_path).contracts_handle()
        handle.set_artifact(
            "spec", "x", "spec.md", "# spec\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
        )
        result = run_approve("spec", root=tmp_path, approver="test")
        assert result["ok"] is True
        assert result["marker"] == "state store (approved/spec)"
        approval = phase_access(tmp_path).contracts_handle().get_approval("spec")
        assert approval is not None
        assert "artifact_digest" in approval

    def test_advance_lands_in_the_store(self, tmp_path: Path) -> None:
        """Approving the current phase advances it — in the store, not a file.

        ``run_approve`` used to hand the whole verb to ``StateClient.approve``
        whenever the service answered.  It now reads and writes the phase
        through the shared seam, so the advance is observable in one place.
        """
        from agentalloy.install.subcommands._state import phase_access
        from agentalloy.install.subcommands.approve import run_approve
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("spec", root=tmp_path)
        handle = phase_access(tmp_path).contracts_handle()
        handle.set_artifact(
            "spec", "x", "spec.md", "# spec\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n"
        )

        result = run_approve("spec", root=tmp_path, approver="test")

        assert result["ok"] is True
        state = phase_access(tmp_path).read()
        assert state is not None and state.phase == "design"
        assert not (tmp_path / ".agentalloy" / "phase").exists()


# ---------------------------------------------------------------------------
# CLI routing: task next / start / status
# ---------------------------------------------------------------------------


class TestTaskRouting:
    @staticmethod
    def _seed(root: Path, phase: str, names: list[str]) -> None:
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set(phase, root=root, force=True)
        d = root / ".agentalloy" / "contracts" / "active" / phase
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / f"{n}.md").write_text(f"---\nphase: {phase}\n---\n# {n}\n")

    def test_task_next_writes_the_cursor_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.task import run_task_next

        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        result = run_task_next(tmp_path)
        assert result["ok"] is True
        assert result["cursor"] == "active/build/01-cache.md"
        # Verify file was written
        cursor_file = tmp_path / ".agentalloy" / "cursor"
        assert cursor_file.exists()

    def test_task_next_returns_service_result_when_up(self, tmp_path: Path) -> None:
        """When the service is up, task next routes through HTTP."""
        from agentalloy.install.subcommands.task import run_task_next

        # Seed two contracts and set cursor to the first so "next" advances to
        # the second — this exercises the index+1 path.
        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        cursor_file = tmp_path / ".agentalloy" / "cursor"
        cursor_file.write_text("active/build/01-cache.md", encoding="utf-8")

        with patch.object(StateClient, "is_running", return_value=True):
            with patch.object(StateClient, "set_cursor") as mock_set_cursor:
                mock_set_cursor.return_value = {"cursor": "active/build/02-api.md"}
                result = run_task_next(tmp_path)
                assert result["ok"] is True
                assert result["cursor"] == "active/build/02-api.md"
                mock_set_cursor.assert_called_once_with("active/build/02-api.md")

    def test_task_start_resolves_a_named_contract(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.task import run_task_start

        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        result = run_task_start("02-api", tmp_path)
        assert result["ok"] is True
        assert result["cursor"] == "active/build/02-api.md"

    def test_task_status_reports_the_started_contract(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.task import run_task_start, run_task_status

        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        run_task_start("01-cache", tmp_path)
        result = run_task_status(tmp_path)
        assert result["ok"] is True
        assert result["cursor"] == "active/build/01-cache.md"


# ---------------------------------------------------------------------------
# CLI routing: workflow pause / resume / status
# ---------------------------------------------------------------------------


class TestFlowRouting:
    def test_workflow_pause_falls_back_to_store_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.phase import run_phase_set
        from agentalloy.install.subcommands.workflow import run_workflow_pause

        run_phase_set("build", root=tmp_path, force=True)
        result = run_workflow_pause(root=tmp_path)
        assert result["changed"] is True
        assert result["mode"] == "paused"
        assert result["phase"] == "build"

    def test_workflow_resume_falls_back_to_store_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.phase import run_phase_set
        from agentalloy.install.subcommands.workflow import run_workflow_pause, run_workflow_resume

        run_phase_set("build", root=tmp_path, force=True)
        run_workflow_pause(root=tmp_path)
        result = run_workflow_resume(root=tmp_path)
        assert result["changed"] is True
        assert result["mode"] == "workflow"
        assert result["phase"] == "build"

    def test_workflow_status_falls_back_to_store_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.phase import run_phase_set
        from agentalloy.install.subcommands.workflow import run_workflow_status

        run_phase_set("design", root=tmp_path, force=True)
        result = run_workflow_status(root=tmp_path)
        assert result["mode"] == "workflow"
        assert result["phase"] == "design"


# ---------------------------------------------------------------------------
# Error handling: clear messages when both paths fail
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_phase_set_invalid_phase_fails_clearly(self, tmp_path: Path) -> None:
        """Invalid phase returns a clear error even when service is down."""
        from agentalloy.install.subcommands.phase import run_phase_set

        with pytest.raises(SystemExit):
            run_phase_set("invalid_phase", root=tmp_path)

    def test_task_next_no_phase_fails_clearly(self, tmp_path: Path) -> None:
        """No active phase returns a clear error."""
        from agentalloy.install.subcommands.task import run_task_next

        result = run_task_next(tmp_path)
        assert result["ok"] is False
        assert "No active phase" in result["message"]

    def test_task_start_no_match_fails_clearly(self, tmp_path: Path) -> None:
        """No matching contract returns a clear error."""
        # A phase, but no contracts under it.
        from agentalloy.install.subcommands.phase import run_phase_set
        from agentalloy.install.subcommands.task import run_task_start

        run_phase_set("build", root=tmp_path, force=True)

        result = run_task_start("nonexistent", tmp_path)
        assert result["ok"] is False
        assert "No contract matching" in result["message"]
