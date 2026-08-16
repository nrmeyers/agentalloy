"""Regression tests for bughunt Batch 4 (install/server) findings.

Each test pins the specific behavior that was broken:
- 4.1 container restart must not kill a healthy service
- 4.2 sentinel-block removal must not glue adjacent lines
- 4.3 .env upsert must handle ``export ``-prefixed keys
- 4.4 load_state must exit cleanly (code 3) on corrupt / non-dict / bad-version state
- 4.5 systemd env file must be owner-only (0o600)
- 4.6 container env load: process env wins over .env
- 4.8 start_background: check+Popen serialized by an exclusive flock
- 4.9 llama-server idempotency gates on /health, not a bare TCP connect
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install import container_service, server_proc
from agentalloy.install import state as install_state
from agentalloy.install.subcommands import enable_service, uninstall
from agentalloy.install.subcommands import start_rerank_server as srs

# ---------------------------------------------------------------------------
# 4.2 — _remove_sentinel_block must not glue adjacent lines
# ---------------------------------------------------------------------------


class TestRemoveSentinelBlock:
    def test_does_not_glue_adjacent_lines(self) -> None:
        text = "line2\n# BEGIN\nblock\n# END\nline3"
        assert uninstall._remove_sentinel_block(text, "# BEGIN", "# END") == "line2\nline3"

    def test_preserves_blank_line_separation(self) -> None:
        text = "a\n\n# BEGIN\nblock\n# END\n\nb"
        assert uninstall._remove_sentinel_block(text, "# BEGIN", "# END") == "a\n\nb"

    def test_no_markers_returns_unchanged(self) -> None:
        assert uninstall._remove_sentinel_block("a\nb", "# BEGIN", "# END") == "a\nb"

    def test_inverted_markers_returns_unchanged(self) -> None:
        text = "# END\nblock\n# BEGIN"
        assert uninstall._remove_sentinel_block(text, "# BEGIN", "# END") == text


# ---------------------------------------------------------------------------
# 4.3 — upsert_env_file must handle export-prefixed keys
# ---------------------------------------------------------------------------


class TestUpsertEnvFileExport:
    def test_updates_export_prefixed_key(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        p.write_text("export FOO=old\nBAR=baz\n")
        install_state.upsert_env_file({"FOO": "new"}, path=p)
        content = p.read_text()
        assert "export FOO=new" in content
        assert "BAR=baz" in content
        assert content.count("FOO") == 1  # no duplicate line

    def test_deletes_export_prefixed_key(self, tmp_path: Path) -> None:
        p = tmp_path / ".env"
        p.write_text("export FOO=old\nBAR=baz\n")
        install_state.upsert_env_file({"FOO": None}, path=p)
        content = p.read_text()
        assert "FOO" not in content
        assert "BAR=baz" in content


# ---------------------------------------------------------------------------
# 4.4 — load_state must exit 3 on corrupt / non-dict / bad-version state
# ---------------------------------------------------------------------------


def _state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / ".config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    state_file = config_dir / "agentalloy" / "install-state.json"
    state_file.parent.mkdir(parents=True)
    return state_file


class TestLoadStateCorrupt:
    def test_corrupt_json_exits_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file(tmp_path, monkeypatch).write_text("{not valid json")
        with pytest.raises(SystemExit) as exc:
            install_state.load_state()
        assert exc.value.code == 3

    def test_non_dict_json_exits_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file(tmp_path, monkeypatch).write_text("[1, 2, 3]")
        with pytest.raises(SystemExit) as exc:
            install_state.load_state()
        assert exc.value.code == 3

    def test_string_schema_version_exits_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file(tmp_path, monkeypatch).write_text('{"schema_version": "abc"}')
        with pytest.raises(SystemExit) as exc:
            install_state.load_state()
        assert exc.value.code == 3


# ---------------------------------------------------------------------------
# 4.5 — systemd env file must be owner-only (0o600)
# ---------------------------------------------------------------------------


class TestSanitizeEnvSystemdMode:
    def test_sanitized_env_is_owner_only(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text('export API_KEY="secret"\nPLAIN=hello\n')
        sanitized = enable_service._sanitize_env_for_systemd(env_file)
        assert sanitized.name == "agentalloy.env"
        assert os.stat(sanitized).st_mode & 0o777 == 0o600
        content = sanitized.read_text()
        assert "API_KEY=secret" in content  # export + quotes stripped
        assert "PLAIN=hello" in content


# ---------------------------------------------------------------------------
# 4.1 — container restart must not terminate a healthy service
# ---------------------------------------------------------------------------


class TestContainerRestartHealthy:
    def test_healthy_service_is_left_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTALLOY_DB_LOCK_HELD", raising=False)
        monkeypatch.setattr(container_service.install_state, "load_state", lambda: {"port": 47950})
        monkeypatch.setattr(container_service.install_state, "validate_port", lambda p: p)
        monkeypatch.setattr(container_service.install_state, "user_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            container_service.server_proc, "server_log_path", lambda: tmp_path / "server.log"
        )
        monkeypatch.setattr(
            container_service.server_proc, "port_reachable", lambda port, host=None: True
        )
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.pid = 999
        monkeypatch.setattr(container_service.subprocess, "Popen", lambda *a, **k: fake_proc)

        assert container_service.restart_service_in_container() is True
        # The regression: a healthy service must NOT be terminated.
        fake_proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# 4.6 — container env load: process env wins over .env
# ---------------------------------------------------------------------------


class TestContainerEnvPrecedence:
    def test_process_env_wins_over_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTALLOY_DB_LOCK_HELD", raising=False)
        monkeypatch.setattr(container_service.install_state, "load_state", lambda: {"port": 47950})
        monkeypatch.setattr(container_service.install_state, "validate_port", lambda p: p)
        monkeypatch.setattr(container_service.install_state, "user_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            container_service.server_proc, "server_log_path", lambda: tmp_path / "server.log"
        )
        monkeypatch.setattr(
            container_service.server_proc, "port_reachable", lambda port, host=None: True
        )
        (tmp_path / ".env").write_text("MYKEY=from_dotenv\nOTHER=othervalue\n")
        monkeypatch.setenv("MYKEY", "from_process")
        captured: dict[str, Any] = {}

        def fake_popen(*a: Any, **k: Any) -> MagicMock:
            captured["env"] = k.get("env")
            m = MagicMock()
            m.poll.return_value = None
            m.pid = 999
            return m

        monkeypatch.setattr(container_service.subprocess, "Popen", fake_popen)

        assert container_service.restart_service_in_container() is True
        env = captured["env"]
        assert env["MYKEY"] == "from_process"  # process env wins (setdefault)
        assert env["OTHER"] == "othervalue"  # .env-only key still loaded


# ---------------------------------------------------------------------------
# 4.8 — start_background: check+Popen serialized by an exclusive flock
# ---------------------------------------------------------------------------


class TestStartBackgroundFlock:
    def test_check_and_popen_held_under_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("agentalloy.install.state.user_data_dir", lambda: tmp_path)
        monkeypatch.setattr("agentalloy.install.state.parse_env_file", lambda: {})
        monkeypatch.setattr(server_proc, "find_listening_pid", lambda *a, **k: None)
        entered = threading.Event()
        release = threading.Event()

        class FakePopen:
            def __init__(self, *a: Any, **k: Any) -> None:
                entered.set()
                release.wait(timeout=10)  # block inside Popen, still holding the flock
                self.pid = 1234

        monkeypatch.setattr("subprocess.Popen", FakePopen)
        lock_path = tmp_path / "server-start.lock"

        def worker() -> None:
            server_proc.start_background(47997)

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert entered.wait(timeout=5), "worker never reached Popen"
            # While start_background holds the flock (blocked in Popen), a
            # non-blocking flock from the main thread must fail.
            with open(lock_path, "a") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.set()
        t.join(timeout=5)
        assert not t.is_alive()


# ---------------------------------------------------------------------------
# 4.9 — llama-server idempotency gates on /health, not a bare TCP connect
# ---------------------------------------------------------------------------


def _rerank_args(models: Path) -> argparse.Namespace:
    return argparse.Namespace(
        models=str(models), hardware_target="cpu", timeout=1.0, json=False, quiet=True
    )


class TestLlamaServerHealthGate:
    def _models(self, tmp_path: Path) -> Path:
        p = tmp_path / "recommend-models.json"
        p.write_text(json.dumps({"options": [{"default": True, "rerank_model": "m.gguf"}]}))
        return p

    def test_ready_skips_launch(self, tmp_path: Path) -> None:
        models = self._models(tmp_path)
        with (
            patch.object(srs, "_health_ready", return_value=True),
            patch.object(srs, "_save"),
            patch("subprocess.Popen") as popen,
        ):
            rc = srs.run(_rerank_args(models))
        assert rc == 0
        popen.assert_not_called()

    def test_not_ready_proceeds_to_launch(self, tmp_path: Path) -> None:
        # A TCP-open-but-not-ready server must NOT be skipped: the first
        # /health probe (idempotency gate) is False, so we launch; the probe in
        # the wait loop is True, so we complete.
        gguf_dir = tmp_path / "models"
        gguf_dir.mkdir()
        (gguf_dir / "m.gguf").write_text("stub")
        models = self._models(tmp_path)
        with (
            patch.object(srs.install_state, "user_data_dir", return_value=tmp_path),
            patch.object(srs, "_health_ready", side_effect=[False, True]),
            patch.object(srs, "_save"),
            patch("subprocess.Popen") as popen,
        ):
            popen.return_value = MagicMock()
            rc = srs.run(_rerank_args(models))
        assert rc == 0
        popen.assert_called_once()
