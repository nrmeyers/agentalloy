# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Unit tests for the user-facing CLI verbs (setup / wire / unwire / serve / status).

These compose the existing 13-step subcommand surface — tests here
verify the composition behavior, not each underlying step (those have
their own test files).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agentalloy.install import state as install_state


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_empty_install_returns_safe_snapshot(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentalloy.install.subcommands import status

        args = argparse.Namespace(json=True)
        rc = status._run(args)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == 1
        assert out["completed_steps"] == []
        assert out["wired_repos"] == []
        assert out["corpus"]["present"] is False  # bundled corpus blocked by conftest
        assert out["service"]["port"] == 47950

    def test_groups_entries_by_repo_root(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentalloy.install.subcommands import status

        st = install_state.load_state(repo_root)
        st["harness_files_written"] = [
            {
                "path": "/repo-a/CLAUDE.md",
                "repo_root": "/repo-a",
                "harness": "claude-code",
                "action": "injected_block",
            },
            {
                "path": "/repo-a/.cursor/rules/agentalloy.mdc",
                "repo_root": "/repo-a",
                "harness": "cursor",
                "action": "wrote_new_file",
            },
            {
                "path": "/repo-b/GEMINI.md",
                "repo_root": "/repo-b",
                "harness": "gemini-cli",
                "action": "injected_block",
            },
        ]
        install_state.save_state(st, repo_root)
        rc = status._run(argparse.Namespace(json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        repos = {r["repo_root"]: r["entries"] for r in out["wired_repos"]}
        assert set(repos.keys()) == {"/repo-a", "/repo-b"}
        assert len(repos["/repo-a"]) == 2
        assert len(repos["/repo-b"]) == 1

    def test_invalid_port_handled_gracefully(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tampered port shouldn't crash status; surface it as null + unreachable."""
        from agentalloy.install.subcommands import status

        st = install_state.load_state(repo_root)
        st["port"] = "1@evil.com:80"  # type: ignore[assignment]
        install_state.save_state(st, repo_root)
        rc = status._run(argparse.Namespace(json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["service"]["port"] is None
        assert out["service"]["reachable_on_loopback"] is False


# ---------------------------------------------------------------------------
# wire
# ---------------------------------------------------------------------------


class TestWire:
    def test_auto_detects_claude_code(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.install.subcommands import wire

        (repo_root / "CLAUDE.md").write_text("# Project\n")
        fake_home = repo_root / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.chdir(repo_root)
        args = argparse.Namespace(harness=None, port=None, force=False)
        rc = wire._run(args)
        assert rc == 0
        # Sentinels written to ~/.agentalloy/claude-code-env.sh
        env_path = fake_home / ".agentalloy" / "claude-code-env.sh"
        assert env_path.exists()
        content = env_path.read_text()
        assert "agentalloy" in content.lower()

    def test_auto_detects_cursor_when_dir_present(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.install.subcommands import wire

        (repo_root / ".cursor").mkdir()
        monkeypatch.chdir(repo_root)
        args = argparse.Namespace(harness=None, port=None, force=False)
        rc = wire._run(args)
        assert rc == 0
        assert (repo_root / ".cursor" / "rules" / "agentalloy.mdc").exists()

    def test_no_marker_requires_explicit_harness(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.install.subcommands import wire

        monkeypatch.chdir(repo_root)
        args = argparse.Namespace(harness=None, port=None, force=False)
        rc = wire._run(args)
        assert rc == 1

    def test_explicit_harness_wins_over_detection(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.install.subcommands import wire

        # CLAUDE.md present (would auto-detect claude-code) but caller
        # forces gemini-cli — a separate file should be created.
        (repo_root / "CLAUDE.md").write_text("# Project\n")
        monkeypatch.chdir(repo_root)
        args = argparse.Namespace(harness="gemini-cli", port=None, force=False)
        rc = wire._run(args)
        assert rc == 0
        assert (repo_root / "GEMINI.md").exists()


# ---------------------------------------------------------------------------
# unwire
# ---------------------------------------------------------------------------


class TestUnwire:
    def test_removes_only_cwd_repo_entries(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from agentalloy.install.subcommands import unwire, wire

        # Wire the cwd-derived repo
        monkeypatch.chdir(repo_root)
        wire._run(argparse.Namespace(harness="claude-code", port=None, force=False))
        # Inject an entry from another repo into state — unwire must NOT touch it.
        st = install_state.load_state(repo_root)
        other_path = "/some/other-repo/CLAUDE.md"
        st["harness_files_written"].append(
            {
                "path": other_path,
                "repo_root": "/some/other-repo",
                "harness": "claude-code",
                "action": "injected_block",
            }
        )
        install_state.save_state(st, repo_root)
        capsys.readouterr()  # flush wire output
        rc = unwire._run(argparse.Namespace(force=False))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        # cwd ~/.agentalloy/claude-code-env.sh was either modified or removed
        cwd_touched = any(
            "claude-code-env.sh" in f.get("path", "")
            for f in out["files_modified"] + out["files_removed"]
        )
        assert cwd_touched
        # The other-repo entry should have produced a "different repo" warning, not deletion
        assert any("different repo" in w.lower() for w in out["warnings"])

    def test_preserves_user_state_and_env(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """unwire must NOT delete the user-scope state directory or .env.
        Earlier behavior accidentally invoked uninstall's full teardown."""
        from agentalloy.install.subcommands import unwire, wire

        # Set up a wired repo + user-scope artifacts
        monkeypatch.chdir(repo_root)
        wire._run(argparse.Namespace(harness="claude-code", port=None, force=False))
        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("# Generated by agentalloy install write-env\nKEY=val\n")
        state_file = install_state.state_path()
        assert state_file.exists()  # wire wrote to it

        capsys.readouterr()
        unwire._run(argparse.Namespace(force=False))

        # User-scope artifacts must survive unwire
        assert state_file.exists(), "unwire must NOT delete the user state file"
        assert env_path.exists(), "unwire must NOT delete the user .env"
        assert install_state.state_dir().exists(), "unwire must NOT remove the user-scope state dir"


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


class TestServe:
    def test_export_prefix_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`.env` written shell-style with `export KEY=val` is common; the
        parser must strip the prefix or the actual key never gets set."""

        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("export PORT=9999\nexport NAME=agentalloy\n")
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("NAME", raising=False)
        loaded = install_state.load_env_into_environ(env_path)
        assert "PORT" in loaded
        assert "NAME" in loaded
        import os

        assert os.environ["PORT"] == "9999"
        assert os.environ["NAME"] == "agentalloy"
        assert "export PORT" not in os.environ

    def test_loads_env_into_environ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("# header\nFOO=bar\nBAZ='quoted value'\nEMPTY=\nNO_EQUALS_LINE\n")
        # FOO must not be already set in environ for our load to take effect.
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)
        loaded = install_state.load_env_into_environ(env_path)
        assert "FOO" in loaded
        assert "BAZ" in loaded
        assert "EMPTY" in loaded
        import os

        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "quoted value"

    def test_existing_env_var_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key already in the process env must NOT be overridden by .env —
        process env is the higher-priority source."""

        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("FOO=from_env_file\n")
        monkeypatch.setenv("FOO", "from_process")
        loaded = install_state.load_env_into_environ(env_path)
        import os

        assert os.environ["FOO"] == "from_process"
        assert "FOO" not in loaded


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


# TestSetup class removed: the old 11-step composer (subcommands/setup.py)
# was replaced by simple_setup. Tests for the new flow live in
# tests/test_simple_setup.py (18 tests covering prompts, execution,
# argparse registration, and error handling).


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


class TestDispatcherRegistration:
    def test_all_verbs_registered(self) -> None:
        """The new verbs must be dispatched by the top-level CLI parser."""
        from agentalloy.install.__main__ import build_parser

        parser = build_parser()
        # argparse stores subparser names in the choices of the
        # subparsers action — find it.
        sp_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                sp_action = action
                break
        assert sp_action is not None
        registered = set(sp_action.choices.keys())  # pyright: ignore[reportAttributeAccessIssue]
        for verb in ("setup", "wire", "unwire", "serve", "status"):
            assert verb in registered, f"{verb} not registered in dispatcher"
