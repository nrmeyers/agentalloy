# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Adversarial tests for the install module.

Each test asserts the install code refuses or sandboxes a hostile input
that an attacker / compromised dependency / curious user could plausibly
construct. Maps directly to the Critical/High findings remediated in
the adversarial-fix PR.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.install import state as install_state


@pytest.fixture(autouse=True)
def _fake_home_for_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """claude-code wiring (hook default) writes under Path.home() —
    every test in this module must see a throwaway home, or the suite
    pollutes the developer's real ~/.claude/settings.json (tripwire:
    _guard_real_home_wiring in tests/conftest.py)."""
    home = tmp_path / "fake-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture(autouse=True)
def _clean_global_state(tmp_path: Path):
    """Remove global state file after each test to prevent cross-test pollution."""
    fp = install_state.state_path()
    existed = fp.exists()
    original = None
    if existed:
        original = fp.read_text()
    # Start each test with no global state
    if fp.exists():
        fp.unlink()
    yield
    # Restore or remove global state after test
    if fp.exists():
        fp.unlink()
    if existed and original is not None:
        fp.write_text(original)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# Hostile state file
# ---------------------------------------------------------------------------


class TestStateContainment:
    def test_uninstall_skips_path_outside_repo(self, repo_root: Path) -> None:
        """A tampered state file pointing harness_files_written outside the
        repo (e.g. /etc/cron.d/evil) must be skipped, never unlinked."""
        from agentalloy.install.subcommands.uninstall import uninstall

        evil = Path("/etc/cron.d/evil")  # we only assert non-deletion
        st = install_state.load_state(repo_root)
        st["harness_files_written"] = [
            {
                "path": str(evil),
                "action": "wrote_new_file",
                "sentinel_begin": None,
                "sentinel_end": None,
                "content_sha256": "abc",
            }
        ]
        install_state.save_state(st, repo_root)
        result = uninstall(force=True, root=repo_root)
        # File would never have existed; assertion is on the warning path
        # (entry must be skipped, not unlinked).
        assert any(
            "non-harness" in w.lower() or "different repo" in w.lower() for w in result["warnings"]
        )

    def test_self_attesting_repo_root_rejected(self, repo_root: Path) -> None:
        """Even when an entry's `path` is inside its own claimed `repo_root`,
        if both fields come from the (untrusted) state file, the trusted
        cwd-derived bound must reject `/etc/shadow`-style attacks."""
        from agentalloy.install.subcommands.uninstall import uninstall

        st = install_state.load_state(repo_root)
        st["harness_files_written"] = [
            {
                "path": "/etc/shadow",
                "repo_root": "/etc",  # attacker-supplied, would self-validate
                "action": "wrote_new_file",
                "harness": "claude-code",
                "sentinel_begin": None,
                "sentinel_end": None,
                "content_sha256": "abc",
            }
        ]
        install_state.save_state(st, repo_root)
        result = uninstall(force=True, root=repo_root)
        # Entry must be rejected — basename is not a known harness target,
        # AND the path isn't inside the cwd-derived trusted root.
        assert any(
            "non-harness" in w.lower() or "different repo" in w.lower() for w in result["warnings"]
        )
        # `/etc/shadow` must never be visited
        assert all("/etc/shadow" not in str(f) for f in result["files_removed"])

    def test_harness_target_basename_required(self, repo_root: Path) -> None:
        """Even an in-repo path is rejected if it's not a known harness target."""
        from agentalloy.install.subcommands.uninstall import uninstall

        target = repo_root / "subdir" / "evil.txt"
        target.parent.mkdir(parents=True)
        target.write_text("user file")
        st = install_state.load_state(repo_root)
        st["harness_files_written"] = [
            {
                "path": str(target),
                "repo_root": str(repo_root),
                "action": "wrote_new_file",
                "harness": "claude-code",
            }
        ]
        install_state.save_state(st, repo_root)
        uninstall(force=True, root=repo_root)
        assert target.exists(), "non-harness file must not be unlinked"


