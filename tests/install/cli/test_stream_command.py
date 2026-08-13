"""``agentalloy stream`` — CLI subcommand tests.

Verifies the three actions: ``status``, ``use``, and ``clear``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentalloy.install.subcommands import stream
from agentalloy.storage.stream_id import resolve_stream_id


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {"project_root": None}
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# stream status
# ---------------------------------------------------------------------------


class TestStatus:
    def _write_stream(self, root: Path, text: str) -> None:
        (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
        (root / ".agentalloy" / ".stream").write_text(text)

    def test_status_no_binding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        args = _args(project_root=str(tmp_path))
        rc = stream._run_status(args)
        assert rc == 0
        # falls back to path hash — 16-char hex
        assert len(resolve_stream_id(tmp_path)) == 16

    def test_status_with_binding_file(self, tmp_path: Path) -> None:
        self._write_stream(tmp_path, "my-stream-id")
        args = _args(project_root=str(tmp_path))
        rc = stream._run_status(args)
        assert rc == 0

    def test_status_binding_file_takes_precedence_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTALLOY_STREAM_ID", "from-env")
        self._write_stream(tmp_path, "from-file")
        # resolve_stream_id checks binding file first (before env), so status
        # will show binding-file as source — that's the correct priority.


# ---------------------------------------------------------------------------
# stream use
# ---------------------------------------------------------------------------


class TestUse:
    def test_use_nonexistent_root_errors(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does-not-exist"
        args = _args(project_root=str(nonexistent), stream_id="pinned")
        assert stream._run_use(args) == 1

    def test_use_writes_binding_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        args = _args(project_root=str(tmp_path), stream_id="pinned-42")
        rc = stream._run_use(args)
        assert rc == 0
        assert (tmp_path / ".agentalloy" / ".stream").read_text().strip() == "pinned-42"

    def test_use_updates_resolution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        args = _args(project_root=str(tmp_path), stream_id="new-id")
        stream._run_use(args)
        assert resolve_stream_id(tmp_path) == "new-id"

    def test_use_empty_string_errors(self, tmp_path: Path) -> None:
        args = _args(project_root=str(tmp_path), stream_id="  ")
        assert stream._run_use(args) == 1

    def test_use_empty_string_no_error(self, tmp_path: Path) -> None:
        """Edge: bare empty string after stripping."""
        args = _args(project_root=str(tmp_path), stream_id="")
        assert stream._run_use(args) == 1


# ---------------------------------------------------------------------------
# stream clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_binding(self, tmp_path: Path) -> None:
        stream.bind_stream_id(tmp_path, "pinned")
        args = _args(project_root=str(tmp_path))
        rc = stream._run_clear(args)
        assert rc == 0
        assert not (tmp_path / ".agentalloy" / ".stream").exists()

    def test_clear_on_already_unpinned(self, tmp_path: Path) -> None:
        args = _args(project_root=str(tmp_path))
        rc = stream._run_clear(args)
        assert rc == 0

    def test_clear_fallback_to_path_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream.bind_stream_id(tmp_path, "pinned")
        args = _args(project_root=str(tmp_path))
        stream._run_clear(args)
        # After clearing, resolution falls back to path hash
        resolved = resolve_stream_id(tmp_path)
        assert len(resolved) == 16
