# ruff: noqa: I001, E501 -- private member imports; long lines for test data
"""Tests for backup/restore functionality (original_content in install/uninstall)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch


from agentalloy.install.subcommands.wire_harness import (
    _build_result,
    _capture_original,
)


# ---------------------------------------------------------------------------
# _capture_original
# ---------------------------------------------------------------------------


class TestCaptureOriginal:
    """Tests for _capture_original helper."""

    def test_captures_existing_file(self, tmp_path: Path) -> None:
        """_capture_original returns file content when file exists."""
        f = tmp_path / "test.txt"
        f.write_text("original content\n")
        assert _capture_original(f) == "original content\n"

    def test_returns_none_for_new_file(self, tmp_path: Path) -> None:
        """_capture_original returns None when file doesn't exist."""
        f = tmp_path / "new_file.txt"
        assert _capture_original(f) is None


# ---------------------------------------------------------------------------
# _build_result merge
# ---------------------------------------------------------------------------


class TestBuildResultMerge:
    """Tests for _build_result merge preserving original_content."""

    def test_preserves_original_on_rewire(self, tmp_path: Path) -> None:
        """On re-wire, _build_result preserves original_content from prior entry."""
        prior = [
            {
                "path": str(tmp_path / "config.json"),
                "action": "injected_block",
                "original_content": '{"key": "original"}\n',
                "content_sha256": "sha1",
            }
        ]
        new_entries = [
            {
                "path": str(tmp_path / "config.json"),
                "action": "injected_block",
                "content_sha256": "sha2",
            }
        ]
        st = {"harness_files_written": prior}

        with (
            patch("agentalloy.install.state.load_state", return_value=st),
            patch("agentalloy.install.state.record_step", return_value=st),
            patch("agentalloy.install.state.save_state"),
        ):
            result = _build_result(
                harness="claude-code",
                vector="markdown",
                files_written=new_entries,
                root=tmp_path,
            )
        entry = result["files_written"][0]
        assert entry["original_content"] == '{"key": "original"}\n'

    def test_does_not_overwrite_new_original(self, tmp_path: Path) -> None:
        """If new entry already has original_content, prior is not used."""
        prior = [
            {
                "path": str(tmp_path / "config.json"),
                "action": "injected_block",
                "original_content": "prior original\n",
                "content_sha256": "sha1",
            }
        ]
        new_entries = [
            {
                "path": str(tmp_path / "config.json"),
                "action": "injected_block",
                "original_content": "new original\n",
                "content_sha256": "sha2",
            }
        ]
        st = {"harness_files_written": prior}

        with (
            patch("agentalloy.install.state.load_state", return_value=st),
            patch("agentalloy.install.state.record_step", return_value=st),
            patch("agentalloy.install.state.save_state"),
        ):
            result = _build_result(
                harness="claude-code",
                vector="markdown",
                files_written=new_entries,
                root=tmp_path,
            )
        entry = result["files_written"][0]
        assert entry["original_content"] == "new original\n"


# ---------------------------------------------------------------------------
# Uninstall restore — unit tests for the harness loop logic
# ---------------------------------------------------------------------------

# Uninstall module namespace for patching (uninstall.py imports as install_state)
_UNINSTALL = "agentalloy.install.subcommands.uninstall"


