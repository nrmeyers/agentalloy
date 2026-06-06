# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for fast-start entrypoint generation and readiness polling.

Covers UT-9..UT-25 from docs/tests/container-setup-improvements.md and
IT-2 (bash syntax check) and EC-12/EC-13 (no-packs path).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install.subcommands import container_runtime as cr

_build_entrypoint_script = cr._build_entrypoint_script  # pyright: ignore[reportPrivateUsage]
_wait_for_readiness = cr._wait_for_readiness  # pyright: ignore[reportPrivateUsage]
_get_bootstrap_progress = cr._get_bootstrap_progress  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Entrypoint generation — UT-9..UT-16, EC-12, EC-13, IT-2
# ---------------------------------------------------------------------------


class TestEntrypointScript:
    def test_ut9_creates_lock_at_start(self) -> None:
        script = _build_entrypoint_script("python,nodejs")
        # ISO timestamp written into lock file
        assert 'date -Iseconds > "$LOCK"' in script
        assert 'LOCK="$APP_DIR/.bootstrap-lock"' in script

    def test_ut10_uvicorn_starts_after_pack_ingest(self) -> None:
        script = _build_entrypoint_script("python,nodejs")
        uvicorn_idx = script.find("uvicorn agentalloy.app:app")
        # Pack ingest happens inside the per-pack loop, identified by
        # "Installing pack:"
        ingest_idx = script.find("Installing pack")
        assert uvicorn_idx != -1 and ingest_idx != -1, script
        assert uvicorn_idx > ingest_idx, (
            "uvicorn must start after pack ingest (moved after bootstrap)"
        )
        # Uvicorn launched in background, not exec'd.
        assert (
            "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &"
            in script
        )
        assert "UVICORN_PID=$!" in script

    def test_ut11_progress_writes_are_atomic(self) -> None:
        script = _build_entrypoint_script("python")
        # Stage to .tmp, then mv onto target.
        assert 'PROGRESS_TMP="$APP_DIR/.bootstrap-progress.tmp"' in script
        assert 'mv "$PROGRESS_TMP" "$PROGRESS"' in script

    def test_ut12_removes_lock_and_creates_complete(self) -> None:
        script = _build_entrypoint_script("python")
        assert 'rm -f "$LOCK"' in script
        assert 'touch "$COMPLETE"' in script
        # And the complete-file path is the documented one.
        assert 'COMPLETE="$APP_DIR/.bootstrap-complete"' in script

    def test_ut13_writes_checkpoint_after_each_pack(self) -> None:
        script = _build_entrypoint_script("python,nodejs")
        # Per-pack checkpoint append with JSON shape.
        assert "pack_ingested" in script
        assert '>> "$CHECKPOINTS"' in script

    def test_ut14_detects_stale_lock_on_restart(self) -> None:
        script = _build_entrypoint_script("python")
        # 7200s == 2 hours
        assert "7200" in script
        assert "Stale bootstrap lock detected" in script
        # Stale lock recovery wipes lock + checkpoints to start fresh.
        assert 'rm -f "$LOCK" "$CHECKPOINTS"' in script

    def test_ut15_reads_checkpoints_on_restart(self) -> None:
        script = _build_entrypoint_script("python,nodejs")
        assert "pack_already_done" in script
        assert "grep -Fq" in script
        assert "already ingested - skipping" in script

    def test_ut16_corrupt_checkpoints_treated_as_none(self) -> None:
        script = _build_entrypoint_script("python")
        # `|| INGESTED=0` swallows grep failures; pack_already_done returns
        # non-zero on no match so a corrupt file simply re-runs packs.
        assert ") || INGESTED=0" in script

    def test_nonempty_packs_uses_per_pack_loop(self) -> None:
        """T2: non-empty packs generates the per-pack loop with correct structure
        (REQ-4, C04)."""
        script = _build_entrypoint_script("core,documentation")
        assert "PACK_LIST=(core documentation)" in script
        assert "TOTAL=2" in script
        assert "for pack in" in script
        assert "--non-interactive" not in script
        assert "Installing pack:" in script

    def test_empty_packs_installs_always_on(self) -> None:
        """T1: empty packs triggers install-packs instead of skipping (REQ-3, C03)."""
        script = _build_entrypoint_script("")
        assert "agentalloy install-packs --non-interactive --no-restart" in script
        assert "No packs specified - skipping pack installation" not in script
        assert "Installing always-on packs..." in script

    def test_empty_packs_no_per_pack_loop(self) -> None:
        """T1: empty packs must not generate the per-pack loop."""
        script = _build_entrypoint_script("")
        assert "PACK_LIST=" not in script
        assert "for pack in" not in script
        assert "Installing pack:" not in script

    def test_empty_packs_branch_has_both_echo_and_command(self) -> None:
        """T1: the echo must appear before the install-packs command in the script."""
        script = _build_entrypoint_script("")
        echo_pos = script.find("Installing always-on packs...")
        cmd_pos = script.find("agentalloy install-packs --non-interactive --no-restart")
        assert echo_pos != -1 and cmd_pos != -1, "Both echo and command must be present"
        assert echo_pos < cmd_pos, "Echo must appear before the install-packs command"

    def test_ec12_ec13_no_packs_path(self) -> None:
        # Still wires uvicorn + complete marker even with no packs.
        script = _build_entrypoint_script("")
        assert "uvicorn agentalloy.app:app" in script
        assert 'touch "$COMPLETE"' in script

    def test_it2_script_passes_bash_syntax_check(self) -> None:
        if shutil.which("bash") is None:
            pytest.skip("bash not on PATH")
        for packs in ("", "python", "python,nodejs,rust"):
            script = _build_entrypoint_script(packs)
            result = subprocess.run(
                ["bash", "-n", "/dev/stdin"],
                input=script.encode(),
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0, (
                f"bash -n failed for packs={packs!r}: "
                f"{result.stderr.decode(errors='replace')}\n---\n{script}"
            )


# ---------------------------------------------------------------------------
# T3 — Uvicorn starts AFTER bootstrap complete (REQ-4, C04, C05)
# ---------------------------------------------------------------------------


class TestUvicornAfterBootstrap:
    def test_uvicorn_after_bootstrap_complete(self) -> None:
        """Uvicorn starts AFTER touch $COMPLETE (bootstrap complete marker)."""
        script = _build_entrypoint_script("")
        lines = script.split("\n")
        complete_idx = next(i for i, line in enumerate(lines) if 'touch "$COMPLETE"' in line)
        uvicorn_idx = next(i for i, line in enumerate(lines) if "uv run uvicorn" in line)
        assert uvicorn_idx > complete_idx, (
            f"uvicorn line {uvicorn_idx} must be after touch $COMPLETE line {complete_idx}"
        )
        # uvicorn must be OUTSIDE the if [ "$BOOTSTRAP_NEEDED" = "true" ] block,
        # i.e. after the closing fi.
        _bootstrap_if_idx = next(
            i for i, line in enumerate(lines) if 'if [ "$BOOTSTRAP_NEEDED" = "true" ]' in line
        )
        # Find the fi that closes this if — it's the fi at the same indent level
        # that appears after the complete marker.
        fi_idx = None
        for i in range(complete_idx + 1, len(lines)):
            if lines[i].strip() == "fi":
                fi_idx = i
                break
        assert fi_idx is not None, "Could not find closing fi for bootstrap block"
        assert uvicorn_idx > fi_idx, (
            f"uvicorn line {uvicorn_idx} must be after the closing fi at line {fi_idx}"
        )

    def test_uvicorn_not_before_pack_install(self) -> None:
        """Uvicorn starts AFTER pack install commands, not before."""
        script = _build_entrypoint_script("core")
        lines = script.split("\n")
        install_idx = next(
            i for i, line in enumerate(lines) if "agentalloy install-packs --packs" in line
        )
        uvicorn_idx = next(i for i, line in enumerate(lines) if "uv run uvicorn" in line)
        assert uvicorn_idx > install_idx, (
            f"uvicorn line {uvicorn_idx} must be after install-packs line {install_idx}"
        )

    def test_uvicorn_after_migrations(self) -> None:
        """Uvicorn starts AFTER the migrations command."""
        script = _build_entrypoint_script("")
        lines = script.split("\n")
        migrate_idx = next(i for i, line in enumerate(lines) if "agentalloy.migrate" in line)
        uvicorn_idx = next(i for i, line in enumerate(lines) if "uv run uvicorn" in line)
        assert uvicorn_idx > migrate_idx, (
            f"uvicorn line {uvicorn_idx} must be after migrate line {migrate_idx}"
        )

    def test_uvicorn_start_comment_updated(self) -> None:
        """Comment reflects the new ordering — no 'fast-start' or 'before pack ingest'."""
        script = _build_entrypoint_script("")
        assert "# --- Start uvicorn AFTER all bootstrap steps" in script, (
            "Missing new uvicorn start comment"
        )
        assert "# --- Fast-start uvicorn" not in script, "Old 'fast-start' comment must be removed"
        assert "Start uvicorn BEFORE pack ingest" not in script, (
            "Old 'before pack ingest' comment must be removed"
        )


# ---------------------------------------------------------------------------
# _wait_for_readiness — UT-17..UT-22
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class TestWaitForReadiness:
    def test_ut17_returns_true_on_ready(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResp({"status": "ready"})):
            assert _wait_for_readiness(47950, timeout=5, poll_interval=0.01) is True

    def test_ut18_returns_false_on_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeResp({"status": "error", "progress": {"error": "stale_lock"}}),
        ):
            assert _wait_for_readiness(47950, timeout=5, poll_interval=0.01) is False

    def test_ut19_continues_on_warming_up_then_ready(self) -> None:
        responses = [
            _FakeResp(
                {"status": "warming_up", "progress": {"packs_ingested": 1, "packs_total": 3}}
            ),
            _FakeResp(
                {"status": "warming_up", "progress": {"packs_ingested": 2, "packs_total": 3}}
            ),
            _FakeResp({"status": "ready"}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            assert _wait_for_readiness(47950, timeout=10, poll_interval=0.01) is True

    def test_ut20_fails_on_repeated_connection_errors(self) -> None:
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            # Tight timeout so the grace window expires fast.
            assert _wait_for_readiness(47950, timeout=2, poll_interval=0.01) is False

    def test_ut21_timeout_1800_accepted(self) -> None:
        # Argument plumbing only — patch urlopen so we never sleep the full timeout.
        with patch("urllib.request.urlopen", return_value=_FakeResp({"status": "ready"})):
            assert _wait_for_readiness(47950, timeout=1800, poll_interval=0.01) is True

    def test_ut22_timeout_300_accepted(self) -> None:
        with patch("urllib.request.urlopen", return_value=_FakeResp({"status": "ready"})):
            assert _wait_for_readiness(47950, timeout=300, poll_interval=0.01) is True

    def test_on_progress_callback_invoked(self) -> None:
        seen: list[dict] = []
        responses = [
            _FakeResp(
                {"status": "warming_up", "progress": {"packs_ingested": 1, "packs_total": 2}}
            ),
            _FakeResp({"status": "ready"}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            ok = _wait_for_readiness(
                47950,
                timeout=10,
                poll_interval=0.01,
                on_progress=lambda evt: seen.append(evt),
            )
        assert ok is True
        # At least one warming_up event and one ready event.
        statuses = [e["status"] for e in seen]
        assert "warming_up" in statuses
        assert "ready" in statuses


# ---------------------------------------------------------------------------
# _get_bootstrap_progress — UT-23..UT-25
# ---------------------------------------------------------------------------


class TestGetBootstrapProgress:
    def test_ut23_returns_parsed_json(self) -> None:
        progress = {"current_pack": "python", "packs_ingested": 1, "packs_total": 3}
        fake_completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(progress).encode(),
            stderr=b"",
        )
        with patch("subprocess.run", return_value=fake_completed):
            result = _get_bootstrap_progress("podman", "agentalloy")
        assert result == progress

    def test_ut24_returns_empty_dict_on_subprocess_failure(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["podman"]),
        ):
            assert _get_bootstrap_progress("podman", "agentalloy") == {}

    def test_ut24_returns_empty_dict_on_malformed_json(self) -> None:
        fake_completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"not json", stderr=b""
        )
        with patch("subprocess.run", return_value=fake_completed):
            assert _get_bootstrap_progress("podman", "agentalloy") == {}

    def test_ut25_uses_detected_runtime_binary(self) -> None:
        seen_args: list[list[str]] = []

        def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
            seen_args.append(args)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"{}", stderr=b"")

        with patch("subprocess.run", side_effect=fake_run):
            _get_bootstrap_progress("docker", "agentalloy")
        assert seen_args[0][0] == "docker"
        assert "exec" in seen_args[0]
        assert "agentalloy" in seen_args[0]


