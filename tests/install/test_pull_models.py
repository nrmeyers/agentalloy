# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownLambdaType=false
"""Unit tests for the ``pull-models`` subcommand.

Maps to test-plan.md § Model pulling.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands.pull_models import (
    _PRESENCE_CHECKS,  # pyright: ignore[reportPrivateUsage]
    STEP_NAME,
    _auto_pull,  # pyright: ignore[reportPrivateUsage]
    _collect_model_runner_pairs,  # pyright: ignore[reportPrivateUsage]
    _generate_ollama_ssh_key,  # pyright: ignore[reportPrivateUsage]
    _is_model_present_ollama,  # pyright: ignore[reportPrivateUsage]
    _is_remote_ollama,  # pyright: ignore[reportPrivateUsage]
    _ollama_requires_auth,  # pyright: ignore[reportPrivateUsage]
    _register_ollama_ssh_key,  # pyright: ignore[reportPrivateUsage]
    _ssh_key_error_hint,  # pyright: ignore[reportPrivateUsage]
    pull_models,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _recommend_output(
    *,
    embed_model: str = "qwen3-embedding:0.6b",
    embed_runner: str = "ollama",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host_target": "CPU+RAM",
        "preset": "cpu",
        "options": [
            {
                "default": True,
                "embed_model": embed_model,
                "embed_runner": embed_runner,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Pair extraction
# ---------------------------------------------------------------------------


class TestCollectPairs:
    def test_single_embed_pair(self) -> None:
        option = {
            "embed_model": "qwen3-embedding:0.6b",
            "embed_runner": "ollama",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1
        assert ("qwen3-embedding:0.6b", "ollama") in pairs

    def test_ignores_ingest_fields_if_present(self) -> None:
        option = {
            "embed_model": "qwen3-embedding:0.6b",
            "embed_runner": "ollama",
            "ingest_model": "qwen3.5:0.8b",
            "ingest_runner": "ollama",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1
        assert ("qwen3-embedding:0.6b", "ollama") in pairs

    def test_deduplicates_same_model_runner(self) -> None:
        option = {
            "embed_model": "qwen3-embedding:0.6b",
            "embed_runner": "ollama",
        }
        pairs = _collect_model_runner_pairs(option)
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# Ollama presence check
# ---------------------------------------------------------------------------


class TestOllamaPresence:
    def test_model_present(self) -> None:
        output = "NAME           ID          SIZE    MODIFIED\nembeddinggemma:latest   abc123   622 MB  2 days ago\n"
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            assert _is_model_present_ollama("embeddinggemma") is True

    def test_model_absent(self) -> None:
        output = "NAME           ID          SIZE    MODIFIED\nother:latest   abc123   100 MB  1 day ago\n"
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            assert _is_model_present_ollama("embeddinggemma") is False

    def test_ollama_not_installed(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _is_model_present_ollama("embeddinggemma") is False

    def test_tagged_model_match(self) -> None:
        output = "NAME              ID       SIZE    MODIFIED\nqwen3.5:0.8b      xyz789   1.0 GB  1 day ago\n"
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            assert _is_model_present_ollama("qwen3.5:0.8b") is True


# ---------------------------------------------------------------------------
# Auto-pull
# ---------------------------------------------------------------------------


class TestAutoPull:
    def test_successful_pull(self) -> None:
        # Daemon-running probe must return True so _auto_pull skips
        # the spawn path and goes straight to the pull subprocess.
        # _ollama_requires_auth must also return False so the pre-flight
        # check does not call subprocess.run (which is mocked).
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_requires_auth",
                return_value=False,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = _auto_pull("ollama", "embeddinggemma")
            assert result["success"] is True
            assert "duration_ms" in result
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["/usr/bin/ollama", "pull", "--", "embeddinggemma"]

    def test_pull_failure(self) -> None:
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_requires_auth",
                return_value=False,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
            result = _auto_pull("ollama", "embeddinggemma")
            assert result["success"] is False
            assert "connection refused" in result["error"]

    def test_binary_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            result = _auto_pull("ollama", "embeddinggemma")
            assert result["success"] is False
            assert "not found" in result["error"]

    def test_fastflowlm_pull(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/flm"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = _auto_pull("fastflowlm", "embed-gemma:300m")
            assert result["success"] is True
            args = mock_run.call_args[0][0]
            assert args == ["/usr/bin/flm", "pull", "--", "embed-gemma:300m"]


# ---------------------------------------------------------------------------
# Full pull_models
# ---------------------------------------------------------------------------


class TestPullModels:
    def test_auto_pull_embed_model(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_false}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {"success": True, "duration_ms": 100}
            result = pull_models(models, root=repo_root)
        assert result["schema_version"] == 1
        assert len(result["auto_pulled"]) == 1
        assert result["auto_pulled"][0]["model"] == "qwen3-embedding:0.6b"
        assert result["manual_steps_required"] == []
        assert result["skipped_already_present"] == []

    def test_skips_already_present(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"ollama": always_true}):
            result = pull_models(models, root=repo_root)
        assert len(result["skipped_already_present"]) == 1
        assert result["auto_pulled"] == []

    def test_manual_steps_for_fastflowlm(self, repo_root: Path) -> None:
        models = _recommend_output(
            embed_model="qwen3-embedding:0.6b",
            embed_runner="fastflowlm",
        )

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"fastflowlm": always_false}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {"success": True, "duration_ms": 50}
            result = pull_models(models, root=repo_root)
        assert len(result["auto_pulled"]) == 1
        assert result["auto_pulled"][0]["model"] == "qwen3-embedding:0.6b"

    def test_idempotent_skip(self, repo_root: Path) -> None:
        """Second run returns cached result when step already completed."""
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"ollama": always_true}):
            pull_models(models, root=repo_root)
        # Second call should return cached result (no longer raises SystemExit)
        cached = pull_models(models, root=repo_root)
        assert cached is not None
        assert "auto_pulled" in cached

    def test_records_state(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_true(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"ollama": always_true}):
            pull_models(models, root=repo_root)
        from agentalloy.install.state import is_step_completed, load_state

        st = load_state(repo_root)
        assert is_step_completed(st, STEP_NAME)

    def test_no_options_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit):
            pull_models({"options": []}, root=repo_root)

    def test_pull_error_recorded(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_false}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {
                "success": False,
                "error": "connection refused",
                "duration_ms": 10,
            }
            result = pull_models(models, root=repo_root)
        assert "errors" in result
        assert len(result["errors"]) == 1

    def test_partial_failure_does_not_record_completion(self, repo_root: Path) -> None:
        """If any pull fails, pull-models must NOT mark itself completed —
        otherwise idempotency permanently skips it on rerun."""
        from agentalloy.install.state import is_step_completed, load_state

        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_false}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {
                "success": False,
                "error": "connection refused",
                "duration_ms": 10,
            }
            pull_models(models, root=repo_root)
        st = load_state(repo_root)
        assert not is_step_completed(st, "pull-models")
        # And no models_pulled tracking either, since nothing succeeded.
        assert not st.get("models_pulled")

    def test_models_pulled_in_state(self, repo_root: Path) -> None:
        models = _recommend_output()

        def always_false(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_false}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {"success": True, "duration_ms": 100}
            pull_models(models, root=repo_root)
        from agentalloy.install.state import load_state

        st = load_state(repo_root)
        assert "ollama:qwen3-embedding:0.6b" in st["models_pulled"]


class TestOllamaDaemonAutoStart:
    """``_auto_pull`` must start the ollama daemon if it's down before pulling."""

    def test_no_binary_surfaces_clear_error(self, repo_root: Path) -> None:
        """When the ``ollama`` binary isn't on PATH, _auto_pull short-circuits
        with an actionable error — no daemon-start is attempted."""
        with patch(
            "agentalloy.install.subcommands.pull_models.shutil.which",
            return_value=None,
        ):
            result = _auto_pull("ollama", "qwen3-embedding:0.6b")
        assert result["success"] is False
        assert "not found" in (result["error"] or "").lower()

    def test_daemon_already_up_no_spawn(self, repo_root: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        with (
            patch.object(pm, "_ollama_daemon_running", return_value=True),
            patch(
                "agentalloy.install.subcommands.pull_models.shutil.which",
                return_value="/usr/bin/ollama",
            ),
            patch("agentalloy.install.subcommands.pull_models.subprocess.Popen") as mock_popen,
            patch("agentalloy.install.subcommands.pull_models.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = _auto_pull("ollama", "qwen3-embedding:0.6b")
        assert result["success"] is True
        mock_popen.assert_not_called()

    def test_daemon_down_triggers_spawn(self, repo_root: Path) -> None:
        from agentalloy.install.subcommands import pull_models as pm

        # First probe → False (down). After spawn, probes during the wait
        # loop return True (came up).
        check_results = iter([False, True])

        with (
            patch.object(
                pm,
                "_ollama_daemon_running",
                side_effect=lambda *_a, **_k: next(check_results),
            ),
            patch(
                "agentalloy.install.subcommands.pull_models.shutil.which",
                return_value="/usr/bin/ollama",
            ),
            patch("agentalloy.install.subcommands.pull_models.subprocess.Popen") as mock_popen,
            patch("agentalloy.install.subcommands.pull_models.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = _auto_pull("ollama", "qwen3-embedding:0.6b")
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "/usr/bin/ollama"
        assert cmd[1] == "serve"
        assert result["success"] is True

    def test_daemon_never_comes_up_fails_loudly(self, repo_root: Path) -> None:
        """Spawn succeeded but daemon never bound the port → clear error."""
        from agentalloy.install.subcommands import pull_models as pm

        # All probes return False. The deadline loop bails after the
        # time.monotonic() sequence indicates timeout.
        with (
            patch.object(pm, "_ollama_daemon_running", return_value=False),
            patch(
                "agentalloy.install.subcommands.pull_models.shutil.which",
                return_value="/usr/bin/ollama",
            ),
            patch("agentalloy.install.subcommands.pull_models.subprocess.Popen"),
            # Sequence: initial t0, then time.monotonic() > deadline on first
            # check inside the while loop.
            patch(
                "agentalloy.install.subcommands.pull_models.time.monotonic",
                side_effect=[0.0, 100.0, 100.0, 100.0],
            ),
            patch("agentalloy.install.subcommands.pull_models.time.sleep"),
        ):
            result = _auto_pull("ollama", "qwen3-embedding:0.6b")
        assert result["success"] is False
        assert "ollama" in (result["error"] or "").lower()


class TestRunExitCodes:
    """``_run`` exit codes: 0 = work done, 4 = no-op, 1 = error."""

    def _models_file(self, repo_root: Path, runner: str = "ollama") -> Path:
        import json as _json

        p = repo_root / "models.json"
        p.write_text(_json.dumps(_recommend_output(embed_runner=runner)))
        return p

    def test_exit_4_when_all_models_already_present(self, repo_root: Path) -> None:
        """Re-running pull-models after install: every model is present.
        No pulls happen, no manual steps required → EXIT_NOOP (4)."""
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_present(m: str) -> bool:
            return True

        with patch.dict(_PRESENCE_CHECKS, {"ollama": always_present}):
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 4

    def test_exit_0_when_models_pulled(self, repo_root: Path) -> None:
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_absent(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_absent}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {"success": True, "duration_ms": 50}
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 0

    def test_exit_1_when_pull_fails(self, repo_root: Path) -> None:
        from argparse import Namespace

        from agentalloy.install.subcommands.pull_models import _run

        models_path = self._models_file(repo_root)

        def always_absent(m: str) -> bool:
            return False

        with (
            patch.dict(_PRESENCE_CHECKS, {"ollama": always_absent}),
            patch("agentalloy.install.subcommands.pull_models._auto_pull") as mock_pull,
        ):
            mock_pull.return_value = {
                "runner": "ollama",
                "model": "qwen3-embedding:0.6b",
                "success": False,
                "error": "ollama daemon unavailable",
            }
            rc = _run(Namespace(models=str(models_path), runner=None, quiet=True))
        assert rc == 1


# ---------------------------------------------------------------------------
# SSH key error detection
# ---------------------------------------------------------------------------


_EXACT_ERROR = "pull model manifest: open /home/user/.ollama/id_ed25519: no such file or directory"


class TestSshKeyErrorHint:
    """Tests for _ssh_key_error_hint() — must require ALL patterns."""

    def test_exact_ollama_error(self) -> None:
        hint = _ssh_key_error_hint(_EXACT_ERROR)
        assert hint is not None
        assert "id_ed25519" in hint
        assert "ssh-keygen" in hint
        assert "server_user.pub" in hint

    def test_all_patterns_required(self) -> None:
        """Missing any ONE pattern → no hint (regression guard)."""
        # Has id_ed25519 + "no such file" but not "open"
        partial = "id_ed25519: no such file"
        assert _ssh_key_error_hint(partial) is None
        # Has "open" + "no such file" but not "id_ed25519"
        partial2 = "open /tmp/foo: no such file"
        assert _ssh_key_error_hint(partial2) is None
        # Has "open" + "id_ed25519" but not "no such file"
        partial3 = "open id_ed25519 succeeded"
        assert _ssh_key_error_hint(partial3) is None

    def test_no_match_unrelated_error(self) -> None:
        assert _ssh_key_error_hint("connection refused") is None
        assert _ssh_key_error_hint("network timeout") is None
        assert _ssh_key_error_hint("") is None

    def test_remote_host_adds_remote_guidance(self) -> None:
        with patch.dict(os.environ, {"OLLAMA_HOST": "https://remote.example.com"}):
            hint = _ssh_key_error_hint(_EXACT_ERROR)
        assert hint is not None
        assert "Remote Ollama" in hint
        assert "Contact your Ollama administrator" in hint

    def test_local_host_adds_local_guidance(self) -> None:
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://127.0.0.1:11434"}):
            hint = _ssh_key_error_hint(_EXACT_ERROR)
        assert hint is not None
        assert "does NOT require auth" in hint
        assert "Remote Ollama" not in hint

    def test_no_ollama_host_defaults_to_local(self) -> None:
        env = os.environ.copy()
        env.pop("OLLAMA_HOST", None)
        with patch.dict(os.environ, env, clear=False):
            # Need to clear it — patch.dict with clear=False won't remove
            pass
        # Use a direct approach: set env var to empty
        with patch("os.environ", {"OLLAMA_HOST": ""}):
            hint = _ssh_key_error_hint(_EXACT_ERROR)
        assert hint is not None
        assert "does NOT require auth" in hint
        assert "Remote Ollama" not in hint


# ---------------------------------------------------------------------------
# Pre-flight auth check
# ---------------------------------------------------------------------------


class TestOllamaRequiresAuth:
    """Tests for _ollama_requires_auth() heuristic."""

    def test_binary_missing_returns_false(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _ollama_requires_auth() is False

    def test_list_succeeds_returns_false(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            assert _ollama_requires_auth() is False

    def test_list_fails_with_auth_error_returns_true(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr="pull model manifest: open ~/.ollama/id_ed25519: no such file",
            )
            assert _ollama_requires_auth() is True

    def test_list_fails_unrelated_error_returns_false(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr="could not connect to ollama server",
            )
            assert _ollama_requires_auth() is False

    def test_timeout_returns_false(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ollama", 10)),
        ):
            assert _ollama_requires_auth() is False

    def test_oserror_returns_false(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run", side_effect=OSError("permission denied")),
        ):
            assert _ollama_requires_auth() is False

    def test_ssh_keyword_triggers(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="ssh authentication required")
            assert _ollama_requires_auth() is True

    def test_permission_denied_triggers(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")
            assert _ollama_requires_auth() is True


# ---------------------------------------------------------------------------
# Remote vs local detection
# ---------------------------------------------------------------------------


class TestIsRemoteOllama:
    """Tests for _is_remote_ollama() — host format handling."""

    def _set_host(self, value: str) -> None:
        os.environ["OLLAMA_HOST"] = value

    def _unset_host(self) -> None:
        os.environ.pop("OLLAMA_HOST", None)

    def test_empty_env_returns_false(self) -> None:
        self._unset_host()
        assert _is_remote_ollama() is False

    def test_bare_localhost_returns_false(self) -> None:
        for host in ("127.0.0.1", "localhost", "0.0.0.0"):
            self._set_host(host)
            assert _is_remote_ollama() is False, f"expected False for {host}"

    def test_localhost_with_port_returns_false(self) -> None:
        for host in ("127.0.0.1:11434", "localhost:11434", "0.0.0.0:11434"):
            self._set_host(host)
            assert _is_remote_ollama() is False, f"expected False for {host}"

    def test_protocol_prefix_localhost_returns_false(self) -> None:
        """Critical: http://127.0.0.1 must not be classified as remote."""
        for host in ("http://127.0.0.1:11434", "https://localhost:11434"):
            self._set_host(host)
            assert _is_remote_ollama() is False, f"expected False for {host}"

    def test_remote_host_returns_true(self) -> None:
        for host in ("192.168.1.100", "10.0.0.5:11434", "remote.example.com"):
            self._set_host(host)
            assert _is_remote_ollama() is True, f"expected True for {host}"

    def test_remote_with_protocol_returns_true(self) -> None:
        for host in ("http://remote.example.com", "https://192.168.1.100:11434"):
            self._set_host(host)
            assert _is_remote_ollama() is True, f"expected True for {host}"

    def test_trailing_slash_stripped(self) -> None:
        self._set_host("127.0.0.1/")
        assert _is_remote_ollama() is False

    def test_whitespace_stripped(self) -> None:
        self._set_host("  127.0.0.1  ")
        assert _is_remote_ollama() is False

    def tearDown(self) -> None:
        os.environ.pop("OLLAMA_HOST", None)


# ---------------------------------------------------------------------------
# Auto-pull SSH error detection integration
# ---------------------------------------------------------------------------


class TestAutoPullSshError:
    """Integration: _auto_pull must detect SSH key errors and set hint."""

    def test_ssh_error_sets_hint(self) -> None:
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr=_EXACT_ERROR)
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None
        assert "id_ed25519" in result["hint"]
        assert "ssh-keygen" in result["hint"]

    def test_non_ssh_error_no_hint(self) -> None:
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is None

    def test_fastflowlm_no_ssh_hint(self) -> None:
        """Non-ollama runners must not trigger SSH hint."""
        with (
            patch("shutil.which", return_value="/usr/bin/flm"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr=_EXACT_ERROR)
            result = _auto_pull("fastflowlm", "embed-gemma:300m")
        assert result["success"] is False
        assert result.get("hint") is None

    def test_success_no_hint(self) -> None:
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is True
        assert result.get("hint") is None


# ---------------------------------------------------------------------------
# SSH key auto-generation tests
# ---------------------------------------------------------------------------


class TestGenerateOllamaSshKey:
    """Tests for _generate_ollama_ssh_key()."""

    def test_key_already_exists_returns_success(self, tmp_path: Path) -> None:
        """Idempotent: if key already exists, return success."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        (ollama_dir / "id_ed25519").write_text("private_key")
        (ollama_dir / "id_ed25519.pub").write_text("public_key")

        with patch("pathlib.Path.home", return_value=tmp_path):
            ok, err = _generate_ollama_ssh_key()
        assert ok is True
        assert err is None

    def test_ssh_keygen_missing_returns_failure(self, tmp_path: Path) -> None:
        """When ssh-keygen is not on PATH, return failure."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("shutil.which", return_value=None):
                ok, err = _generate_ollama_ssh_key()
        assert ok is False
        assert "ssh-keygen not found" in err  # type: ignore[operator]

    def test_ssh_keygen_failure_returns_error(self, tmp_path: Path) -> None:
        """When ssh-keygen fails, return the error."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()

        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("shutil.which", return_value="/usr/bin/ssh-keygen"):
                with patch(
                    "subprocess.run",
                    return_value=MagicMock(returncode=1, stderr="permission denied"),
                ):
                    ok, err = _generate_ollama_ssh_key()
        assert ok is False
        assert err is not None
        assert "permission denied" in err  # type: ignore[operator]


class TestRegisterOllamaSshKey:
    """Tests for _register_ollama_ssh_key()."""

    def test_public_key_missing_returns_failure(self, tmp_path: Path) -> None:
        """When public key doesn't exist, return failure."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            ok, err = _register_ollama_ssh_key()
        assert ok is False
        assert "Public key not found" in err  # type: ignore[operator]

    def test_key_already_registered_returns_success(self, tmp_path: Path) -> None:
        """Idempotent: if key already in server_user.pub, return success."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        (ollama_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA... user@host")
        (ollama_dir / "server_user.pub").write_text("ssh-ed25519 AAAA... user@host\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            ok, err = _register_ollama_ssh_key()
        assert ok is True
        assert err is None

    def test_register_appends_to_server_user_pub(self, tmp_path: Path) -> None:
        """New key should be appended to server_user.pub."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        pub_key = "ssh-ed25519 AAAA... user@host"
        (ollama_dir / "id_ed25519.pub").write_text(pub_key)
        (ollama_dir / "server_user.pub").write_text("ssh-ed25519 BBBB... other@host\n")

        with patch("pathlib.Path.home", return_value=tmp_path):
            ok, err = _register_ollama_ssh_key()
        assert ok is True
        assert err is None
        server_pub = ollama_dir / "server_user.pub"
        content = server_pub.read_text()
        assert "BBBB" in content
        assert "AAAA" in content

    def test_register_creates_server_user_pub_if_missing(self, tmp_path: Path) -> None:
        """server_user.pub should be created if it doesn't exist."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        pub_key = "ssh-ed25519 AAAA... user@host"
        (ollama_dir / "id_ed25519.pub").write_text(pub_key)
        # server_user.pub does NOT exist

        with patch("pathlib.Path.home", return_value=tmp_path):
            ok, err = _register_ollama_ssh_key()
        assert ok is True
        assert err is None
        server_pub = ollama_dir / "server_user.pub"
        assert server_pub.exists()
        assert pub_key in server_pub.read_text()


class TestAutoPullSshKeyAutoFix:
    """Integration: _auto_pull SSH key auto-generation and retry flow."""

    def test_ssh_error_auto_fix_succeeds(self, tmp_path: Path) -> None:
        """When SSH key error occurs on local Ollama, auto-generate and retry."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        pub_key = "ssh-ed25519 AAAA... user@host"
        (ollama_dir / "id_ed25519.pub").write_text(pub_key)

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            # Call 1: ollama list (auth check) - SSH error
            # Call 2: ollama pull - SSH error
            # Call 3: ssh-keygen - success
            # Call 4: ollama pull (retry) - success
            if call_count in (1, 2):
                return MagicMock(returncode=1, stderr=_EXACT_ERROR)
            else:
                return MagicMock(returncode=0, stderr="")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run", side_effect=fake_run),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is True
        assert result.get("ssh_key_auto_fixed") is True
        assert call_count == 4  # list + pull + ssh-keygen + retry

    def test_ssh_error_auto_fix_user_declines(self, tmp_path: Path) -> None:
        """When user declines registration, return original error."""
        with (
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr=_EXACT_ERROR)
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None
        assert "id_ed25519" in result["hint"]

    def test_ssh_error_remote_ollama_no_auto_fix(self) -> None:
        """Remote Ollama instances should NOT trigger auto-fix."""
        with (
            patch.dict(os.environ, {"OLLAMA_HOST": "https://remote.example.com"}),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
            patch("sys.stdin.isatty", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr=_EXACT_ERROR)
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None
        # No auto-fix attempt — 2 calls: list (auth check) + pull
        assert mock_run.call_count == 2

    def test_ssh_error_non_interactive_no_auto_fix(self, tmp_path: Path) -> None:
        """Non-interactive mode should NOT trigger auto-fix."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
            patch("sys.stdin.isatty", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr=_EXACT_ERROR)
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None
        # No auto-fix attempt — 2 calls: list (auth check) + pull
        assert mock_run.call_count == 2

    def test_ssh_error_key_generation_fails(self, tmp_path: Path) -> None:
        """When key generation fails, return original error with hint."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr=_EXACT_ERROR),  # Call 1: ollama list
                MagicMock(returncode=1, stderr=_EXACT_ERROR),  # Call 2: ollama pull
                MagicMock(returncode=1, stderr="ssh-keygen failed"),  # Call 3: ssh-keygen
            ]
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None

    def test_ssh_error_registration_fails(self, tmp_path: Path) -> None:
        """When registration fails, return original error with hint."""
        ollama_dir = tmp_path / ".ollama"
        ollama_dir.mkdir()
        (ollama_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA... user@host")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
            patch("pathlib.Path.write_text", side_effect=OSError("permission denied")),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=1, stderr=_EXACT_ERROR),  # Call 1: ollama list
                MagicMock(returncode=1, stderr=_EXACT_ERROR),  # Call 2: ollama pull
                MagicMock(returncode=0, stderr=""),  # Call 3: ssh-keygen
                MagicMock(returncode=1, stderr="permission denied"),  # Call 4: retry pull
            ]
            result = _auto_pull("ollama", "embeddinggemma")
        assert result["success"] is False
        assert result["hint"] is not None