class TestUninstallRestoreLogic:
    """Unit tests for uninstall harness loop restoring original_content.

    We isolate the harness loop logic by mocking state loading and proxy
    unwiring, so tests run in isolation against tmp_path files only.
    """

    def _uninstall(
        self,
        st: dict[str, Any],
        root: Path,
        force: bool = False,
    ) -> dict[str, Any]:
        """Call uninstall() with state and proxy mocks applied."""
        from agentalloy.install.subcommands.uninstall import uninstall

        with (
            patch(f"{_UNINSTALL}.install_state.load_state", return_value=st),
            patch(f"{_UNINSTALL}.install_state.save_state"),
            patch(f"{_UNINSTALL}.install_state.is_inside_root", return_value=True),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_aider", return_value=[]),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_opencode", return_value=[]),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_cline", return_value=[]),
        ):
            return uninstall(
                remove_data=False,
                force=force,
                root=root,
                remove_user_state=False,
                remove_env=False,
                all_repos=True,
                remove_models=False,
                remove_wiring=True,
                stop_services=False,
            )

    def test_restores_original_content(self, tmp_path: Path) -> None:
        """Uninstall restores original content when present in state."""
        config_file = tmp_path / "CLAUDE.md"
        original = "# My Claude Config\n\nOriginal content here.\n"
        config_file.write_text(original)

        st = {
            "harness_files_written": [
                {
                    "path": str(config_file),
                    "action": "injected_block",
                    "sentinel_begin": "<!-- BEGIN -->",
                    "sentinel_end": "<!-- END -->",
                    "original_content": original,
                    "content_sha256": "sha1",
                }
            ]
        }

        # Now file has modified content
        config_file.write_text("# My Claude Config\n\n<!-- BEGIN -->\nInjected\n<!-- END -->\n")

        result = self._uninstall(st, tmp_path)

        assert config_file.read_text() == original
        assert any(
            r.get("path") == str(config_file) and r.get("action") == "restored_original"
            for r in result.get("files_modified", [])
        )

    def test_deletes_new_files_without_original(self, tmp_path: Path) -> None:
        """Uninstall deletes files that had no original_content (were new)."""
        new_file = tmp_path / "CLAUDE.md"
        new_file.write_text("<!-- BEGIN -->\nInjected\n<!-- END -->\n")

        st = {
            "harness_files_written": [
                {
                    "path": str(new_file),
                    "action": "wrote_new_file",
                    "content_sha256": "sha1",
                }
            ]
        }

        result = self._uninstall(st, tmp_path)

        assert not new_file.exists()
        assert any(
            r.get("path") == str(new_file) and r.get("action") == "deleted_dedicated_file"
            for r in result.get("files_removed", [])
        )

    def test_fallback_to_sentinel_stripping(self, tmp_path: Path) -> None:
        """Legacy state without original_content falls back to sentinel stripping."""
        config_file = tmp_path / "CLAUDE.md"
        content = "user content\n<!-- BEGIN -->\ninjected block\n<!-- END -->\nmore user content\n"
        config_file.write_text(content)

        st = {
            "harness_files_written": [
                {
                    "path": str(config_file),
                    "action": "injected_block",
                    "sentinel_begin": "<!-- BEGIN -->",
                    "sentinel_end": "<!-- END -->",
                    "content_sha256": "sha1",
                    "repo_root": str(tmp_path),
                }
            ]
        }

        # Use force=True to strip sentinels even with SHA mismatch
        self._uninstall(st, tmp_path, force=True)

        restored = config_file.read_text()
        assert "<!-- BEGIN -->" not in restored
        assert "injected block" not in restored
        assert "user content" in restored


# ---------------------------------------------------------------------------
# .env backup
# ---------------------------------------------------------------------------


class TestEnvBackup:
    """Tests for .env backup in write_env."""

    def test_write_env_backs_up_original(self, tmp_path: Path) -> None:
        """write_env captures original .env content before first write."""
        from agentalloy.install.subcommands.write_env import write_env

        env_file = tmp_path / ".env"
        original = "EXISTING_VAR=value\n"
        env_file.write_text(original)

        # Mock real load_state shape: env_original_content is None by default
        empty_st = {"env_original_content": None}

        with (
            patch(
                "agentalloy.install.subcommands.write_env.install_state.env_path",
                return_value=env_file,
            ),
            patch(
                "agentalloy.install.subcommands.write_env.install_state.load_state",
                return_value=empty_st,
            ),
            patch("agentalloy.install.subcommands.write_env.install_state.save_state") as mock_save,
        ):
            write_env(preset="cpu", port=47950, force=True)
            mock_save.assert_called_once()
            state_arg = mock_save.call_args[0][0]
            assert state_arg.get("env_original_content") == original

    def test_write_env_does_not_overwrite_existing_backup(self, tmp_path: Path) -> None:
        """write_env doesn't overwrite existing env_original_content in state."""
        from agentalloy.install.subcommands.write_env import write_env

        env_file = tmp_path / ".env"
        env_file.write_text("CURRENT_VAR=value\n")

        with (
            patch(
                "agentalloy.install.subcommands.write_env.install_state.env_path",
                return_value=env_file,
            ),
            patch(
                "agentalloy.install.subcommands.write_env.install_state.load_state",
                return_value={"env_original_content": "PRIOR_BACKUP\n"},
            ),
            patch("agentalloy.install.subcommands.write_env.install_state.save_state") as mock_save,
        ):
            write_env(preset="cpu", port=47950, force=True)
            # save_state should NOT be called when backup already exists
            mock_save.assert_not_called()
