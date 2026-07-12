"""Session-scoped work-item cursor (Bug C).

The cursor is written by the CLI (`task start/next`) and read by the proxy; a
single shared `.agentalloy/cursor` let one session clobber another's current
work-item. Scoping the file by the session key isolates concurrent sessions,
with the shared file as the back-compat / no-key fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.contracts import (
    cursor_state_name,
    resolve_current_contract,
)
from agentalloy.install.subcommands.task import run_task_start
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _clear_all_cursors,
    _read_cursor,
    _write_cursor_atomic,
    _write_phase_atomic,
    cli_session_key,
)

_KEY_A = "11111111-aaaa-4aaa-8aaa-111111111111"
_KEY_B = "22222222-bbbb-4bbb-8bbb-222222222222"


def _seed(root: Path, phase: str, names: list[str]) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "phase").write_text(f"phase: {phase}\n")
    d = root / ".agentalloy" / "contracts" / phase
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.md").write_text(
            f"---\nphase: {phase}\ntask_slug: {n}\ndomain_tags: [pytest]\n---\n# {n}\nbody\n"
        )


class TestCursorStateName:
    def test_none_key_is_shared_cursor(self) -> None:
        assert cursor_state_name(None) == "cursor"
        assert cursor_state_name("") == "cursor"

    def test_key_is_scoped_and_stable(self) -> None:
        name = cursor_state_name(_KEY_A)
        assert name.startswith("cursor.") and name != "cursor"
        assert name == cursor_state_name(_KEY_A)  # deterministic

    def test_distinct_keys_distinct_files(self) -> None:
        assert cursor_state_name(_KEY_A) != cursor_state_name(_KEY_B)


class TestScopedReadWrite:
    def test_scoped_write_read_roundtrip(self, tmp_path: Path) -> None:
        (tmp_path / ".agentalloy").mkdir()
        _write_cursor_atomic(tmp_path, "build/x.md", _KEY_A)
        assert _read_cursor(tmp_path, _KEY_A) == "build/x.md"
        # Backing file is the scoped one, not the shared cursor.
        assert (tmp_path / ".agentalloy" / cursor_state_name(_KEY_A)).is_file()
        assert not (tmp_path / ".agentalloy" / "cursor").exists()

    def test_read_falls_back_to_shared(self, tmp_path: Path) -> None:
        (tmp_path / ".agentalloy").mkdir()
        _write_cursor_atomic(tmp_path, "build/shared.md", None)  # shared
        # A keyed reader with no scoped file falls back to the shared value.
        assert _read_cursor(tmp_path, _KEY_A) == "build/shared.md"

    def test_scoped_wins_over_shared(self, tmp_path: Path) -> None:
        (tmp_path / ".agentalloy").mkdir()
        _write_cursor_atomic(tmp_path, "build/shared.md", None)
        _write_cursor_atomic(tmp_path, "build/mine.md", _KEY_A)
        assert _read_cursor(tmp_path, _KEY_A) == "build/mine.md"
        assert _read_cursor(tmp_path, None) == "build/shared.md"

    def test_no_leak_between_sessions(self, tmp_path: Path) -> None:
        # The regression: A's cursor must not be visible to B.
        (tmp_path / ".agentalloy").mkdir()
        _write_cursor_atomic(tmp_path, "build/a-work.md", _KEY_A)
        # B has no scoped file and no shared file → None, NOT a-work.
        assert _read_cursor(tmp_path, _KEY_B) is None


class TestResolveWithSessionKey:
    def test_two_sessions_resolve_their_own_workitem(self, tmp_path: Path) -> None:
        _seed(tmp_path, "build", ["01-cache", "02-api", "03-log"])
        _write_cursor_atomic(tmp_path, "build/01-cache.md", _KEY_A)
        _write_cursor_atomic(tmp_path, "build/03-log.md", _KEY_B)
        cid_a, _ = resolve_current_contract(tmp_path, "build", _KEY_A)
        cid_b, _ = resolve_current_contract(tmp_path, "build", _KEY_B)
        assert cid_a == "build/01-cache.md"
        assert cid_b == "build/03-log.md"

    def test_keyless_fanout_is_strict_none(self, tmp_path: Path) -> None:
        # No scoped file, no shared cursor, ≥2 contracts → the resolver never guesses.
        _seed(tmp_path, "build", ["01-cache", "02-api"])
        assert resolve_current_contract(tmp_path, "build", _KEY_A) == (None, None)


class TestClearAndTransition:
    def test_clear_all_removes_scoped_and_shared(self, tmp_path: Path) -> None:
        (tmp_path / ".agentalloy").mkdir()
        _write_cursor_atomic(tmp_path, "build/x.md", None)
        _write_cursor_atomic(tmp_path, "build/a.md", _KEY_A)
        _write_cursor_atomic(tmp_path, "build/b.md", _KEY_B)
        _clear_all_cursors(tmp_path)
        assert _read_cursor(tmp_path, None) is None
        assert _read_cursor(tmp_path, _KEY_A) is None
        assert _read_cursor(tmp_path, _KEY_B) is None

    def test_phase_transition_drops_stale_scoped(self, tmp_path: Path) -> None:
        # A session's scoped cursor from the old phase must not survive a transition
        # (it resolves by filename, not phase — the cross-phase-bleed trap).
        _seed(tmp_path, "build", ["01-cache", "02-api"])
        _write_cursor_atomic(tmp_path, "build/02-api.md", _KEY_A)
        _write_phase_atomic(tmp_path, "qa")  # qa has no contracts → nothing seeded
        assert _read_cursor(tmp_path, _KEY_A) is None  # scoped cleared, no shared to fall back to


class TestCliSessionKey:
    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _KEY_A)
        assert cli_session_key() == _KEY_A

    def test_absent_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        assert cli_session_key() is None

    def test_task_start_writes_scoped_not_shared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end: the CLI writer scopes by the env session id; a concurrent
        # session (different key) does not see it, and the shared file is untouched.
        _seed(tmp_path, "build", ["01-cache", "02-api"])
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _KEY_A)
        run_task_start("02-api", tmp_path)
        assert _read_cursor(tmp_path, _KEY_A) == "build/02-api.md"
        assert _read_cursor(tmp_path, _KEY_B) is None  # no leak to another session
        assert not (tmp_path / ".agentalloy" / "cursor").exists()  # shared untouched
