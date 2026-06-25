# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Per-harness wire/unwire: a repo can carry several harnesses, and each can be
wired or unwired on its own without disturbing the others or the shared
SDD lifecycle state.

Covers the multi-harness requirement (run Claude Code + Hermes against the same
repos): `wire --harness a --harness b` stacks, `wire --list` reports, and
`unwire --harness <name>` tears down exactly one harness — keeping the repo's
`.agentalloy/{phase,config}` alive while any other harness still owns the repo,
and only removing a harness's shared user-scope config on the last repo out.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import unwire, wire


@pytest.fixture(autouse=True)
def _fake_home_for_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermes writes ~/.hermes/config.yaml and claude-code touches ~/.claude — run
    every test against a throwaway home so the suite never pollutes the
    developer's real dotfiles (tripwire: _guard_real_home_wiring in conftest)."""
    home = tmp_path / "fake-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


def _wire(harness: object, **over: object) -> argparse.Namespace:
    base: dict[str, object] = {"harness": harness, "port": None, "force": False, "json": True}
    base.update(over)
    return argparse.Namespace(**base)


def _unwire(harness: str | None = None, *, all_repos: bool = False) -> argparse.Namespace:
    return argparse.Namespace(force=False, json=True, harness=harness, all_repos=all_repos)


def _block_present(p: Path) -> bool:
    return p.exists() and "BEGIN agentalloy" in p.read_text()


def _wired_harnesses(repo: Path) -> set[str]:
    st = install_state.load_state(repo)
    return {
        e.get("harness")
        for e in st.get("harness_files_written", [])
        if isinstance(e, dict) and e.get("harness")
    }


# ---------------------------------------------------------------------------
# wire: multiple harnesses + --list
# ---------------------------------------------------------------------------


class TestWireMultiple:
    def test_repeated_harness_wires_both(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        rc = wire._run(_wire(["claude-code", "hermes-agent"]))
        assert rc == 0
        assert (repo_root / ".claude" / "settings.local.json").exists()
        assert _block_present(Path.home() / ".hermes" / "config.yaml")
        assert _wired_harnesses(repo_root) == {"claude-code", "hermes-agent"}

    def test_comma_separated_harness_wires_both(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo_root)
        rc = wire._run(_wire("claude-code,hermes-agent"))
        assert rc == 0
        assert _wired_harnesses(repo_root) == {"claude-code", "hermes-agent"}

    def test_unknown_harness_errors_without_wiring(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(repo_root)
        rc = wire._run(_wire("not-a-harness"))
        assert rc == 1
        assert _wired_harnesses(repo_root) == set()

    def test_list_reports_wired_harnesses(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        wire._run(_wire(["claude-code", "hermes-agent"]))
        capsys.readouterr()  # flush wire output
        rc = wire._run(argparse.Namespace(list_wired=True, json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert sorted(out["harnesses"]) == ["claude-code", "hermes-agent"]
        assert out["lifecycle_mode"] == "full"
        assert out["phase"] == "intake"


# ---------------------------------------------------------------------------
# unwire --harness <name>
# ---------------------------------------------------------------------------


class TestUnwireSingleHarness:
    def test_unwire_one_preserves_other_and_lifecycle(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        wire._run(_wire(["claude-code", "hermes-agent"]))
        hermes_cfg = Path.home() / ".hermes" / "config.yaml"
        phase = repo_root / ".agentalloy" / "phase"
        config = repo_root / ".agentalloy" / "config"
        assert phase.exists() and config.exists()
        capsys.readouterr()

        rc = unwire._run(_unwire("claude-code"))
        assert rc == 0

        # Claude carriers are gone...
        assert not (repo_root / ".claude" / "settings.local.json").exists()
        assert not (repo_root / ".agentalloy" / "claude-code-env.sh").exists()
        assert not (repo_root / ".claude" / "CLAUDE.md").exists()
        # ...Hermes + the shared lifecycle state survive...
        assert _block_present(hermes_cfg), "hermes carrier must survive a claude-code unwire"
        assert phase.exists(), "lifecycle phase must survive while hermes remains wired"
        assert config.exists()
        # ...and state now lists only the remaining harness.
        assert _wired_harnesses(repo_root) == {"hermes-agent"}

    def test_unwire_last_harness_removes_lifecycle(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        wire._run(_wire(["claude-code", "hermes-agent"]))
        capsys.readouterr()

        unwire._run(_unwire("claude-code"))
        unwire._run(_unwire("hermes-agent"))

        # Last harness out: the shared lifecycle state + empty husk go too.
        assert not _block_present(Path.home() / ".hermes" / "config.yaml")
        assert not (repo_root / ".agentalloy" / "phase").exists()
        assert not (repo_root / ".agentalloy").exists()
        assert _wired_harnesses(repo_root) == set()

    def test_unwire_one_preserves_user_contracts(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        wire._run(_wire(["claude-code", "hermes-agent"]))
        contract = repo_root / ".agentalloy" / "contracts" / "spec" / "keep.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("# user's contract\n")
        capsys.readouterr()

        unwire._run(_unwire("hermes-agent"))  # remove the proxy-only harness
        assert contract.exists(), "user contracts are never touched by unwire"


# ---------------------------------------------------------------------------
# shared user-scope config: last repo out
# ---------------------------------------------------------------------------


class TestSharedUserScopeLastRepoOut:
    def test_shared_hermes_config_survives_until_last_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        for r in (repo_a, repo_b):
            r.mkdir()
            (r / "pyproject.toml").write_text("")
        hermes_cfg = Path.home() / ".hermes" / "config.yaml"

        monkeypatch.chdir(repo_a)
        wire._run(_wire("hermes-agent"))
        monkeypatch.chdir(repo_b)
        wire._run(_wire("hermes-agent"))
        assert _block_present(hermes_cfg)
        capsys.readouterr()

        # Unwiring hermes from repo A must NOT pull the shared config out from
        # under repo B.
        monkeypatch.chdir(repo_a)
        unwire._run(_unwire("hermes-agent"))
        assert _block_present(hermes_cfg), "shared config must survive while repo B wires hermes"

        # Repo B is the last one out — now the shared config goes.
        monkeypatch.chdir(repo_b)
        unwire._run(_unwire("hermes-agent"))
        assert not _block_present(hermes_cfg), "last repo out removes the shared config"