# ---------------------------------------------------------------------------
# T4 — SIGTERM trap position + lock/complete marker positioning (REQ-5, C05)
# ---------------------------------------------------------------------------


class TestSigtermTrapAndMarkerPosition:
    def test_sigterm_trap_before_uvicorn(self) -> None:
        """SIGTERM trap fires before uvicorn PID assignment (REQ-5, C05)."""
        script = _build_entrypoint_script("")
        lines = script.split("\n")
        trap_idx = next(i for i, line in enumerate(lines) if "trap 'kill" in line)
        uvicorn_pid_idx = next(i for i, line in enumerate(lines) if "UVICORN_PID=$!" in line)
        assert trap_idx < uvicorn_pid_idx, (
            f"trap line {trap_idx} must be before UVICORN_PID line {uvicorn_pid_idx}"
        )

    def test_no_packs_complete_marker_written(self) -> None:
        """touch $COMPLETE is inside the bootstrap block (REQ-5, C05)."""
        script = _build_entrypoint_script("")
        lines = script.split("\n")
        bootstrap_if_idx = next(
            i for i, line in enumerate(lines) if 'if [ "$BOOTSTRAP_NEEDED" = "true" ]' in line
        )
        complete_idx = next(i for i, line in enumerate(lines) if 'touch "$COMPLETE"' in line)
        assert complete_idx > bootstrap_if_idx, (
            f"touch $COMPLETE line {complete_idx} must be after bootstrap if at line {bootstrap_if_idx}"
        )
        # Must be before the closing fi of the bootstrap block.
        fi_idx = None
        for i in range(complete_idx + 1, len(lines)):
            if lines[i].strip() == "fi":
                fi_idx = i
                break
        assert fi_idx is not None, "Could not find closing fi for bootstrap block"
        assert complete_idx < fi_idx, (
            f"touch $COMPLETE line {complete_idx} must be before closing fi at line {fi_idx}"
        )

    def test_no_packs_lock_cleared(self) -> None:
        """rm -f $LOCK is inside the bootstrap block, before touch $COMPLETE (REQ-5, C05)."""
        script = _build_entrypoint_script("")
        lines = script.split("\n")
        bootstrap_if_idx = next(
            i for i, line in enumerate(lines) if 'if [ "$BOOTSTRAP_NEEDED" = "true" ]' in line
        )
        lock_rm_idx = next(i for i, line in enumerate(lines) if line.strip() == 'rm -f "$LOCK"')
        complete_idx = next(i for i, line in enumerate(lines) if 'touch "$COMPLETE"' in line)
        assert lock_rm_idx > bootstrap_if_idx, (
            f"rm -f $LOCK line {lock_rm_idx} must be after bootstrap if at line {bootstrap_if_idx}"
        )
        assert lock_rm_idx < complete_idx, (
            f"rm -f $LOCK line {lock_rm_idx} must be before touch $COMPLETE line {complete_idx}"
        )

    def test_sigterm_traps_both_pids(self) -> None:
        """Trap covers both OLLAMA_PID and UVICORN_PID (REQ-5, C05)."""
        script = _build_entrypoint_script("")
        assert "kill ${OLLAMA_PID:-} ${UVICORN_PID:-}" in script