class TestCorpusSeedAtomicity:
    def test_partial_seed_recovers(self, tmp_path: Path) -> None:
        """A half-written ladybug from an interrupted prior run must not
        block re-seeding: the next call wipes the .part sibling and retries."""
        from agentalloy.install import state as install_state

        # Simulate the post-failure state: a `.part` sibling from a
        # previous interrupted copy still on disk.
        user_corpus = install_state.corpus_dir()
        user_corpus.mkdir(parents=True, exist_ok=True)
        partial = user_corpus / "ladybug.part"
        partial.write_text("interrupted")
        # Stub the bundled corpus into a tmp source so we can fake a real one.
        bundled = tmp_path / "_bundled"
        bundled.mkdir()
        (bundled / "skills.duck").write_text("fake-duck")
        (bundled / "ladybug").write_text("fake-ladybug")
        with patch.object(install_state, "bundled_corpus_dir", return_value=bundled):
            install_state.ensure_corpus_seeded()
        # `.part` from prior failure must be cleaned up; final files present.
        assert not partial.exists()
        assert (user_corpus / "skills.duck").exists()
        assert (user_corpus / "ladybug").exists()


class TestBundledCorpusSentinel:
    def test_empty_dir_not_treated_as_corpus(self, tmp_path: Path) -> None:
        """A `_corpus/` dir that exists but lacks `skills.duck` must NOT be
        used (defends against shadow packages on PYTHONPATH)."""
        from agentalloy.install import state as install_state

        empty = tmp_path / "_corpus"
        empty.mkdir()
        # Direct unit test of the helper used by both code paths.
        assert install_state._is_real_corpus(empty) is False  # pyright: ignore[reportPrivateUsage]
        # And one with the sentinel file IS treated as real.
        (empty / "skills.duck").write_text("x")
        assert install_state._is_real_corpus(empty) is True  # pyright: ignore[reportPrivateUsage]


class TestSchemaMigrationPreservesHarness:
    def test_v1_state_migrates_with_correct_harness(self, repo_root: Path) -> None:
        """v1 state with `harness: 'gemini-cli'` must stamp entries with that
        value, not the fallback `'claude-code'`."""
        from agentalloy.install import state as install_state

        fp = install_state.state_path()
        fp.parent.mkdir(parents=True, exist_ok=True)
        v1 = {
            "schema_version": 1,
            "completed_steps": [],
            "harness": "gemini-cli",
            "repo_root": "/some/repo",
            "harness_files_written": [{"path": "/some/repo/GEMINI.md", "action": "injected_block"}],
        }
        fp.write_text(json.dumps(v1))
        data = install_state.load_state(repo_root)
        assert data["harness_files_written"][0]["harness"] == "gemini-cli"

    def test_uninstall_skips_non_string_path(self, repo_root: Path) -> None:
        from agentalloy.install.subcommands.uninstall import uninstall

        st = install_state.load_state(repo_root)
        st["harness_files_written"] = [
            {"path": ["a", "b"], "action": "wrote_new_file"},  # type: ignore[list-item]
            {"path": None, "action": "wrote_new_file"},
        ]
        install_state.save_state(st, repo_root)
        result = uninstall(root=repo_root)
        # Both entries should be skipped with warnings, not raise.
        assert len([w for w in result["warnings"] if "non-string path" in w]) >= 2


class TestSchemaVersionType:
    def test_non_numeric_schema_version_exits_3(self, repo_root: Path) -> None:
        state_dir = install_state.state_dir(repo_root)
        state_dir.mkdir(parents=True)
        (state_dir / "install-state.json").write_text(json.dumps({"schema_version": "vNEXT"}))
        with pytest.raises(SystemExit) as exc:
            install_state.load_state(repo_root)
        assert exc.value.code == 3

    def test_list_schema_version_exits_3(self, repo_root: Path) -> None:
        state_dir = install_state.state_dir(repo_root)
        state_dir.mkdir(parents=True)
        (state_dir / "install-state.json").write_text(json.dumps({"schema_version": [1]}))
        with pytest.raises(SystemExit) as exc:
            install_state.load_state(repo_root)
        assert exc.value.code == 3


class TestPortValidation:
    def test_string_port_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc:
            install_state.validate_port("1@evil.com:80")
        assert exc.value.code == 2

    def test_negative_port_rejected(self) -> None:
        with pytest.raises(SystemExit):
            install_state.validate_port(-1)

    def test_oversized_port_rejected(self) -> None:
        with pytest.raises(SystemExit):
            install_state.validate_port(70000)

    def test_bool_rejected(self) -> None:
        # bool is an int subclass — explicit guard is needed
        with pytest.raises(SystemExit):
            install_state.validate_port(True)

    def test_valid_port_passes(self) -> None:
        assert install_state.validate_port(8000) == 8000


