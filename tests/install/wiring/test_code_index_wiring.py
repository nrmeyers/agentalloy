"""Code-index harness block: render/idempotency, legacy migration, unwire, purge."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install import code_index_wiring as ciw
from agentalloy.install.subcommands.wire import (
    _code_index_harness,  # pyright: ignore[reportPrivateUsage]
)

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

    def test_windsurf_dedicated_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".windsurf").mkdir()
        ciw.wire_code_index_block(tmp_path, 47950)
        assert (tmp_path / ".windsurf/rules/agentalloy.md").exists()
        content = (tmp_path / ".windsurf/rules/agentalloy.md").read_text()
        assert ciw.SENTINEL_BEGIN in content
        assert ciw.SENTINEL_END in content

    def test_windsurf_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".windsurfrules").write_text("# Rules\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        assert (tmp_path / ".windsurfrules").exists()
        content = (tmp_path / ".windsurfrules").read_text()
        assert ciw.SENTINEL_BEGIN in content

    def test_github_copilot_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".github").mkdir(parents=True)
        (tmp_path / ".github/copilot-instructions.md").write_text("# Copilot\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        content = (tmp_path / ".github/copilot-instructions.md").read_text()
        assert ciw.SENTINEL_BEGIN in content

    def test_aider_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".agentalloy-aider-instructions.md").write_text("# Aider\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        content = (tmp_path / ".agentalloy-aider-instructions.md").read_text()
        assert ciw.SENTINEL_BEGIN in content

    def test_opencode_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / ".opencode").mkdir(parents=True)
        (tmp_path / ".opencode/system-prompt.md").write_text("# OpenCode\n")
        ciw.wire_code_index_block(tmp_path, 47950)
        content = (tmp_path / ".opencode/system-prompt.md").read_text()
        assert ciw.SENTINEL_BEGIN in content


class TestDetectTargetHarness:
    """Harness-aware target detection for each harness type."""

    def test_codex_returns_none_no_dedicated_carrier(self, tmp_path: Path) -> None:
        """codex has no dedicated carrier (proxy-wired) → None when no shared target."""
        assert ciw.detect_target(tmp_path, harness="codex") is None

    def test_codex_returns_none_even_when_shared_target_exists(self, tmp_path: Path) -> None:
        """codex is proxy-only — no code-index block even if CLAUDE.md exists."""
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        assert ciw.detect_target(tmp_path, harness="codex") is None

    def test_qwen_code_returns_none_no_dedicated_carrier(self, tmp_path: Path) -> None:
        """qwen-code has no dedicated carrier (proxy-wired) → None when no shared target."""
        assert ciw.detect_target(tmp_path, harness="qwen-code") is None

    def test_qwen_code_returns_none_even_when_shared_target_exists(self, tmp_path: Path) -> None:
        """qwen-code is proxy-only — no code-index block even if AGENTS.md exists."""
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        assert ciw.detect_target(tmp_path, harness="qwen-code") is None

    def test_claude_code_returns_none_even_when_claude_md_exists(self, tmp_path: Path) -> None:
        """claude-code is proxy-only — no code-index block even if CLAUDE.md exists."""
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        assert ciw.detect_target(tmp_path, harness="claude-code") is None

    def test_hermes_agent_returns_none_without_agents(self, tmp_path: Path) -> None:
        """hermes-agent maps to AGENTS.md; returns None when it doesn't exist."""
        assert ciw.detect_target(tmp_path, harness="hermes-agent") is None

    def test_hermes_agent_returns_agents_md(self, tmp_path: Path) -> None:
        """hermes-agent returns AGENTS.md when it exists."""
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        assert ciw.detect_target(tmp_path, harness="hermes-agent") == tmp_path / "AGENTS.md"

    def test_openclaw_returns_none(self, tmp_path: Path) -> None:
        """openclaw is home-scoped, so detect_target returns None."""
        assert ciw.detect_target(tmp_path, harness="openclaw") is None

    def test_continue_closed_returns_none(self, tmp_path: Path) -> None:
        """continue-closed is home-scoped, so detect_target returns None."""
        assert ciw.detect_target(tmp_path, harness="continue-closed") is None

    def test_continue_local_returns_none(self, tmp_path: Path) -> None:
        """continue-local is home-scoped, so detect_target returns None."""
        assert ciw.detect_target(tmp_path, harness="continue-local") is None

    def test_copilot_cli_returns_shared_target(self, tmp_path: Path) -> None:
        """copilot-cli falls back to shared targets."""
        (tmp_path / ".github").mkdir(parents=True)
        (tmp_path / ".github/copilot-instructions.md").write_text("# Copilot\n")
        assert (
            ciw.detect_target(tmp_path, harness="copilot-cli")
            == tmp_path / ".github/copilot-instructions.md"
        )

    def test_copilot_cli_no_target_returns_none(self, tmp_path: Path) -> None:
        """copilot-cli with no shared target returns None."""
        assert ciw.detect_target(tmp_path, harness="copilot-cli") is None

    def test_cursor_with_dedicated_returns_none(self, tmp_path: Path) -> None:
        """cursor without .cursor/rules returns None (shared targets only)."""
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        assert ciw.detect_target(tmp_path, harness="cursor") == tmp_path / "CLAUDE.md"

    def test_windsurf_with_dedicated_returns_none(self, tmp_path: Path) -> None:
        """windsurf without .windsurf/ returns None (shared targets only)."""
        (tmp_path / ".windsurfrules").write_text("# Rules\n")
        assert ciw.detect_target(tmp_path, harness="windsurf") == tmp_path / ".windsurfrules"

    def test_no_harness_defaults_to_claude_md(self, tmp_path: Path) -> None:
        """No harness → CLAUDE.md as default."""
        (tmp_path / "CLAUDE.md").write_text("# Repo\n")
        assert ciw.detect_target(tmp_path) == tmp_path / "CLAUDE.md"

    def test_cline_returns_shared_target(self, tmp_path: Path) -> None:
        """cline falls back to shared targets like .clinerules."""
        (tmp_path / ".clinerules").write_text("# Rules\n")
        assert ciw.detect_target(tmp_path, harness="cline") == tmp_path / ".clinerules"

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

    def test_new_targets_are_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """remove_code_index_blocks sweeps all _CANDIDATE_TARGETS including new ones."""
        _fake_slug(monkeypatch)

        # write_block creates the sentinel block — we manually write blocks to
        # each new target because detect_target() picks only the highest-priority
        # target. The remove function sweeps ALL _CANDIDATE_TARGETS on disk.
        block_content = f"# Repo\n{ciw.SENTINEL_BEGIN}\ncode-index block\n{ciw.SENTINEL_END}\n"
        targets = [
            tmp_path / ".windsurf/rules/agentalloy.md",
            tmp_path / ".windsurfrules",
            tmp_path / ".github/copilot-instructions.md",
            tmp_path / ".agentalloy-aider-instructions.md",
            tmp_path / ".opencode/system-prompt.md",
        ]
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(block_content)

        actions = ciw.remove_code_index_blocks(tmp_path)
        for target in targets:
            content = target.read_text()
            assert ciw.SENTINEL_BEGIN not in content
            assert ciw.LEGACY_SENTINEL_BEGIN not in content
            assert "# Repo" in content
        # All 5 targets had blocks removed
        assert len(actions) == 5
        assert all(a["action"] == "removed_block" for a in actions)


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

    def test_already_active_job_points_at_status_not_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A 409 'already active' from the service is not a start failure (B2)."""
        import sys as _sys

        submitted = self._seams(monkeypatch, slugs=[], job={"already_active": True, "job_id": "j9"})
        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        job = ciw.offer_index(tmp_path, 47950)
        assert job is not None and job["already_active"] is True
        assert submitted == [tmp_path]
        err = capsys.readouterr().err
        assert "already active (id=j9)" in err
        assert "agentalloy code status" in err
        assert "could not start" not in err


class TestDetectTarget:
    """detect_target() resolves the correct file for each harness target."""

    def test_windsurf_dedicated_path(self, tmp_path: Path) -> None:
        """When .windsurf/ exists, returns the dedicated file."""
        (tmp_path / ".windsurf").mkdir()
        assert ciw.detect_target(tmp_path) == tmp_path / ".windsurf/rules/agentalloy.md"

    def test_windsurf_fallback_no_windsurf_dir(self, tmp_path: Path) -> None:
        """When only .windsurfrules exists, returns the shared fallback."""
        (tmp_path / ".windsurfrules").write_text("# Rules\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".windsurfrules"

    def test_windsurf_dedicated_takes_priority_over_fallback(self, tmp_path: Path) -> None:
        """When both exist, dedicated file wins."""
        (tmp_path / ".windsurf").mkdir()
        (tmp_path / ".windsurfrules").write_text("# Rules\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".windsurf/rules/agentalloy.md"

    def test_github_copilot_target(self, tmp_path: Path) -> None:
        """When .github/copilot-instructions.md exists, returns it."""
        (tmp_path / ".github").mkdir(parents=True)
        (tmp_path / ".github/copilot-instructions.md").write_text("# Copilot\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".github/copilot-instructions.md"

    def test_aider_target(self, tmp_path: Path) -> None:
        """When .agentalloy-aider-instructions.md exists, returns it."""
        (tmp_path / ".agentalloy-aider-instructions.md").write_text("# Aider\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".agentalloy-aider-instructions.md"

    def test_opencode_target(self, tmp_path: Path) -> None:
        """When .opencode/system-prompt.md exists, returns it."""
        (tmp_path / ".opencode").mkdir(parents=True)
        (tmp_path / ".opencode/system-prompt.md").write_text("# OpenCode\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".opencode/system-prompt.md"

    def test_no_harness_returns_claude_md_by_default(self, tmp_path: Path) -> None:
        """When no harness is specified, defaults to CLAUDE.md."""
        assert ciw.detect_target(tmp_path) == tmp_path / "CLAUDE.md"

    def test_non_claude_harness_returns_none_when_no_target(self, tmp_path: Path) -> None:
        """When harness is known but no target exists, returns None."""
        assert ciw.detect_target(tmp_path, harness="qwen-code") is None

    def test_priority_order_shared_targets(self, tmp_path: Path) -> None:
        """When multiple shared targets exist, returns the highest priority one."""
        # Create CLAUDE.md and GEMINI.md — GEMINI.md should win
        (tmp_path / "CLAUDE.md").write_text("# Claude\n")
        (tmp_path / "GEMINI.md").write_text("# Gemini\n")
        assert ciw.detect_target(tmp_path) == tmp_path / "GEMINI.md"

    def test_windsurfrules_before_claude_md(self, tmp_path: Path) -> None:
        """windsurfrules takes priority over CLAUDE.md."""
        (tmp_path / "CLAUDE.md").write_text("# Claude\n")
        (tmp_path / ".windsurfrules").write_text("# Rules\n")
        assert ciw.detect_target(tmp_path) == tmp_path / ".windsurfrules"


class TestServiceBaseUrl:
    """The seam that keeps wiring off a live service (2026-07-28 leak).

    `offer_index` POSTs /code/index for the repo being wired. In tests that
    repo is a `tmp_path`, and a live :47950 resolves the developer's REAL data
    dir — so every wire/add test indexed its temp dir into
    `~/.local/share/agentalloy/code_index/repos/`, 32 of them by the time it
    was caught. `tests/conftest.py` pins STATE_SERVICE_URL autouse; these pin
    the behaviour that pin depends on.
    """

    def test_defaults_to_the_local_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
        assert ciw.service_base_url(47950) == "http://127.0.0.1:47950"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STATE_SERVICE_URL", "http://127.0.0.1:1")
        assert ciw.service_base_url(47950) == "http://127.0.0.1:1"

    def test_every_http_call_honours_the_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No caller may build its own URL — that is how one escapes the pin."""
        seen: list[str] = []

        class _Resp:
            status = 200

            def read(self) -> bytes:
                return b"[]"

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def _fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
            seen.append(req.full_url)
            return _Resp()

        monkeypatch.setenv("STATE_SERVICE_URL", "http://127.0.0.1:1")
        monkeypatch.setattr(ciw.urllib.request, "urlopen", _fake_urlopen)

        ciw.service_module_status(47950)
        ciw.registry_slugs(47950)
        ciw.submit_index_job(47950, Path("/tmp/does-not-matter"))

        assert len(seen) == 3
        assert all(url.startswith("http://127.0.0.1:1/") for url in seen), seen


