# ruff: noqa: I001 -- testing private module members intentionally
"""Tests for entrypoint generation and readiness polling.

Covers UT-9..UT-25 from docs/tests/container-setup-improvements.md and
IT-2 (bash syntax check) and EC-12/EC-13 (no-packs path).
"""

from __future__ import annotations

import json
import shutil
import subprocess
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
            "uvicorn must start after pack ingest (avoids Ladybug lock conflict)"
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
        # `|| echo 0` swallows grep failures; pack_already_done returns
        # non-zero on no match so a corrupt file simply re-runs packs.
        assert "|| echo 0" in script

    def test_ec12_ec13_no_packs_path(self) -> None:
        # When packs="" the entrypoint still installs always-on packs
        # (so the container reaches MIN_SKILL_COUNT) and still wires
        # uvicorn + the complete marker.
        script = _build_entrypoint_script("")
        assert "uv run agentalloy install-packs --no-restart" in script
        # Still wires uvicorn + complete marker even with no packs.
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

    def test_first_success_model_connection_errors_before_200(self) -> None:
        """Connection errors before the first 200 response are silently ignored.

        This is the critical fix from the review: the old grace-window model
        counted these errors and gave up after 3 consecutive failures (~120s).
        The new first-success model ignores them entirely until the container
        proves it is alive.
        """
        import urllib.error

        call_count = [0]

        def fake_urlopen(url, timeout=5):  # type: ignore[no-untyped-def]
            call_count[0] += 1
            if call_count[0] <= 5:
                # First 5 calls: connection refused (container not ready yet)
                raise urllib.error.URLError("connection refused")
            # 6th call onwards: container is alive, eventually ready
            if call_count[0] <= 7:
                return _FakeResp({"status": "warming_up"})
            # 8th call: container is ready
            return _FakeResp({"status": "ready"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = _wait_for_readiness(
                47950,
                timeout=60,
                poll_interval=0.01,
            )
        # The key assertion: it did NOT return False after 3 errors.
        assert ok is True, "Should succeed after initial errors resolve"
        assert call_count[0] > 3, (
            "Should have made more than 3 calls (did not short-circuit on errors)"
        )

    def test_first_success_model_errors_after_200_count(self) -> None:
        """After first 200, consecutive errors count toward the 3-strike limit.

        Once the container has proven it is alive (returned 200), errors
        indicate a crash or network issue and should be counted.
        """
        import urllib.error

        responses = [
            _FakeResp({"status": "warming_up"}),  # 1st: success, resets counter
            urllib.error.URLError("connection refused"),  # 2nd: error, count=1
            urllib.error.URLError("connection refused"),  # 3rd: error, count=2
            urllib.error.URLError("connection refused"),  # 4th: error, count=3 → FAIL
        ]

        with patch("urllib.request.urlopen", side_effect=responses):
            ok = _wait_for_readiness(
                47950,
                timeout=60,
                poll_interval=0.01,
            )
        assert ok is False, "Should return False after 3 consecutive errors post-first-success"

    def test_first_success_model_recovers_after_error(self) -> None:
        """After first 200, a single error followed by success resets the counter.

        A transient error after the container is alive should not cause
        early exit — only 3 consecutive errors should.
        """
        import urllib.error

        responses = [
            _FakeResp({"status": "warming_up"}),  # 1st: success, resets counter
            urllib.error.URLError("connection refused"),  # 2nd: error, count=1
            _FakeResp({"status": "warming_up"}),  # 3rd: success, resets counter
            urllib.error.URLError("connection refused"),  # 4th: error, count=1
            _FakeResp({"status": "ready"}),  # 5th: success → return True
        ]

        with patch("urllib.request.urlopen", side_effect=responses):
            ok = _wait_for_readiness(
                47950,
                timeout=60,
                poll_interval=0.01,
            )
        assert ok is True, "Should recover from transient errors and return True on ready"


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
