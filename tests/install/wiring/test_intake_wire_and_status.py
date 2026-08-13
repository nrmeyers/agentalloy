"""Unit tests for the intent-correct intake entry + wire/status fixes.

Covers:
- Bug 1: _repo_root() treats $HOME as a boundary (never resolves the repo
  root to the home directory via a stray ~/package.json).
- Bug 3: `wire` drops the repo README and git-excludes .agentalloy/ — and
  writes no lifecycle state at all; `status` classifies user-global files and
  reports per-repo activation.
- Intake: the `intake` phase is accepted and selects the intake workflow skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]
from agentalloy.install.subcommands.phase import VALID_PHASES, run_phase_set
from agentalloy.install.subcommands.status import (
    _path_scope,  # pyright: ignore[reportPrivateUsage]
    _repo_phase,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.install.subcommands.wire import (
    _seed_repo_metadata,  # pyright: ignore[reportPrivateUsage]
)


class TestRepoRootHomeBoundary:
    """Bug 1: the walk-up stops at $HOME and falls back to cwd."""

    def test_stray_marker_at_home_does_not_make_home_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        proj = home / "proj"  # markerless project under home
        proj.mkdir(parents=True)
        (home / "package.json").write_text("{}")  # the stray marker
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(proj)
        assert _repo_root() == proj.resolve()

    def test_marker_below_home_is_still_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        proj = home / "proj"
        (proj / ".git").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(proj)
        assert _repo_root() == proj.resolve()

    def test_non_home_path_walks_up_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # home is not in the ancestor chain, so the walk is unaffected.
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "elsewhere")
        work = tmp_path / "work"
        sub = work / "a" / "b"
        sub.mkdir(parents=True)
        (work / "pyproject.toml").write_text("")
        monkeypatch.chdir(sub)
        assert _repo_root() == work.resolve()


class TestWireSeedsRepoMetadata:
    """Bug 3, as it stands now: wiring is a pure configuration step.

    It used to seed the entry phase here, which made ``wire`` a writer of SDD
    state and gave it a hard dependency on a running service.  The phase is
    seeded lazily by the proxy on the repo's first request instead, so these
    tests pin the inverse: metadata yes, lifecycle state no.
    """

    def test_writes_readme_and_git_excludes(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        _seed_repo_metadata(tmp_path)
        assert (tmp_path / ".agentalloy" / "README.md").is_file()
        exclude = tmp_path / ".git" / "info" / "exclude"
        assert exclude.exists()
        assert any(line.strip() == ".agentalloy/" for line in exclude.read_text().splitlines())

    def test_writes_no_phase(self, tmp_path: Path) -> None:
        _seed_repo_metadata(tmp_path)
        assert _repo_phase(str(tmp_path)) is None
        assert not (tmp_path / ".agentalloy" / "phase").exists()

    def test_a_phaseless_repo_is_legitimate(self, tmp_path: Path) -> None:
        """AC-9 — nothing downstream treats "wired, no phase yet" as broken.

        Between `wire` and the repo's first proxy request there is a window in
        which the repo is fully wired and has no phase at all.  That used to be
        impossible, so the read paths were never asked to handle it; each one
        must now answer "none" rather than raise.
        """
        from agentalloy.install.subcommands.phase import run_phase_get
        from agentalloy.web.ops_api import _repo_info  # pyright: ignore[reportPrivateUsage]

        _seed_repo_metadata(tmp_path)

        assert run_phase_get(root=tmp_path)["phase"] is None
        assert _repo_info(str(tmp_path), ["claude-code"]).phase is None

    def test_does_not_disturb_an_existing_phase(self, tmp_path: Path) -> None:
        run_phase_set("build", root=tmp_path)
        _seed_repo_metadata(tmp_path)
        assert _repo_phase(str(tmp_path)) == "build"

    def test_no_git_repo_is_not_an_error(self, tmp_path: Path) -> None:
        _seed_repo_metadata(tmp_path)
        assert (tmp_path / ".agentalloy" / "README.md").is_file()
        assert not (tmp_path / ".git").exists()  # git-exclude was a no-op

    def test_git_exclude_is_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        _seed_repo_metadata(tmp_path)
        _seed_repo_metadata(tmp_path)
        exclude_lines = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
        assert sum(1 for line in exclude_lines if line.strip() == ".agentalloy/") == 1


class TestStatusHonesty:
    """Bug 3: status classifies user-global files and reports activation."""

    def test_path_scope_flags_global_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        # User-global locations stay flagged "global" — covers --mcp-fallback
        # configs and any migration-leftover hook artifacts.
        assert _path_scope(str(home / ".claude" / "settings.json")) == "global"
        assert _path_scope(str(home / ".agentalloy" / "hooks" / "h.sh")) == "global"
        assert _path_scope(str(tmp_path / "proj" / ".claude" / "settings.json")) == "repo"
        assert _path_scope(None) == "repo"

    def test_repo_phase_reflects_activation(self, tmp_path: Path) -> None:
        assert _repo_phase(str(tmp_path)) is None  # not activated
        run_phase_set("intake", root=tmp_path)
        assert _repo_phase(str(tmp_path)) == "intake"


class TestIntakePhase:
    """The intake phase is a first-class phase that selects its workflow skill."""

    def test_intake_in_valid_phases(self) -> None:
        assert "intake" in VALID_PHASES

    def test_phase_set_intake_writes_workflow(self, tmp_path: Path) -> None:
        result = run_phase_set("intake", root=tmp_path)
        assert result["phase"] == "intake"
        assert result.get("workflow") == "sdd-intake"

    def test_intake_workflow_skill_loads_from_packs(self) -> None:
        from agentalloy.signals.skill_loader import (
            _load_workflow_skill_from_packs,  # pyright: ignore[reportPrivateUsage]
        )

        skill = _load_workflow_skill_from_packs("intake")
        assert skill is not None
        assert skill.get("skill_class") == "workflow"
        assert "intake" in (skill.get("applies_to_phases") or [])