class TestSubmitIndexJob409:
    """A 409 'already active' from /code/index is not a start failure (B2).

    ``urlopen`` raises ``urllib.error.HTTPError`` (a ``URLError`` subclass) on
    the 409. The old generic ``except (URLError, ...)`` swallowed it into
    ``None``, so ``offer_index`` printed the misleading "could not start ... run
    manually" even though the job was running. The specific ``HTTPError``
    handler must precede the generic one and map 409 to ``already_active``.
    """

    def _http_error(self, code: int, detail: object) -> urllib.error.HTTPError:
        import io
        import json as _json

        body = _json.dumps({"detail": detail}).encode("utf-8")
        return urllib.error.HTTPError(
            "http://127.0.0.1:1/code/index", code, "Conflict", {}, io.BytesIO(body)
        )

    def _raise(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        def _fake_urlopen(req: Any, timeout: float = 0) -> Any:
            raise exc

        monkeypatch.setattr(ciw.urllib.request, "urlopen", _fake_urlopen)

    def test_409_already_active_parses_job_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._raise(
            monkeypatch,
            self._http_error(409, "an index job for slug 'org__repo' is already active: j9"),
        )
        result = ciw.submit_index_job(47950, Path("/tmp/repo"))
        assert result == {"already_active": True, "job_id": "j9"}

    def test_409_without_detail_still_already_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._raise(monkeypatch, self._http_error(409, ""))
        result = ciw.submit_index_job(47950, Path("/tmp/repo"))
        assert result == {"already_active": True, "job_id": None}

    def test_other_http_error_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._raise(monkeypatch, self._http_error(500, "boom"))
        assert ciw.submit_index_job(47950, Path("/tmp/repo")) is None

    def test_unreachable_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._raise(monkeypatch, urllib.error.URLError("refused"))
        assert ciw.submit_index_job(47950, Path("/tmp/repo")) is None


class TestWireCodeIndexHarnessSelection:
    """_code_index_harness() picks the block owner for `wire` (never None).

    The old rule (`"claude-code" if claude-code in harnesses else None`)
    passed None for every non-claude-code wiring, and detect_target(None)
    defaults to creating CLAUDE.md — so `wire qwen-code` left a stray
    CLAUDE.md behind. The selection must resolve each wired harness's own
    carrier and fall back to a harness that writes no block.
    """

    def test_proxy_only_harness_falls_back_to_itself(self, tmp_path: Path) -> None:
        """qwen-code alone: no carrier resolves, so it owns (no) block."""
        assert _code_index_harness(tmp_path, ["qwen-code"]) == "qwen-code"

    def test_claude_code_alone_falls_back_to_itself(self, tmp_path: Path) -> None:
        assert _code_index_harness(tmp_path, ["claude-code"]) == "claude-code"

    def test_all_proxy_only_picks_first(self, tmp_path: Path) -> None:
        assert _code_index_harness(tmp_path, ["codex", "qwen-code"]) == "codex"

    def test_skips_proxy_only_for_markdown_harness(self, tmp_path: Path) -> None:
        """claude-code + cursor: the block belongs to cursor's carrier."""
        (tmp_path / ".cursor" / "rules").mkdir(parents=True)
        (tmp_path / ".cursor" / "rules" / "agentalloy.mdc").write_text("---\n")
        assert _code_index_harness(tmp_path, ["claude-code", "cursor"]) == "cursor"

    def test_first_harness_with_target_wins(self, tmp_path: Path) -> None:
        (tmp_path / ".cursor" / "rules").mkdir(parents=True)
        (tmp_path / ".cursor" / "rules" / "agentalloy.mdc").write_text("---\n")
        (tmp_path / "GEMINI.md").write_text("# Gemini\n")
        assert _code_index_harness(tmp_path, ["cursor", "antigravity"]) == "cursor"

    def test_markdown_harness_resolves_shared_target(self, tmp_path: Path) -> None:
        (tmp_path / "GEMINI.md").write_text("# Gemini\n")
        assert _code_index_harness(tmp_path, ["antigravity"]) == "antigravity"

    def test_proxy_only_selection_writes_no_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end invariant: `wire qwen-code` leaves no CLAUDE.md."""
        _fake_slug(monkeypatch)
        picked = _code_index_harness(tmp_path, ["qwen-code"])
        ciw.wire_code_index_block(tmp_path, 47950, harness=picked)
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_fresh_repo_no_carrier_writes_no_block_and_no_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No carrier exists yet: no block anywhere, and no CLAUDE.md created."""
        _fake_slug(monkeypatch)
        picked = _code_index_harness(tmp_path, ["cursor"])
        assert picked == "cursor"
        ciw.wire_code_index_block(tmp_path, 47950, harness=picked)
        assert not (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / ".cursor" / "rules" / "agentalloy-code-index.mdc").exists()

    def test_mixed_selection_writes_to_markdown_carrier_not_claude_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_slug(monkeypatch)
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        picked = _code_index_harness(tmp_path, ["qwen-code", "hermes-agent"])
        assert picked == "hermes-agent"
        ciw.wire_code_index_block(tmp_path, 47950, harness=picked)
        assert not (tmp_path / "CLAUDE.md").exists()
        assert ciw.SENTINEL_BEGIN in (tmp_path / "AGENTS.md").read_text()
