"""Unit tests for the ``wire-harness`` subcommand.

Maps to test-plan.md § Harness wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.wire_harness import (
    SENTINEL_BEGIN,
    SENTINEL_END,
    STEP_NAME,
    VALID_HARNESSES,
    _detect_line_ending,  # pyright: ignore[reportPrivateUsage]
    _inject_sentinel_block,  # pyright: ignore[reportPrivateUsage]
    wire_harness,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# Sentinel injection
# ---------------------------------------------------------------------------


class TestSentinelInjection:
    def test_inject_into_empty(self) -> None:
        result = _inject_sentinel_block("", "content here")
        assert SENTINEL_BEGIN in result
        assert SENTINEL_END in result
        assert "content here" in result

    def test_inject_appends_to_existing(self) -> None:
        existing = "# My CLAUDE.md\n\nExisting content.\n"
        result = _inject_sentinel_block(existing, "injected")
        assert result.startswith("# My CLAUDE.md")
        assert "Existing content." in result
        assert SENTINEL_BEGIN in result
        assert "injected" in result

    def test_replace_existing_block(self) -> None:
        existing = f"Before\n{SENTINEL_BEGIN}\nold content\n{SENTINEL_END}\nAfter\n"
        result = _inject_sentinel_block(existing, "new content")
        assert "old content" not in result
        assert "new content" in result
        assert "Before" in result
        assert "After" in result
        assert result.count(SENTINEL_BEGIN) == 1

    def test_preserves_crlf(self) -> None:
        existing = "Line 1\r\nLine 2\r\n"
        result = _inject_sentinel_block(existing, "injected")
        assert "\r\n" in result

    def test_detect_lf(self) -> None:
        assert _detect_line_ending("a\nb\n") == "\n"

    def test_detect_crlf(self) -> None:
        assert _detect_line_ending("a\r\nb\r\n") == "\r\n"


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class TestClaudeCode:
    def test_creates_claude_md(self, repo_root: Path) -> None:
        result = wire_harness("claude-code", port=8000, root=repo_root)
        assert result["harness"] == "claude-code"
        assert result["integration_vector"] == "markdown_injection"
        assert len(result["files_written"]) == 2  # CLAUDE.md + .claude/settings.json hooks
        claude_md = repo_root / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content

    def test_appends_to_existing_claude_md(self, repo_root: Path) -> None:
        (repo_root / "CLAUDE.md").write_text("# My Project\n\nExisting.\n")
        wire_harness("claude-code", port=9090, root=repo_root)
        content = (repo_root / "CLAUDE.md").read_text()
        assert "# My Project" in content
        assert "Existing." in content
        assert "localhost:9090" in content

    def test_replaces_on_rerun(self, repo_root: Path) -> None:
        wire_harness("claude-code", port=8000, root=repo_root)
        wire_harness("claude-code", port=9090, root=repo_root)
        content = (repo_root / "CLAUDE.md").read_text()
        assert "localhost:9090" in content
        assert "localhost:8000" not in content
        assert content.count(SENTINEL_BEGIN) == 1

    def test_custom_port(self, repo_root: Path) -> None:
        wire_harness("claude-code", port=3000, root=repo_root)
        content = (repo_root / "CLAUDE.md").read_text()
        assert "localhost:3000" in content


# ---------------------------------------------------------------------------
# Hermes Agent
# ---------------------------------------------------------------------------


class TestHermesAgent:
    def test_user_scope_writes_soul_md(self, tmp_path: Path) -> None:
        result = wire_harness("hermes-agent", port=8000, root=tmp_path, scope="user")
        assert result["integration_vector"] == "markdown_injection"
        soul = tmp_path / ".hermes" / "SOUL.md"
        assert soul.exists()
        content = soul.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content
        assert "/health" in content

    def test_repo_scope_writes_agents_md(self, repo_root: Path) -> None:
        result = wire_harness("hermes-agent", port=8000, root=repo_root, scope="repo")
        assert result["integration_vector"] == "markdown_injection"
        agents = repo_root / "AGENTS.md"
        assert agents.exists()
        content = agents.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content

    def test_preserves_existing_soul_content(self, tmp_path: Path) -> None:
        soul = tmp_path / ".hermes" / "SOUL.md"
        soul.parent.mkdir(parents=True)
        soul.write_text("# My persona\n\nBe terse.\n")
        wire_harness("hermes-agent", port=8000, root=tmp_path, scope="user")
        content = soul.read_text()
        assert "# My persona" in content
        assert "Be terse." in content
        assert SENTINEL_BEGIN in content


# ---------------------------------------------------------------------------
# Gemini CLI
# ---------------------------------------------------------------------------


class TestGeminiCli:
    def test_creates_gemini_md(self, repo_root: Path) -> None:
        result = wire_harness("gemini-cli", port=8000, root=repo_root)
        assert result["harness"] == "gemini-cli"
        gemini_md = repo_root / "GEMINI.md"
        assert gemini_md.exists()
        content = gemini_md.read_text()
        assert SENTINEL_BEGIN in content
        assert "shell tool" in content


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class TestCursor:
    def test_modern_cursor_dir(self, repo_root: Path) -> None:
        """If .cursor/ exists, use .cursor/rules/agentalloy.mdc (dedicated)."""
        (repo_root / ".cursor").mkdir()
        result = wire_harness("cursor", port=8000, root=repo_root)
        assert len(result["files_written"]) == 1
        mdc = repo_root / ".cursor" / "rules" / "agentalloy.mdc"
        assert mdc.exists()
        content = mdc.read_text()
        # Dedicated file — no sentinels
        assert SENTINEL_BEGIN not in content
        assert "localhost:8000" in content
        # Has frontmatter
        assert "description:" in content

    def test_legacy_cursorrules(self, repo_root: Path) -> None:
        """No .cursor/ → .cursorrules with sentinels."""
        wire_harness("cursor", port=8000, root=repo_root)
        cursorrules = repo_root / ".cursorrules"
        assert cursorrules.exists()
        content = cursorrules.read_text()
        assert SENTINEL_BEGIN in content


# ---------------------------------------------------------------------------
# Windsurf
# ---------------------------------------------------------------------------


class TestWindsurf:
    def test_modern_windsurf_dir(self, repo_root: Path) -> None:
        """If .windsurf/ exists, use .windsurf/rules/agentalloy.md (dedicated)."""
        (repo_root / ".windsurf").mkdir()
        result = wire_harness("windsurf", port=8000, root=repo_root)
        assert len(result["files_written"]) == 1
        md = repo_root / ".windsurf" / "rules" / "agentalloy.md"
        assert md.exists()
        content = md.read_text()
        # Dedicated file — no sentinels
        assert SENTINEL_BEGIN not in content
        assert "localhost:8000" in content
        # Has frontmatter
        assert "trigger:" in content

    def test_legacy_windsurfrules(self, repo_root: Path) -> None:
        """No .windsurf/ → .windsurfrules with sentinels."""
        wire_harness("windsurf", port=8000, root=repo_root)
        rules = repo_root / ".windsurfrules"
        assert rules.exists()
        content = rules.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content


# ---------------------------------------------------------------------------
# GitHub Copilot
# ---------------------------------------------------------------------------


class TestGithubCopilot:
    def test_writes_copilot_instructions(self, repo_root: Path) -> None:
        result = wire_harness("github-copilot", port=8000, root=repo_root)
        assert result["integration_vector"] == "markdown_injection"
        path = repo_root / ".github" / "copilot-instructions.md"
        assert path.exists()
        content = path.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content

    def test_preserves_existing_user_content(self, repo_root: Path) -> None:
        """User-authored content above the sentinel block must survive re-wires."""
        gh_dir = repo_root / ".github"
        gh_dir.mkdir()
        path = gh_dir / "copilot-instructions.md"
        path.write_text("# Project copilot rules\n\nUse TypeScript strict mode.\n")
        wire_harness("github-copilot", port=8000, root=repo_root)
        content = path.read_text()
        assert "Use TypeScript strict mode." in content
        assert SENTINEL_BEGIN in content


# ---------------------------------------------------------------------------
# Open harnesses
# ---------------------------------------------------------------------------


class TestOpenHarnesses:
    def test_opencode(self, repo_root: Path) -> None:
        result = wire_harness("opencode", port=8000, root=repo_root)
        assert result["integration_vector"] == "system_prompt_snippet"
        path = repo_root / ".opencode" / "system-prompt.md"
        assert path.exists()

    def test_cline(self, repo_root: Path) -> None:
        wire_harness("cline", port=8000, root=repo_root)
        path = repo_root / ".clinerules"
        assert path.exists()
        content = path.read_text()
        assert SENTINEL_BEGIN in content

    def test_aider(self, repo_root: Path) -> None:
        result = wire_harness("aider", port=8000, root=repo_root)
        # Instructions file (dedicated)
        instructions = repo_root / ".agentalloy-aider-instructions.md"
        assert instructions.exists()
        # .aider.conf.yml entry
        conf = repo_root / ".aider.conf.yml"
        assert conf.exists()
        content = conf.read_text()
        assert ".agentalloy-aider-instructions.md" in content
        assert len(result["files_written"]) == 2


# ---------------------------------------------------------------------------
# Continue.dev
# ---------------------------------------------------------------------------


class TestContinue:
    def test_closed_creates_config(self, repo_root: Path) -> None:
        result = wire_harness("continue-closed", port=8000, root=repo_root)
        assert result["harness"] == "continue-closed"
        config_path = repo_root / ".continuerc.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "customCommands" in config
        assert any(c["name"] == "skill" for c in config["customCommands"])
        assert "systemMessage" in config
        assert "agentalloy:begin" in config["systemMessage"]

    def test_local_no_system_message(self, repo_root: Path) -> None:
        wire_harness("continue-local", port=8000, root=repo_root)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert any(c["name"] == "skill" for c in config["customCommands"])
        assert "systemMessage" not in config

    def test_preserves_existing_config(self, repo_root: Path) -> None:
        existing = {"models": [{"title": "GPT-4"}], "customCommands": []}
        (repo_root / ".continuerc.json").write_text(json.dumps(existing))
        wire_harness("continue-closed", port=8000, root=repo_root)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert config["models"] == [{"title": "GPT-4"}]
        assert any(c["name"] == "skill" for c in config["customCommands"])


# ---------------------------------------------------------------------------
# Manual
# ---------------------------------------------------------------------------


class TestManual:
    def test_manual_prints_to_stderr(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Block goes to stderr so stdout stays parseable as the result JSON.
        # The block is also returned in result["manual_block"].
        result = wire_harness("manual", port=8000, root=repo_root)
        assert result["files_written"] == []
        assert SENTINEL_BEGIN in result["manual_block"]
        assert "localhost:8000" in result["manual_block"]
        captured = capsys.readouterr()
        assert SENTINEL_BEGIN in captured.err
        assert SENTINEL_BEGIN not in captured.out


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class TestOutputSchema:
    def test_required_keys(self, repo_root: Path) -> None:
        result = wire_harness("claude-code", port=8000, root=repo_root)
        assert result["schema_version"] == 1
        assert "harness" in result
        assert "integration_vector" in result
        assert "files_written" in result

    def test_file_entry_shape(self, repo_root: Path) -> None:
        result = wire_harness("claude-code", port=8000, root=repo_root)
        entry = result["files_written"][0]
        assert "path" in entry
        assert "action" in entry
        assert "content_sha256" in entry


# ---------------------------------------------------------------------------
# State recording
# ---------------------------------------------------------------------------


class TestState:
    def test_records_harness_in_state(self, repo_root: Path) -> None:
        # Schema v2: each harness_files_written entry carries its own
        # `harness` field (state may span multiple repos with different
        # harnesses wired). No top-level `harness` field exists.
        wire_harness("claude-code", port=8000, root=repo_root)
        st = install_state.load_state(repo_root)
        assert "harness" not in st
        assert st["harness_files_written"][0]["harness"] == "claude-code"
        assert st["harness_files_written"][0]["repo_root"] == str(repo_root)
        assert install_state.is_step_completed(st, STEP_NAME)

    def test_records_files_written(self, repo_root: Path) -> None:
        wire_harness("claude-code", port=8000, root=repo_root)
        st = install_state.load_state(repo_root)
        assert len(st["harness_files_written"]) == 2  # CLAUDE.md + settings.json hooks


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_harness_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit):
            wire_harness("nonexistent", root=repo_root)

    def test_all_valid_harnesses_accepted(self, repo_root: Path) -> None:
        """Smoke test: every registered harness produces a result without error.

        ``mcp-only`` is the documented exception — it's accepted by the CLI
        parser so users get a clear error rather than argparse's "invalid
        choice", but the actual MCP fallback is deferred to install spec
        step 11. It exits 1 with a "deferred" message.
        """
        for harness in VALID_HARNESSES:
            # Reset state for each
            state_file = repo_root / ".agentalloy" / "install-state.json"
            if state_file.exists():
                state_file.unlink()
            if harness == "mcp-only":
                with pytest.raises(SystemExit):
                    wire_harness(harness, port=8000, root=repo_root)
                continue
            result = wire_harness(harness, port=8000, root=repo_root)
            assert result["harness"] == harness


class TestRewireMerge:
    def test_rewire_different_harness_preserves_prior_files(self, repo_root: Path) -> None:
        """Switching harness must merge harness_files_written, not overwrite —
        otherwise uninstall can't clean up the prior harness's sentinel block."""
        from agentalloy.install.state import load_state

        # Wire claude-code first
        wire_harness("claude-code", port=8000, root=repo_root)
        st = load_state(repo_root)
        first_paths = {f["path"] for f in st["harness_files_written"]}
        assert any("CLAUDE.md" in p for p in first_paths)

        # Now wire cursor — claude-code's CLAUDE.md entry must remain
        wire_harness("cursor", port=8000, root=repo_root)
        st = load_state(repo_root)
        merged_paths = {f["path"] for f in st["harness_files_written"]}
        assert any("CLAUDE.md" in p for p in merged_paths)
        assert any(".cursor" in p for p in merged_paths)
        # Each entry records which harness wrote it.
        harnesses = {f["harness"] for f in st["harness_files_written"]}
        assert harnesses == {"claude-code", "cursor"}

    def test_rewire_same_harness_replaces_entry_in_place(self, repo_root: Path) -> None:
        """Re-wiring the same harness must not duplicate the same path entry."""
        from agentalloy.install.state import load_state

        wire_harness("claude-code", port=8000, root=repo_root, force=True)
        wire_harness("claude-code", port=9000, root=repo_root, force=True)
        st = load_state(repo_root)
        paths = [f["path"] for f in st["harness_files_written"]]
        # No duplicates of the same path
        assert len(paths) == len(set(paths))


class TestScopeFlag:
    """Tests for --scope user|repo behavior. Maps to test-plan.md § Wire scope."""

    def test_scope_user_defaults_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scope='user' resolves root to $HOME so wiring is global across repos."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        # State directory also routes through HOME; force a fresh per-test state.
        monkeypatch.setenv("AGENTALLOY_STATE_DIR", str(fake_home / ".agentalloy"))

        result = wire_harness("aider", port=8000, scope="user")
        for entry in result["files_written"]:
            assert str(fake_home) in entry["path"], entry

    def test_scope_repo_uses_repo_root(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scope='repo' falls back to the discovered repo root (cwd)."""
        monkeypatch.chdir(repo_root)
        result = wire_harness("aider", port=8000, scope="repo")
        for entry in result["files_written"]:
            assert str(repo_root) in entry["path"], entry

    def test_scope_invalid_raises(self) -> None:
        with pytest.raises(SystemExit):
            wire_harness("aider", port=8000, scope="global")