# ---------------------------------------------------------------------------
# T7 — Integration Tests: Script Execution (IT-1 through IT-6)
# ---------------------------------------------------------------------------


class TestIntegrationTests:
    """IT-1 through IT-6: Execute the generated bash script in a controlled
    environment with mocked binaries to verify actual script behavior."""

    @staticmethod
    def _make_mock_path(tmp_path: Path) -> str:
        """Create a temporary directory with stub binaries that exit 0."""
        mock_dir = tmp_path / "mock_bin"
        mock_dir.mkdir()
        for name in ("ollama", "curl", "uv", "agentalloy", "uvicorn"):
            script_path = mock_dir / name
            script_path.write_text("#!/bin/sh\nexit 0\n")
            script_path.chmod(0o755)
        return str(mock_dir)

    def _make_script_and_env(
        self,
        packs: str,
        tmp_path: Path,
        bootstrap_complete: bool = False,
    ) -> tuple[str, str, Path]:
        """Generate script, write to temp file, set up mock PATH.

        Returns (script_path, mock_dir, app_dir).
        """
        script = _build_entrypoint_script(packs)
        script_path = tmp_path / "entrypoint.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)
        mock_dir = self._make_mock_path(tmp_path)
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        if bootstrap_complete:
            (app_dir / ".bootstrap-complete").touch()
        return str(script_path), mock_dir, app_dir

    def test_it1_script_executes_cleanly(self, tmp_path: Path) -> None:
        """IT-1: Generated script executes without syntax errors (exits 0).

        The script runs with mocked binaries that all exit 0.
        Since .bootstrap-complete doesn't exist, bootstrap runs fully
        (with mocked ollama, curl, uv, agentalloy, uvicorn).
        """
        script_path, mock_dir, _ = self._make_script_and_env("", tmp_path)
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(tmp_path / "app")
        (tmp_path / "app").mkdir(exist_ok=True)
        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )

    def test_it2_no_uvicorn_during_bootstrap(self, tmp_path: Path) -> None:
        """IT-2: Bootstrap completes without uvicorn running during pack install.

        After bootstrap, .bootstrap-complete should exist and .bootstrap-lock
        should be removed. The mock uvicorn exits 0 immediately (doesn't
        actually run).
        """
        script_path, mock_dir, app_dir = self._make_script_and_env("", tmp_path)
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(app_dir)
        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        assert (app_dir / ".bootstrap-complete").exists(), (
            ".bootstrap-complete should be created after bootstrap"
        )
        assert not (app_dir / ".bootstrap-lock").exists(), (
            ".bootstrap-lock should be removed after bootstrap"
        )

    def test_it3_per_pack_install_in_script(self, tmp_path: Path) -> None:
        """IT-3: Per-pack install works correctly with multiple packs.

        With packs='core,documentation', the script should install both packs
        and create checkpoints for each.
        """
        script_path, mock_dir, app_dir = self._make_script_and_env("core,documentation", tmp_path)
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(app_dir)
        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        # Checkpoints should be written for both packs
        checkpoints_file = app_dir / ".bootstrap-checkpoints"
        assert checkpoints_file.exists(), "Checkpoints file should be created"
        content = checkpoints_file.read_text()
        assert "core" in content, "core pack should be in checkpoints"
        assert "documentation" in content, "documentation pack should be in checkpoints"
        assert (app_dir / ".bootstrap-complete").exists()

    def test_it4_checkpoint_resume_skips_installed(self, tmp_path: Path) -> None:
        """IT-4: Checkpoint resume skips already installed packs.

        Pre-populate .bootstrap-checkpoints with 'core' checkpointed.
        The script should skip 'core' and only install 'documentation'.
        """
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        # Pre-populate checkpoints with 'core' already done
        checkpoints = app_dir / ".bootstrap-checkpoints"
        checkpoints.write_text(
            '{"step": "pack_ingested", "pack": "core", "at": "2025-01-01T00:00:00+00:00"}\n'
        )
        # Create a recent lock file so stale lock detection doesn't wipe checkpoints
        lock_file = app_dir / ".bootstrap-lock"
        lock_file.touch()

        script = _build_entrypoint_script("core,documentation")
        script_path = tmp_path / "entrypoint.sh"
        script_path.write_text(script)
        script_path.chmod(0o755)

        mock_dir = self._make_mock_path(tmp_path)
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(app_dir)

        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        # 'core' should be skipped (already checkpointed)
        output = result.stdout.decode(errors="replace")
        assert "already ingested - skipping" in output
        assert "Pack core" in output
        # Both packs should end up in checkpoints
        content = checkpoints.read_text()
        assert "core" in content
        assert "documentation" in content
        assert (app_dir / ".bootstrap-complete").exists()

    def test_it5_stale_lock_recovery(self, tmp_path: Path) -> None:
        """IT-5: Stale lock recovery works — script detects stale lock and starts fresh.

        Create a lock file with mtime > 2 hours ago. The script should detect
        the stale lock, wipe it + checkpoints, and start bootstrap from scratch.
        """
        app_dir = tmp_path / "app"
        app_dir.mkdir(exist_ok=True)
        # Create a stale lock file (set mtime to 3 hours ago)
        lock_file = app_dir / ".bootstrap-lock"
        lock_file.touch()
        three_hours_ago = int(time.time()) - 10800
        os.utime(lock_file, (three_hours_ago, three_hours_ago))
        # Also create stale checkpoints
        checkpoints = app_dir / ".bootstrap-checkpoints"
        checkpoints.write_text("corrupt data\n")

        script_path, mock_dir, _ = self._make_script_and_env("", tmp_path)
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(app_dir)

        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        # Stale lock should be removed
        assert not lock_file.exists(), "Stale lock should be removed"
        # Checkpoints should be recreated (old one wiped)
        assert (app_dir / ".bootstrap-complete").exists()

    def test_it6_bootstrap_already_complete(self, tmp_path: Path) -> None:
        """IT-6: Bootstrap already complete — skip all bootstrap steps.

        With .bootstrap-complete already present, the script should skip
        Ollama install, migrations, pack install, and start uvicorn directly.
        """
        script_path, mock_dir, app_dir = self._make_script_and_env(
            "", tmp_path, bootstrap_complete=True
        )
        env = os.environ.copy()
        env["PATH"] = mock_dir + ":" + env.get("PATH", "")
        env["APP_DIR"] = str(app_dir)

        result = subprocess.run(
            ["bash", script_path],
            env=env,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Script exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
        )
        # .bootstrap-complete should still exist
        assert (app_dir / ".bootstrap-complete").exists()