# ---------------------------------------------------------------------------
# Symlink-safe atomic write
# ---------------------------------------------------------------------------


class TestAtomicWriteSymlink:
    def test_refuses_symlink_at_tmp_path(self, repo_root: Path, tmp_path: Path) -> None:
        """A pre-planted symlink at the .tmp sibling must not redirect the write."""
        target = repo_root / "config.json"
        sink = tmp_path / "redirect-target"
        sink.write_text("untouched")
        tmp = target.with_suffix(target.suffix + ".tmp")
        os.symlink(sink, tmp)
        # The .tmp symlink is unlinked first; the actual write goes to target.
        install_state._atomic_write(target, "real content")  # pyright: ignore[reportPrivateUsage]
        assert target.read_text() == "real content"
        assert sink.read_text() == "untouched"


# ---------------------------------------------------------------------------
# Sentinel forgery
# ---------------------------------------------------------------------------


class TestDuplicateSentinels:
    def test_duplicate_sentinels_rejected(self, repo_root: Path) -> None:
        from agentalloy.install.subcommands.wire_harness import (
            SENTINEL_BEGIN,
            SENTINEL_END,
        )
        from tests._wire_compat import wire_compat

        # Two BEGIN/END pairs in the user's CLAUDE.md
        claude = repo_root / "CLAUDE.md"
        claude.write_text(
            f"# Project\n\n{SENTINEL_BEGIN}\nfirst\n{SENTINEL_END}\n\n"
            f"More content\n\n{SENTINEL_BEGIN}\nsecond\n{SENTINEL_END}\n"
        )
        with pytest.raises(ValueError):
            wire_compat("claude-code", port=8000, root=repo_root, legacy=True)


# ---------------------------------------------------------------------------
# Subprocess option-injection
# ---------------------------------------------------------------------------


class TestPullModelsOptionInjection:
    def test_dash_dash_separator_in_argv(self) -> None:
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands.pull_models import _auto_pull

        with (
            # Skip the new daemon-start probe so we exercise only the
            # pull-command path the test is asserting on.
            patch(
                "agentalloy.install.subcommands.pull_models._ollama_daemon_running",
                return_value=True,
            ),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _auto_pull("ollama", "valid-model")
            args = mock_run.call_args[0][0]
        assert "--" in args
        assert args.index("--") < args.index("valid-model")

    def test_leading_dash_model_rejected(self) -> None:
        from agentalloy.install.subcommands.pull_models import _auto_pull

        result = _auto_pull("ollama", "--insecure-flag")
        assert result["success"] is False
        assert "disallowed" in result["error"]

    def test_shell_metachar_model_rejected(self) -> None:
        from agentalloy.install.subcommands.pull_models import _auto_pull

        for hostile in ("model;rm -rf", "model$(whoami)", "model`id`", "model\nls"):
            result = _auto_pull("ollama", hostile)
            assert result["success"] is False, f"accepted: {hostile!r}"


# ---------------------------------------------------------------------------
# Embedding-runtime URL allowlist
# ---------------------------------------------------------------------------


class TestVerifyUrlAllowlist:
    def test_file_scheme_blocked(self) -> None:
        from agentalloy.install.subcommands.verify import _check_embedding_endpoint_reachable

        result = _check_embedding_endpoint_reachable("file:///etc/passwd")
        assert result["passed"] is False
        assert "scheme" in result["error"]

    def test_javascript_scheme_blocked(self) -> None:
        from agentalloy.install.subcommands.verify import _check_embedding_1024_dim

        result = _check_embedding_1024_dim("javascript:alert(1)", "m")
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# MCP server hostile input
# ---------------------------------------------------------------------------


class TestMcpHostileInput:
    def test_non_dict_params_does_not_crash(self) -> None:
        from agentalloy.install import mcp_server

        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1, 2, 3]}
        resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        # tools/list returns a result; the list-shaped params is coerced to {}
        assert resp is not None
        assert "result" in resp or "error" in resp

    def test_handler_exception_returns_internal_error(self) -> None:
        from agentalloy.install import mcp_server

        with patch.object(
            mcp_server,
            "_handle_tools_list",
            side_effect=RuntimeError("boom"),
        ):
            msg = {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}
            resp = mcp_server._process_message(msg, port=8000)  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        assert resp["error"]["code"] == mcp_server.INTERNAL_ERROR