# ---------------------------------------------------------------------------
# Intake activation markers
# ---------------------------------------------------------------------------


class TestIntakeActivationMarkers:
    """Verify wired templates contain intake activation markers.

    Maps to plan: intake activation workflow — harness templates must include
    health-gate, phase lock file reference, and skip-if-non-SDD guidance.
    """

    _INTAKE_MARKERS = [
        ".agentalloy/phase",
        "Health-gate",
        "non-SDD",
    ]

    def test_hermes_agent_has_intake_markers(self, tmp_path: Path) -> None:
        wire_harness("hermes-agent", port=8000, root=tmp_path, scope="user")
        content = (tmp_path / ".hermes" / "SOUL.md").read_text()
        for marker in self._INTAKE_MARKERS:
            assert marker in content, f"Missing marker: {marker}"

    def test_claude_code_has_intake_markers(self, repo_root: Path) -> None:
        wire_harness("claude-code", port=8000, root=repo_root)
        content = (repo_root / "CLAUDE.md").read_text()
        for marker in self._INTAKE_MARKERS:
            assert marker in content, f"Missing marker: {marker}"

    def test_all_harnesses_have_phase_reference(self, repo_root: Path) -> None:
        """Smoke test: every instruction-bearing harness file references .agentalloy/phase.

        Only checks .md and .mdc files — structured config files (.json, .yml, .toml)
        may encode phase references differently and are not required to contain the
        literal string.
        """
        instruction_extensions = {".md", ".mdc"}
        for harness in VALID_HARNESSES:
            state_file = repo_root / ".agentalloy" / "install-state.json"
            if state_file.exists():
                state_file.unlink()
            if harness == "mcp-only":
                continue
            result = wire_harness(harness, port=8000, root=repo_root)
            for entry in result["files_written"]:
                path = Path(entry["path"])
                if path.exists() and path.suffix.lower() in instruction_extensions:
                    content = path.read_text()
                    assert ".agentalloy/phase" in content, (
                        f"Harness {harness} at {path} missing phase reference"
                    )