# ---------------------------------------------------------------------------
# T8 — Host-Side Readiness Polling Tests (HR-1 through HR-3)
# ---------------------------------------------------------------------------


class TestReadinessPolling:
    """HR-1 through HR-3: Verify _wait_for_readiness() handles various scenarios."""

    def test_hr1_readiness_polling_handles_uvicorn_not_started(
        self,
    ) -> None:
        """HR-1: Polling handles connection errors then succeeds.

        First 3 calls raise URLError, then returns ready. Should return True.
        """
        import urllib.error

        call_count = [0]

        def fake_urlopen(url, timeout=None):
            call_count[0] += 1
            if call_count[0] <= 3:
                raise urllib.error.URLError("connection refused")
            return _FakeResp({"status": "ready"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            assert _wait_for_readiness(47950, timeout=60, poll_interval=0.01) is True
        assert call_count[0] >= 4, "Should have made at least 4 calls"

    def test_hr2_readiness_polling_times_out_correctly(self) -> None:
        """HR-2: Polling times out and returns False when service never starts."""
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            assert _wait_for_readiness(47950, timeout=5, poll_interval=0.01) is False

    def test_hr3_readiness_polling_warming_up_then_ready(self) -> None:
        """HR-3: Polling handles warming_up responses then ready.

        Returns True when the service transitions from warming_up to ready.
        """
        responses = [
            _FakeResp({"status": "warming_up", "progress": {"packs_ingested": 1}}),
            _FakeResp({"status": "warming_up", "progress": {"packs_ingested": 2}}),
            _FakeResp({"status": "ready"}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses):
            assert _wait_for_readiness(47950, timeout=60, poll_interval=0.01) is True
