"""Slice 06 — a down service stops the command instead of forking the truth.

Every phase-touching CLI surface used to catch the transport error and write
``.agentalloy/phase`` instead.  That made an outage invisible: the CLI wrote one
place, the service read another, and the two drifted until something noticed.
There is no second place to write now, so the only honest answer is to fail.

These tests are the only ones in the suite that exercise that path.  The
autouse ``_bound_state_store`` fixture binds a process store for every other
test, which makes ``phase_access`` resolve in-process and the service-down
branch unreachable — so each test here unbinds it deliberately.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentalloy.install.subcommands._state import SERVICE_DOWN_MESSAGE


@pytest.fixture(autouse=True)
def _no_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI running outside the service, with nothing listening.

    Two things have to be false at once for this to describe reality: no store
    is bound in this process (otherwise the in-process seam wins), and the
    autostart attempt fails (otherwise the fixture spawns a real uvicorn
    mid-suite and the test waits out the health poll).
    """
    import agentalloy.install.subcommands._state as state_mod
    import agentalloy.storage.state_store as store_mod

    monkeypatch.setattr(store_mod, "process_store", lambda: None)
    monkeypatch.setattr(state_mod, "_try_start", lambda: False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    # A subdirectory, not tmp_path itself: the suite's XDG dirs live at the top
    # of tmp_path, and this module asserts on the repo's *entire* contents.
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    return root


def _assert_refused(excinfo: pytest.ExceptionInfo[SystemExit], capsys: pytest.CaptureFixture[str]):
    """Non-zero exit carrying the one fixed message docs and support point at."""
    assert excinfo.value.code == 1
    assert SERVICE_DOWN_MESSAGE in capsys.readouterr().err


class TestSurfacesRefuse:
    """AC-5 — one test per surface, because each reaches the store differently."""

    def test_phase_get(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from agentalloy.install.subcommands.phase import run_phase_get

        with pytest.raises(SystemExit) as e:
            run_phase_get(root=repo)
        _assert_refused(e, capsys)

    def test_phase_set(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from agentalloy.install.subcommands.phase import run_phase_set

        with pytest.raises(SystemExit) as e:
            run_phase_set("build", root=repo)
        _assert_refused(e, capsys)

    def test_flow(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from agentalloy.install.subcommands.workflow import run_workflow_pause

        with pytest.raises(SystemExit) as e:
            run_workflow_pause(root=repo)
        _assert_refused(e, capsys)

    def test_task(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from agentalloy.install.subcommands.task import run_task_next

        with pytest.raises(SystemExit) as e:
            run_task_next(repo)
        _assert_refused(e, capsys)

    def test_approve(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from agentalloy.install.subcommands.approve import run_approve

        with pytest.raises(SystemExit) as e:
            run_approve("spec", root=repo, approver="test")
        _assert_refused(e, capsys)

    def test_contract(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Contract verbs have no in-process equivalent, but share the message.

        They used to print their own wording, naming an ``agentalloy start``
        command that does not exist.
        """
        from agentalloy.install.subcommands.contract import (
            _show,  # pyright: ignore[reportPrivateUsage]
        )

        with pytest.raises(SystemExit) as e:
            _show(argparse.Namespace(contract_id="c-1"))
        _assert_refused(e, capsys)


class TestRefusalWritesNothing:
    def test_no_file_is_left_behind(self, repo: Path) -> None:
        """The refusal is the whole behaviour — not a refusal plus a side effect.

        A phase set that cannot reach the store must leave the repo exactly as
        it found it; anything on disk here would be the file mirror growing
        back under a different name.
        """
        from agentalloy.install.subcommands.phase import run_phase_set

        with pytest.raises(SystemExit):
            run_phase_set("build", root=repo)

        assert not (repo / ".agentalloy").exists()
        assert sorted(p.name for p in repo.iterdir()) == ["pyproject.toml"]