# ---------------------------------------------------------------------------
# MCP fallback
# ---------------------------------------------------------------------------


class TestMCPFallback:
    """Tests for ``--mcp-fallback`` wiring path. Maps to T13."""

    def test_claude_code_mcp_writes_user_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude-code --mcp-fallback writes ~/.claude/mcp_servers.json."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("AGENTALLOY_STATE_DIR", str(fake_home / ".agentalloy"))

        result = wire_harness("claude-code", port=9999, mcp_fallback=True)
        assert result["integration_vector"] == "mcp_server_config"
        assert result["harness"] == "claude-code"

        mcp_config = fake_home / ".claude" / "mcp_servers.json"
        assert mcp_config.exists()
        config = json.loads(mcp_config.read_text())
        assert "agentalloy" in config["mcpServers"]
        entry = config["mcpServers"]["agentalloy"]
        assert entry["args"] == ["-m", "agentalloy.install.mcp_server", "--port", "9999"]

    def test_cursor_mcp_writes_repo_config(self, repo_root: Path) -> None:
        """cursor --mcp-fallback writes .cursor/mcp.json."""
        result = wire_harness("cursor", port=8888, root=repo_root, mcp_fallback=True)
        assert result["integration_vector"] == "mcp_server_config"

        mcp_config = repo_root / ".cursor" / "mcp.json"
        assert mcp_config.exists()
        config = json.loads(mcp_config.read_text())
        assert "agentalloy" in config["mcpServers"]
        entry = config["mcpServers"]["agentalloy"]
        assert entry["args"] == ["-m", "agentalloy.install.mcp_server", "--port", "8888"]

    def test_continue_closed_mcp_writes_continuerc(self, repo_root: Path) -> None:
        """continue-closed --mcp-fallback writes MCP entry to .continuerc.json."""
        result = wire_harness("continue-closed", port=7777, root=repo_root, mcp_fallback=True)
        assert result["integration_vector"] == "mcp_server_config"

        config_path = repo_root / ".continuerc.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "agentalloy" in config["mcpServers"]
        entry = config["mcpServers"]["agentalloy"]
        assert entry["args"] == ["-m", "agentalloy.install.mcp_server", "--port", "7777"]
        # Marker for uninstall
        assert config["_agentalloy_install_marker"]["variant"] == "mcp-closed"

    def test_continue_local_mcp_variant(self, repo_root: Path) -> None:
        """continue-local --mcp-fallback uses mcp-local variant marker."""
        wire_harness("continue-local", port=7777, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert config["_agentalloy_install_marker"]["variant"] == "mcp-local"

    def test_unsupported_harness_raises(self, repo_root: Path) -> None:
        """--mcp-fallback on unsupported harness raises SystemExit(1)."""
        with pytest.raises(SystemExit, match=".*"):
            wire_harness("hermes-agent", root=repo_root, mcp_fallback=True)

    def test_preserves_existing_mcp_servers(self, repo_root: Path) -> None:
        """Existing MCP server entries survive re-wiring."""
        (repo_root / ".cursor").mkdir()
        existing: dict[str, Any] = {
            "mcpServers": {"other-server": {"command": "other", "args": []}}
        }
        (repo_root / ".cursor" / "mcp.json").write_text(json.dumps(existing))
        wire_harness("cursor", port=8000, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".cursor" / "mcp.json").read_text())
        assert "other-server" in config["mcpServers"]
        assert "agentalloy" in config["mcpServers"]

    def test_uses_sys_executable(self, repo_root: Path) -> None:
        """MCP server entry uses sys.executable, not bare 'python'."""
        import sys

        wire_harness("cursor", port=8000, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".cursor" / "mcp.json").read_text())
        entry = config["mcpServers"]["agentalloy"]
        assert entry["command"] == sys.executable


