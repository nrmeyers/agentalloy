"""Qwen Code session ID fallback reads from the repo-local QWEN_HOME, not ~/.qwen.

The qwen-code provider's env_builder sets QWEN_HOME to <cwd>/.qwen so Qwen
Code picks up a repo-local settings.json (see agentalloy.providers.qwen_code).
Session state — including runtime.json — is written under that same root, so
the fallback reader must look there too, or it silently misses live sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentalloy.api.proxy_router import _fallback_qwen_session_id


def _write_runtime(chats_dir: Path, session_id: str, mtime_offset: float = 0.0) -> Path:
    chats_dir.mkdir(parents=True, exist_ok=True)
    runtime_file = chats_dir / f"{session_id}.runtime.json"
    runtime_file.write_text(json.dumps({"session_id": session_id}))
    if mtime_offset:
        stat = runtime_file.stat()
        import os

        os.utime(runtime_file, (stat.st_atime + mtime_offset, stat.st_mtime + mtime_offset))
    return runtime_file


def _encoded(cwd: Path) -> str:
    import os

    return "-" + os.path.realpath(os.fspath(cwd)).lstrip("/").replace("/", "-")


class TestFallbackQwenSessionId:
    def test_reads_from_repo_local_qwen_home(self, tmp_path: Path) -> None:
        chats_dir = tmp_path / ".qwen" / "projects" / _encoded(tmp_path) / "chats"
        _write_runtime(chats_dir, "abc-123")

        assert _fallback_qwen_session_id(tmp_path) == "abc-123"

    def test_ignores_home_directory_qwen_state(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        home_chats = home / ".qwen" / "projects" / _encoded(tmp_path) / "chats"
        _write_runtime(home_chats, "stale-home-session")

        assert _fallback_qwen_session_id(tmp_path) is None

    def test_picks_most_recently_modified_session(self, tmp_path: Path) -> None:
        chats_dir = tmp_path / ".qwen" / "projects" / _encoded(tmp_path) / "chats"
        _write_runtime(chats_dir, "older", mtime_offset=-100)
        _write_runtime(chats_dir, "newer")

        assert _fallback_qwen_session_id(tmp_path) == "newer"

    def test_no_chats_dir_returns_none(self, tmp_path: Path) -> None:
        assert _fallback_qwen_session_id(tmp_path) is None
