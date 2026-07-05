"""Code-index harness block: render/idempotency, legacy migration, unwire, purge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentalloy.install import code_index_wiring as ciw

LEGACY_BLOCK = (
    f"{ciw.LEGACY_SENTINEL_BEGIN}\n"
    "## codebase-indexer — code intelligence for this repo\n"
    "old daemon block\n"
    f"{ciw.LEGACY_SENTINEL_END}\n"
)


def _fake_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ciw, "repo_slug", lambda root: "org__repo")


class TestWireBlock:
    def test_writes_block_into_existing_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        target = tmp_path / "CLAUDE.md"
        target.write_text("# My repo\n")
        actions = ciw.wire_code_index_block(tmp_path, 47950)
        content = target.read_text()
        assert content.startswith("# My repo\n")
        assert content.count(ciw.SENTINEL_BEGIN) == 1
        assert content.count(ciw.SENTINEL_END) == 1
        assert "org__repo" in content
        assert "http://127.0.0.1:47950/code" in content
        assert "agentalloy code search" in content
        assert actions[-1]["action"] == "injected_block"

    def test_rewire_is_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# My repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        first = (tmp_path / "CLAUDE.md").read_text()
        actions = ciw.wire_code_index_block(tmp_path, 47950)
        assert (tmp_path / "CLAUDE.md").read_text() == first
        assert actions[-1]["action"] == "updated_block"

    def test_rewire_updates_port_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# My repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        ciw.wire_code_index_block(tmp_path, 55555)
        content = (tmp_path / "CLAUDE.md").read_text()
        assert "55555/code" in content
        assert "47950" not in content
        assert content.count(ciw.SENTINEL_BEGIN) == 1

    def test_creates_claude_md_when_no_marker_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        ciw.wire_code_index_block(tmp_path, 47950)
        assert (tmp_path / "CLAUDE.md").exists()

    def test_cursor_repo_gets_dedicated_mdc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".cursor").mkdir()
        ciw.wire_code_index_block(tmp_path, 47950)
        assert (tmp_path / ".cursor/rules/agentalloy-code-index.mdc").exists()


class TestLegacyMigration:
    def test_legacy_block_replaced_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        target = tmp_path / "AGENTS.md"
        target.write_text(f"# Agents\n\n{LEGACY_BLOCK}\nuser content below\n")
        actions = ciw.wire_code_index_block(tmp_path, 47950)
        content = target.read_text()
        assert ciw.LEGACY_SENTINEL_BEGIN not in content
        assert ciw.SENTINEL_BEGIN in content  # new block landed in the SAME file
        assert "user content below" in content
        assert any(a["action"] == "replaced_legacy_codebase_indexer_block" for a in actions)

    def test_legacy_dedicated_mdc_deleted_and_new_mdc_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        legacy = tmp_path / ".cursor/rules/codebase-indexer.mdc"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(LEGACY_BLOCK)
        ciw.wire_code_index_block(tmp_path, 47950)
        assert not legacy.exists()
        assert (tmp_path / ".cursor/rules/agentalloy-code-index.mdc").exists()


class TestRemoveBlocks:
    def test_removes_new_and_legacy_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        (tmp_path / "GEMINI.md").write_text(LEGACY_BLOCK)
        actions = ciw.remove_code_index_blocks(tmp_path)
        assert ciw.SENTINEL_BEGIN not in (tmp_path / "CLAUDE.md").read_text()
        assert ciw.LEGACY_SENTINEL_BEGIN not in (tmp_path / "GEMINI.md").read_text()
        assert (tmp_path / "CLAUDE.md").read_text().startswith("# Repo")
        assert len(actions) == 2

    def test_remove_is_idempotent_and_leaves_clean_repo_alone(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        assert ciw.remove_code_index_blocks(tmp_path) == []
        assert (tmp_path / "CLAUDE.md").read_text() == "# Repo\n"

    def test_dedicated_mdc_is_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".cursor").mkdir()
        ciw.wire_code_index_block(tmp_path, 47950)
        ciw.remove_code_index_blocks(tmp_path)
        assert not (tmp_path / ".cursor/rules/agentalloy-code-index.mdc").exists()


class TestMaybeWire:
    def test_enabled_module_writes_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        monkeypatch.setattr(ciw, "service_module_status", lambda port: "enabled")
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        actions = ciw.maybe_wire(tmp_path, 47950, quiet=True)
        assert actions
        assert ciw.SENTINEL_BEGIN in (tmp_path / "CLAUDE.md").read_text()

    @pytest.mark.parametrize("status", ["disabled", "unavailable", None])
    def test_not_enabled_removes_stale_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        monkeypatch.setattr(ciw, "service_module_status", lambda port: status)
        ciw.maybe_wire(tmp_path, 47950, quiet=True)
        assert ciw.SENTINEL_BEGIN not in (tmp_path / "CLAUDE.md").read_text()

    def test_not_enabled_pristine_repo_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ciw, "service_module_status", lambda port: None)
        assert ciw.maybe_wire(tmp_path, 47950, quiet=True) == []
        assert not (tmp_path / "CLAUDE.md").exists()


class TestUnwireSweep:
    def test_unwire_repo_local_removes_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_state_dir: tuple[Path, Path]
    ) -> None:
        from agentalloy.install.subcommands.uninstall import (
            _unwire_repo_local,  # pyright: ignore[reportPrivateUsage]
        )

        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        _, files_removed = _unwire_repo_local(tmp_path, set())
        assert ciw.SENTINEL_BEGIN not in (tmp_path / "CLAUDE.md").read_text()
        assert any(r["action"] == "removed_block" for r in files_removed)

    def test_per_harness_unwire_keeps_block_while_other_harness_remains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_state_dir: tuple[Path, Path]
    ) -> None:
        from agentalloy.install.subcommands.uninstall import (
            _unwire_repo_local,  # pyright: ignore[reportPrivateUsage]
        )

        _fake_slug(monkeypatch)
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        _unwire_repo_local(tmp_path, set(), harness="claude-code", remove_lifecycle=False)
        assert ciw.SENTINEL_BEGIN in (tmp_path / "CLAUDE.md").read_text()


class TestUninstallPurge:
    def test_remove_data_purges_code_index_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_state_dir: tuple[Path, Path]
    ) -> None:
        from agentalloy.install.subcommands.uninstall import uninstall

        ci_dir = tmp_path / "ci-data"
        (ci_dir / "repos" / "org__repo").mkdir(parents=True)
        monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(ci_dir))
        result: dict[str, Any] = uninstall(
            remove_data=True,
            root=tmp_path / "repo",
            remove_user_state=False,
            remove_env=False,
            remove_wiring=False,
            stop_services=False,
        )
        assert not ci_dir.exists()
        assert any(r["action"] == "deleted_code_index_data_dir" for r in result["files_removed"])

    def test_keep_data_leaves_code_index_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_state_dir: tuple[Path, Path]
    ) -> None:
        from agentalloy.install.subcommands.uninstall import uninstall

        ci_dir = tmp_path / "ci-data"
        ci_dir.mkdir()
        monkeypatch.setenv("CODE_INDEX_DATA_DIR", str(ci_dir))
        uninstall(
            remove_data=False,
            root=tmp_path / "repo",
            remove_user_state=False,
            remove_env=False,
            remove_wiring=False,
            stop_services=False,
        )
        assert ci_dir.exists()


class TestOfferIndex:
    """Wire offers to index an unregistered repo (feature: wire-index offer)."""

    def _seams(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        slugs: list[str] | None,
        job: dict[str, Any] | None = None,
    ) -> list[Path]:
        """Patch the registry/submit seams; returns the submit-call record."""
        _fake_slug(monkeypatch)
        submitted: list[Path] = []

        def _submit(port: int, repo_path: Path) -> dict[str, Any] | None:
            submitted.append(repo_path)
            return job if job is not None else {"id": "j1", "slug": "org__repo"}

        monkeypatch.setattr(ciw, "registry_slugs", lambda port: slugs)
        monkeypatch.setattr(ciw, "submit_index_job", _submit)
        return submitted

    def _tty(self, monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
        import sys as _sys

        monkeypatch.setattr(_sys.stdin, "isatty", lambda: True)
        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(it))

    def test_tty_accept_submits_and_points_at_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=[])
        self._tty(monkeypatch, [""])  # default answer is yes
        job = ciw.offer_index(tmp_path, 47950)
        assert job is not None and job["id"] == "j1"
        assert submitted == [tmp_path]
        err = capsys.readouterr().err
        assert "index job started (id=j1)" in err
        assert "agentalloy code status" in err

    def test_tty_decline_skips_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=[])
        self._tty(monkeypatch, ["n"])
        assert ciw.offer_index(tmp_path, 47950) is None
        assert submitted == []

    def test_non_tty_defaults_to_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys as _sys

        submitted = self._seams(monkeypatch, slugs=[])
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        assert ciw.offer_index(tmp_path, 47950) is not None
        assert submitted == [tmp_path]

    def test_assume_yes_skips_prompt_on_tty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=[])
        self._tty(monkeypatch, [])  # any input() call would raise StopIteration
        assert ciw.offer_index(tmp_path, 47950, assume_yes=True) is not None
        assert submitted == [tmp_path]

    def test_already_registered_repo_not_offered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=["org__repo"])
        assert ciw.offer_index(tmp_path, 47950, assume_yes=True) is None
        assert submitted == []

    def test_service_unreachable_hints_and_wiring_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=None)  # registry unreachable
        monkeypatch.setattr(ciw, "service_module_status", lambda port: "enabled")
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        actions = ciw.maybe_wire(tmp_path, 47950, quiet=True)
        assert actions  # the block still landed — wiring succeeded
        assert ciw.SENTINEL_BEGIN in (tmp_path / "CLAUDE.md").read_text()
        assert submitted == []
        assert "index later with `agentalloy code index`" in capsys.readouterr().err

    def test_maybe_wire_offers_after_block_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=[])
        monkeypatch.setattr(ciw, "service_module_status", lambda port: "enabled")
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        ciw.maybe_wire(tmp_path, 47950, quiet=True, assume_yes=True)
        assert submitted == [tmp_path]

    def test_maybe_wire_disabled_module_never_offers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submitted = self._seams(monkeypatch, slugs=[])
        monkeypatch.setattr(ciw, "service_module_status", lambda port: "disabled")
        ciw.maybe_wire(tmp_path, 47950, quiet=True, assume_yes=True)
        assert submitted == []
