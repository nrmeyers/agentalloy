"""Tests for ``src/agentalloy/api/state_client.py`` and CLI routing.

Verifies the check-then-route pattern:
  - When the state service is running, CLI verbs route through HTTP.
  - When the state service is down, CLI verbs fall back to file-mirror writes.
  - Clear error messages when both paths fail.
  - No regression when the service is down (file-mirror path).
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


class TestPhaseSetRouting:
    """phase set routes through HTTP when service is up, falls back to file."""

    def test_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        """When the service is down, phase set writes the file directly."""
        from agentalloy.install.subcommands.phase import run_phase_set

        result = run_phase_set("build", root=tmp_path)
        assert result["phase"] == "build"
        assert result["blocked"] is False
        phase_file = tmp_path / ".agentalloy" / "phase"
        assert phase_file.exists()
        assert "phase: build" in phase_file.read_text()

    def test_returns_service_result_when_up(self, tmp_path: Path) -> None:
        """When the service is up, phase set returns the service's response."""
        from agentalloy.install.subcommands.phase import run_phase_set

        service_response = {"phase": "build"}

        with patch.object(StateClient, "is_running", return_value=True):
            with patch.object(
                StateClient,
                "set_phase",
                return_value=service_response,
            ) as mock_post:
                result = run_phase_set("build", root=tmp_path)
                assert result == service_response
                mock_post.assert_called_once_with("build")

    def test_clears_phase_file_on_set(self, tmp_path: Path) -> None:
        """phase clear still works when service is down (file-mirror path)."""
        from agentalloy.install.subcommands.phase import run_phase_clear, run_phase_set

        run_phase_set("build", root=tmp_path)
        result = run_phase_clear(root=tmp_path)
        assert result["message"] == "Phase cleared"
        assert not (tmp_path / ".agentalloy" / "phase").exists()


# ---------------------------------------------------------------------------
# CLI routing: approve
# ---------------------------------------------------------------------------


class TestApproveRouting:
    def test_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        """When the service is down, approve writes the marker file directly."""
        from agentalloy.install.subcommands.approve import run_approve
        from agentalloy.install.subcommands.phase import run_phase_set

        # Create a spec doc so the exit artifact check passes
        spec = tmp_path / "docs" / "spec"
        spec.mkdir(parents=True)
        (spec / "x.md").write_text("# spec\n## Acceptance Criteria\n- a\n## Out of Scope\n- b\n")

        run_phase_set("spec", root=tmp_path)
        result = run_approve("spec", root=tmp_path, approver="test")
        assert result["ok"] is True
        marker = tmp_path / ".agentalloy" / "approved" / "spec"
        assert marker.exists()
        assert "approver: test" in marker.read_text()

    def test_returns_service_result_when_up(self, tmp_path: Path) -> None:
        """When the service is up, approve returns the service's response."""
        from agentalloy.install.subcommands.approve import run_approve

        service_response = {"ok": True, "phase": "design"}

        with patch.object(StateClient, "is_running", return_value=True):
            with patch.object(
                StateClient,
                "approve",
                return_value=service_response,
            ) as mock_post:
                result = run_approve("spec", root=tmp_path)
                assert result == service_response
                mock_post.assert_called_once_with("spec")


# ---------------------------------------------------------------------------
# CLI routing: task next / start / status
# ---------------------------------------------------------------------------


class TestTaskRouting:
    @staticmethod
    def _seed(root: Path, phase: str, names: list[str]) -> None:
        (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
        (root / ".agentalloy" / "phase").write_text(f"phase: {phase}\n")
        d = root / ".agentalloy" / "contracts" / "active" / phase
        d.mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / f"{n}.md").write_text(f"---\nphase: {phase}\n---\n# {n}\n")

    def test_task_next_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
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

    def test_task_start_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.task import run_task_start

        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        result = run_task_start("02-api", tmp_path)
        assert result["ok"] is True
        assert result["cursor"] == "active/build/02-api.md"

    def test_task_status_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.task import run_task_start, run_task_status

        TestTaskRouting._seed(tmp_path, "build", ["01-cache", "02-api"])
        run_task_start("01-cache", tmp_path)
        result = run_task_status(tmp_path)
        assert result["ok"] is True
        assert result["cursor"] == "active/build/01-cache.md"


# ---------------------------------------------------------------------------
# CLI routing: flow free / resume / status
# ---------------------------------------------------------------------------


class TestFlowRouting:
    def test_flow_free_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.flow import run_flow_free
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("build", root=tmp_path, force=True)
        result = run_flow_free(root=tmp_path)
        assert result["changed"] is True
        assert result["mode"] == "free"
        assert result["phase"] == "build"

    def test_flow_resume_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.flow import run_flow_free, run_flow_resume
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("build", root=tmp_path, force=True)
        run_flow_free(root=tmp_path)
        result = run_flow_resume(root=tmp_path)
        assert result["changed"] is True
        assert result["mode"] == "workflow"
        assert result["phase"] == "build"

    def test_flow_status_falls_back_to_file_when_service_down(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands.flow import run_flow_status
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("design", root=tmp_path, force=True)
        result = run_flow_status(root=tmp_path)
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
        from agentalloy.install.subcommands.task import run_task_start

        # Manually create phase file without contracts
        (tmp_path / ".agentalloy").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".agentalloy" / "phase").write_text("phase: build\n")

        result = run_task_start("nonexistent", tmp_path)
        assert result["ok"] is False
        assert "No contract matching" in result["message"]
