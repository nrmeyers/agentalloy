"""Task 03 — the upgrade path carries every wired repo's phase file into the store.

Same terms as the code-index layout migration it sits beside: unconditional,
un-prompted, after the service is back up, and never able to fail an upgrade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentalloy.api.state_client import StateClientError
from agentalloy.install.subcommands import upgrade


class _StubClient:
    def __init__(self, *, imported: dict[str, str] | None = None, raises: Exception | None = None):
        self.calls: list[str | None] = []
        self._imported = imported if imported is not None else {"phase": "build"}
        self._raises = raises

    def import_files(self, repo_root: str | None = None) -> dict[str, str]:
        self.calls.append(repo_root)
        if self._raises is not None:
            raise self._raises
        return self._imported


@pytest.fixture()
def stub(monkeypatch: pytest.MonkeyPatch):
    def _install(client: _StubClient) -> _StubClient:
        monkeypatch.setattr("agentalloy.api.state_client.StateClient", lambda: client)
        return client

    return _install


def _wire(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> None:
    state: dict[str, Any] = {
        "harness_files_written": [{"repo_root": str(r), "harness": "claude-code"} for r in roots]
    }
    monkeypatch.setattr(upgrade.install_state, "load_state", lambda: state)


def _mirror(root: Path) -> Path:
    ag = root / ".agentalloy"
    ag.mkdir(parents=True, exist_ok=True)
    (ag / "phase").write_text("phase: build\n", encoding="utf-8")
    return ag


class TestRepoRegistry:
    def test_wiring_records_are_the_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _wire(monkeypatch, tmp_path / "a", tmp_path / "b")
        assert upgrade._repos_with_state_files() == [str(tmp_path / "a"), str(tmp_path / "b")]

    def test_a_repo_wired_twice_appears_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One repo, two harnesses, is still one import."""
        _wire(monkeypatch, tmp_path / "a", tmp_path / "a")
        assert upgrade._repos_with_state_files() == [str(tmp_path / "a")]

    def test_unreadable_install_state_yields_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> dict[str, Any]:
            raise OSError("state file is gone")

        monkeypatch.setattr(upgrade.install_state, "load_state", _boom)
        assert upgrade._repos_with_state_files() == []


class TestImportStep:
    def test_imports_each_wired_repo_that_has_a_mirror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub
    ) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        _mirror(a)
        _mirror(b)
        _wire(monkeypatch, a, b)
        client = stub(_StubClient())

        actions: list[str] = []
        warnings: list[str] = []
        upgrade._import_state_files(actions, warnings, show_progress=False)

        assert client.calls == [str(a), str(b)]
        assert actions == ["migrated phase state into the store (2 repos)"]
        assert warnings == []

    def test_repo_without_a_mirror_is_not_called(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub
    ) -> None:
        """No ``.agentalloy`` at all — nothing to migrate, no HTTP round trip."""
        _wire(monkeypatch, tmp_path / "gone")
        client = stub(_StubClient())

        upgrade._import_state_files([], [], show_progress=False)

        assert client.calls == []

    def test_nothing_imported_reports_no_action(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub
    ) -> None:
        """The steady state after the first upgrade must be silent."""
        a = tmp_path / "a"
        _mirror(a)
        _wire(monkeypatch, a)
        stub(_StubClient(imported={}))

        actions: list[str] = []
        upgrade._import_state_files(actions, [], show_progress=False)

        assert actions == []

    def test_a_down_service_warns_rather_than_failing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub
    ) -> None:
        a = tmp_path / "a"
        _mirror(a)
        _wire(monkeypatch, a)
        stub(_StubClient(raises=StateClientError("agentalloy service is not running")))

        actions: list[str] = []
        warnings: list[str] = []
        upgrade._import_state_files(actions, warnings, show_progress=False)

        assert actions == []
        assert len(warnings) == 1
        assert str(a) in warnings[0]

    def test_an_unexpected_error_never_fails_the_upgrade(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stub
    ) -> None:
        a = tmp_path / "a"
        _mirror(a)
        _wire(monkeypatch, a)
        stub(_StubClient(raises=RuntimeError("boom")))

        upgrade._import_state_files([], [], show_progress=False)  # must not raise

    def test_no_wired_repos_skips_the_client_entirely(
        self, monkeypatch: pytest.MonkeyPatch, stub
    ) -> None:
        monkeypatch.setattr(upgrade.install_state, "load_state", dict)
        client = stub(_StubClient())

        upgrade._import_state_files([], [], show_progress=False)

        assert client.calls == []