# ---------------------------------------------------------------------------
# Proxy wiring
# ---------------------------------------------------------------------------


class TestProxyWiring:
    """Tests for ``--proxy`` wiring path. Maps to T11."""

    def test_continue_closed_proxy_writes_models(self, repo_root: Path) -> None:
        """continue-closed --proxy adds a proxy model to .continuerc.json."""
        result = wire_harness("continue-closed", port=9999, root=repo_root, proxy=True)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "continue-closed"

        config_path = repo_root / ".continuerc.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        # Proxy model added
        models = config.get("models", [])
        proxy_model = [m for m in models if m.get("agentalloy_proxy")]
        assert len(proxy_model) == 1
        assert proxy_model[0]["apiBase"] == "http://localhost:9999/v1"
        assert proxy_model[0]["provider"] == "openai"
        # Marker for uninstall
        assert config["_agentalloy_install_marker"]["variant"] == "proxy-closed"

    def test_continue_local_proxy_variant(self, repo_root: Path) -> None:
        """continue-local --proxy uses proxy-local variant marker."""
        wire_harness("continue-local", port=8888, root=repo_root, proxy=True)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert config["_agentalloy_install_marker"]["variant"] == "proxy-local"

    def test_proxy_idempotent(self, repo_root: Path) -> None:
        """Re-wiring proxy removes old entry and adds new one."""
        wire_harness("continue-closed", port=9999, root=repo_root, proxy=True)
        wire_harness("continue-closed", port=7777, root=repo_root, proxy=True)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        models = config.get("models", [])
        proxy_models = [m for m in models if m.get("agentalloy_proxy")]
        assert len(proxy_models) == 1
        assert proxy_models[0]["apiBase"] == "http://localhost:7777/v1"

    def test_claude_code_proxy_instruction(self, repo_root: Path) -> None:
        """claude-code --proxy writes proxy instruction block to CLAUDE.md."""
        result = wire_harness("claude-code", port=5555, root=repo_root, proxy=True)
        assert result["integration_vector"] == "proxy"

        claude_md = repo_root / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:5555" in content
        assert "proxy" in content.lower()

    def test_manual_proxy_prints_to_stderr(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """manual --proxy emits proxy instruction to stderr."""
        result = wire_harness("manual", port=4444, root=repo_root, proxy=True)
        assert result["integration_vector"] == "proxy"
        assert result["files_written"] == []
        captured = capsys.readouterr()
        assert SENTINEL_BEGIN in captured.err
        assert "localhost:4444" in captured.err

    def test_cursor_proxy_instruction(self, repo_root: Path) -> None:
        """cursor --proxy writes proxy instruction block."""
        (repo_root / ".cursor").mkdir()
        result = wire_harness("cursor", port=6666, root=repo_root, proxy=True)
        assert result["integration_vector"] == "proxy"
        mdc = repo_root / ".cursor" / "rules" / "agentalloy.mdc"
        assert mdc.exists()
        content = mdc.read_text()
        assert "localhost:6666" in content
        assert "proxy" in content.lower()

    def test_hermes_agent_proxy_user_scope(self, tmp_path: Path) -> None:
        """hermes-agent --proxy user scope writes to SOUL.md."""
        result = wire_harness("hermes-agent", port=3333, root=tmp_path, proxy=True, scope="user")
        assert result["integration_vector"] == "proxy"
        soul = tmp_path / ".hermes" / "SOUL.md"
        assert soul.exists()
        content = soul.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:3333" in content

    def test_mcp_only_with_proxy_rejected(self, repo_root: Path) -> None:
        """mcp-only harness with --proxy is rejected (blocked by top-level check)."""
        with pytest.raises(SystemExit):
            wire_harness("mcp-only", port=8000, root=repo_root, proxy=True)


class TestDeprecationWarning:
    """Verify deprecation warnings for markdown-injection wiring. Maps to T12."""

    def test_default_wiring_emits_deprecation(self, repo_root: Path, capsys) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        """Default (non-MCP) wiring emits a deprecation warning to stderr."""
        # Reset the warning flag so it fires for this test
        from agentalloy.install.subcommands import (
            wire_harness as wh_module,  # pyright: ignore[reportPrivateUsage]
        )

        wh_module._deprecation_warned = False
        wire_harness("claude-code", port=8000, root=repo_root)
        captured = capsys.readouterr()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        assert "DEPRECATION" in captured.err  # pyright: ignore[reportUnknownMemberType]
        assert "markdown-injection" in captured.err  # pyright: ignore[reportUnknownMemberType]
        assert "proxy" in captured.err  # pyright: ignore[reportUnknownMemberType]

    def test_mcp_fallback_no_deprecation(self, repo_root: Path, capsys) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        """MCP fallback wiring does NOT emit a deprecation warning."""
        (repo_root / ".cursor").mkdir()
        wire_harness("cursor", port=8000, root=repo_root, mcp_fallback=True)
        captured = capsys.readouterr()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        assert "DEPRECATION" not in captured.err  # pyright: ignore[reportUnknownMemberType]

    def test_proxy_no_deprecation(self, repo_root: Path, capsys) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        """Proxy wiring does NOT emit a deprecation warning."""
        wire_harness("claude-code", port=8000, root=repo_root, proxy=True)
        captured = capsys.readouterr()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        assert "DEPRECATION" not in captured.err  # pyright: ignore[reportUnknownMemberType]

    def test_warns_once_per_session(self, repo_root: Path, capsys) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        """Deprecation warning fires once, not on every call."""
        from agentalloy.install.subcommands import (
            wire_harness as wh_module,  # pyright: ignore[reportPrivateUsage]
        )

        wh_module._deprecation_warned = False
        # Wire twice
        wire_harness("claude-code", port=8000, root=repo_root)
        captured1 = capsys.readouterr()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        wire_harness("cursor", port=8000, root=repo_root)
        captured2 = capsys.readouterr()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        # First call has the warning, second doesn't
        assert "DEPRECATION" in captured1.err  # pyright: ignore[reportUnknownMemberType]
        assert "DEPRECATION" not in captured2.err  # pyright: ignore[reportUnknownMemberType]
