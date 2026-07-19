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


def _unwire(
    harness: str | None = None, *, all_repos: bool = False, scan: bool = False
) -> argparse.Namespace:
    return argparse.Namespace(
        force=False, json=True, harness=harness, all_repos=all_repos, scan=scan
    )


def _seed_unrecorded_hermes(repo: Path) -> None:
    """Write hermes carriers directly, WITHOUT a state record — the record/reality
    drift (state reset, or wiring git-committed and re-cloned) that makes the
    record-driven unwire a silent no-op."""
    (repo / ".hermes").mkdir(exist_ok=True)
    (repo / ".hermes" / ".agentalloy-env").write_text('export HERMES_HOME="$PWD/.hermes"\n')
    (repo / ".hermes" / "config.yaml").write_text(
        "model:\n  default: agentalloy-proxy\n  provider: custom\n"
        "  base_url: http://localhost:47950/proj/ABC/v1\ntoolsets:\n  - hermes-cli\n"
    )
    (repo / "mise.toml").write_text(
        "# <!-- BEGIN agentalloy install -->\n[env]\n"
        'HERMES_HOME = "{{config_root}}/.hermes"\n# <!-- END agentalloy install -->\n'
    )


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
        # hermes is per-repo: its carrier is the repo-local .hermes/, not a global block.
        assert (repo_root / ".hermes" / "config.yaml").exists()
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
        hermes_cfg = repo_root / ".hermes" / "config.yaml"
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
        # ...Hermes (repo-local carrier) + the shared lifecycle state survive...
        assert hermes_cfg.exists(), "hermes carrier must survive a claude-code unwire"
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

        # Last harness out: the repo-local hermes carrier + lifecycle state + husk go too.
        assert not (repo_root / ".hermes" / "config.yaml").exists()
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
# record/reality drift: unrecorded on-disk wiring + the --scan escape hatch
# ---------------------------------------------------------------------------


class TestUnrecordedDrift:
    def test_unwire_without_record_warns_and_removes_nothing(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        _seed_unrecorded_hermes(repo_root)
        capsys.readouterr()

        rc = unwire._run(_unwire("hermes-agent"))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)

        # Nothing removed (there was no record to drive the walk)...
        assert not out.get("files_removed")
        assert not out.get("files_modified")
        # ...but it is no longer silent: the drift is explained and --scan pointed to.
        assert any(
            "nothing was removed" in w and "--scan" in w for w in out.get("warnings") or []
        ), out.get("warnings")
        # Carriers are untouched without --scan.
        assert (repo_root / ".hermes" / ".agentalloy-env").exists()
        assert (repo_root / "mise.toml").exists()

    def test_scan_removes_self_marking_carriers_but_warns_on_config(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        _seed_unrecorded_hermes(repo_root)
        capsys.readouterr()

        rc = unwire._run(_unwire("hermes-agent", scan=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)

        # Self-marking carriers are gone: the dedicated env file and the now-empty
        # sentinel-only mise.toml.
        assert not (repo_root / ".hermes" / ".agentalloy-env").exists()
        assert not (repo_root / "mise.toml").exists()
        # The ambiguous config (maybe a redirected copy of the user's own) is NEVER
        # guessed at — left in place with a warning.
        assert (repo_root / ".hermes" / "config.yaml").exists()
        assert any("config.yaml" in w for w in out.get("warnings") or []), out.get("warnings")

    def test_surgical_removal_suppresses_the_drift_warning(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # claude-code's proxy carrier is stripped by the surgical sweep in
        # uninstall() even with NO record — so a drifted claude-code repo does
        # remove something. The warning must key off actual removals, not record
        # presence, or the JSON would both list a removal and say "nothing removed".
        monkeypatch.chdir(repo_root)
        settings = repo_root / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:47950/proj/ABC/v1"}})
        )
        capsys.readouterr()

        rc = unwire._run(_unwire("claude-code"))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out.get("files_removed"), "surgical sweep must have removed the proxy carrier"
        assert not any("nothing was removed" in w for w in out.get("warnings") or []), (
            "must not claim nothing removed when it did"
        )

    def test_scan_only_strips_sentinel_block_preserving_user_mise(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(repo_root)
        # A mise.toml with the user's own content plus our sentinel block.
        (repo_root / ".hermes").mkdir()
        (repo_root / ".hermes" / ".agentalloy-env").write_text("export HERMES_HOME=x\n")
        (repo_root / "mise.toml").write_text(
            "[tools]\nnode = '24'\n\n"
            "# <!-- BEGIN agentalloy install -->\n[env]\n"
            'HERMES_HOME = "{{config_root}}/.hermes"\n# <!-- END agentalloy install -->\n'
        )
        capsys.readouterr()

        unwire._run(_unwire("hermes-agent", scan=True))
        assert (repo_root / "mise.toml").exists(), "user content keeps the file alive"
        text = (repo_root / "mise.toml").read_text()
        assert "node = '24'" in text
        assert "agentalloy" not in text and "HERMES_HOME" not in text


# ---------------------------------------------------------------------------
# per-repo isolation: each repo owns an independent hermes carrier
# ---------------------------------------------------------------------------


class TestPerRepoIsolation:
    def test_hermes_carriers_are_independent_per_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        for r in (repo_a, repo_b):
            r.mkdir()
            (r / "pyproject.toml").write_text("")

        monkeypatch.chdir(repo_a)
        wire._run(_wire("hermes-agent"))
        monkeypatch.chdir(repo_b)
        wire._run(_wire("hermes-agent"))
        capsys.readouterr()

        cfg_a = repo_a / ".hermes" / "config.yaml"
        cfg_b = repo_b / ".hermes" / "config.yaml"
        assert cfg_a.exists() and cfg_b.exists()
        # Each carries its own /proj/<token>, so the two configs differ.
        assert cfg_a.read_text() != cfg_b.read_text()

        # Unwiring hermes from repo A must NOT touch repo B's independent carrier.
        monkeypatch.chdir(repo_a)
        unwire._run(_unwire("hermes-agent"))
        assert not cfg_a.exists(), "repo A's carrier is removed"
        assert cfg_b.exists(), "repo B's carrier is untouched"
