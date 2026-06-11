# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportArgumentType=false
"""Tests for the wire_harness module — harness wiring, sentinel injection, state tracking, and proxy configuration.

Maps to plan task 12: Deprecate monolithic wire_harness.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import uninstall_proxy, wire_harness
from agentalloy.install.subcommands.wire_harness import (
    SENTINEL_BEGIN,
    SENTINEL_END,
    STEP_NAME,
    VALID_HARNESSES,
    _detect_line_ending,
    _inject_sentinel_block,
)
from tests._wire_compat import wire_compat

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# VALID_HARNESSES uses global REGISTRY
# ---------------------------------------------------------------------------


class TestValidHarnessesFromRegistry:
    """Verify VALID_HARNESSES derives from the global REGISTRY."""

    def test_valid_harnesses_match_registry(self) -> None:
        """VALID_HARNESSES keys match REGISTRY keys."""
        from agentalloy.providers import REGISTRY

        assert frozenset(REGISTRY.keys()) == wire_harness.VALID_HARNESSES

    def test_valid_harnesses_not_from_local_registry(self) -> None:
        """VALID_HARNESSES does not include mcp-only (local-only entry)."""
        # mcp-only is in the local _HARNESS_REGISTRY but not in the global REGISTRY
        assert "mcp-only" not in wire_harness.VALID_HARNESSES

    def test_valid_harnesses_contains_registered_providers(self) -> None:
        """VALID_HARNESSES includes all providers in the global REGISTRY."""
        from agentalloy.providers import REGISTRY

        for key in REGISTRY:
            assert key in wire_harness.VALID_HARNESSES


@pytest.fixture()
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch Path.home() to return a tmp_path subdir for hermetic tests."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home, raising=False)
    return home


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

    def test_inverted_sentinel_order_raises(self) -> None:
        """END before BEGIN must raise ValueError to prevent file corruption."""
        existing = f"before\n{SENTINEL_END}\nbad\n{SENTINEL_BEGIN}\nafter\n"
        with pytest.raises(ValueError, match="END marker appears before BEGIN"):
            _inject_sentinel_block(existing, "new content")

    def test_duplicate_sentinels_raises(self) -> None:
        """Multiple BEGIN or END markers must raise ValueError."""
        existing = (
            f"{SENTINEL_BEGIN}\nfirst\n{SENTINEL_END}\n{SENTINEL_BEGIN}\nsecond\n{SENTINEL_END}\n"
        )
        with pytest.raises(ValueError, match="expected at most 1 of each"):
            _inject_sentinel_block(existing, "new content")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class TestClaudeCode:
    """claude-code with legacy=True uses hooks-based wiring, not markdown injection.

    The legacy path now delegates to the modern provider hook_writer, which
    writes under Path.home() (~/.agentalloy/hooks + ~/.claude/settings.json) —
    home MUST be patched or these tests pollute the developer's real Claude
    settings (it happened: see PR #118 history).
    """

    @pytest.fixture(autouse=True)
    def _fake_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        return home

    def test_creates_hooks_config(self, repo_root: Path) -> None:
        result = wire_compat("claude-code", port=8000, root=repo_root, legacy=True)
        assert result["harness"] == "claude-code"
        assert result["integration_vector"] == "claude_code_hooks"
        # Script install + settings.json merge.
        paths = [f["path"] for f in result["files_written"]]
        assert any("agentalloy-hook-claude-code.sh" in p for p in paths)
        assert any(p.endswith(".claude/settings.json") for p in paths)
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        assert "UserPromptSubmit" in settings["hooks"]

    def test_hooks_idempotent(self, repo_root: Path) -> None:
        wire_compat("claude-code", port=8000, root=repo_root, legacy=True)
        wire_compat("claude-code", port=9090, root=repo_root, legacy=True)
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        # Re-wiring must not duplicate our entries per event.
        for groups in settings["hooks"].values():
            ours = [
                g
                for g in groups
                if any("agentalloy" in h.get("command", "") for h in g.get("hooks", []))
            ]
            assert len(ours) == 1

    def test_custom_port_hooks(self, repo_root: Path) -> None:
        wire_compat("claude-code", port=3000, root=repo_root, legacy=True)
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        blob = json.dumps(settings["hooks"])
        assert "localhost:3000" in blob


# ---------------------------------------------------------------------------
# Hermes Agent
# ---------------------------------------------------------------------------


class TestHermesAgent:
    def test_user_scope_writes_soul_md(self, tmp_path: Path) -> None:
        result = wire_compat("hermes-agent", port=8000, root=tmp_path, scope="user", legacy=True)
        assert result["integration_vector"] == "markdown_injection"
        soul = tmp_path / ".hermes" / "SOUL.md"
        assert soul.exists()
        content = soul.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content
        assert "/health" in content

    def test_repo_scope_writes_agents_md(self, repo_root: Path) -> None:
        result = wire_compat("hermes-agent", port=8000, root=repo_root, scope="repo", legacy=True)
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
        wire_compat("hermes-agent", port=8000, root=tmp_path, scope="user", legacy=True)
        content = soul.read_text()
        assert "# My persona" in content
        assert "Be terse." in content
        assert SENTINEL_BEGIN in content


# ---------------------------------------------------------------------------
# Gemini CLI
# ---------------------------------------------------------------------------


class TestGeminiCli:
    def test_creates_gemini_md(self, repo_root: Path) -> None:
        result = wire_compat("gemini-cli", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("cursor", port=8000, root=repo_root, legacy=True)
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
        wire_compat("cursor", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("windsurf", port=8000, root=repo_root, legacy=True)
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
        wire_compat("windsurf", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("github-copilot", port=8000, root=repo_root, legacy=True)
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
        wire_compat("github-copilot", port=8000, root=repo_root, legacy=True)
        content = path.read_text()
        assert "Use TypeScript strict mode." in content
        assert SENTINEL_BEGIN in content


# ---------------------------------------------------------------------------
# Open harnesses
# ---------------------------------------------------------------------------


class TestOpenHarnesses:
    def test_opencode(self, repo_root: Path) -> None:
        result = wire_compat("opencode", port=8000, root=repo_root, legacy=True)
        assert result["integration_vector"] == "system_prompt_snippet"
        path = repo_root / ".opencode" / "system-prompt.md"
        assert path.exists()

    def test_cline(self, repo_root: Path) -> None:
        wire_compat("cline", port=8000, root=repo_root, legacy=True)
        path = repo_root / ".clinerules"
        assert path.exists()
        content = path.read_text()
        assert SENTINEL_BEGIN in content

    def test_cline_proxy(self, tmp_path: Path) -> None:
        """Proxy path writes .cline/settings.json with proxy API fields."""
        result = wire_compat("cline", port=8000, root=tmp_path)
        assert result["harness"] == "cline"
        assert result["integration_vector"] == "proxy"
        settings = tmp_path / ".cline" / "settings.json"
        assert settings.exists()
        config = json.loads(settings.read_text())
        assert config["apiProvider"] == "openai"
        assert config["apiBaseUrl"] == "http://localhost:8000/v1"
        assert config["apiKey"] == "agentalloy"
        assert config["model"] == "agentalloy-proxy"

    def test_merges_existing_settings(self, tmp_path: Path) -> None:
        """Cline proxy settings merge with existing settings without overwriting."""
        # Create a pre-existing settings file with user-defined settings
        existing_settings = {
            "apiProvider": "anthropic",
            "modelId": "claude-3-sonnet",
            "someOtherSetting": "keep this",
        }
        settings_dir = tmp_path / ".cline"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps(existing_settings, indent=2))

        wire_compat("cline", port=9999, root=tmp_path)

        config = json.loads((settings_dir / "settings.json").read_text())

        # Verify proxy fields are present
        assert config["apiProvider"] == "openai"
        assert config["apiBaseUrl"] == "http://localhost:9999/v1"
        assert config["apiKey"] == "agentalloy"
        assert config["model"] == "agentalloy-proxy"

        # Verify existing settings are preserved
        assert config["modelId"] == "claude-3-sonnet"
        assert config["someOtherSetting"] == "keep this"

    def test_aider(self, repo_root: Path) -> None:
        result = wire_compat("aider", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("continue-closed", port=8000, root=repo_root, legacy=True)
        assert result["harness"] == "continue-closed"
        config_path = repo_root / ".continuerc.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert "customCommands" in config
        assert any(c["name"] == "skill" for c in config["customCommands"])
        assert "systemMessage" in config
        assert "agentalloy:begin" in config["systemMessage"]

    def test_local_no_system_message(self, repo_root: Path) -> None:
        wire_compat("continue-local", port=8000, root=repo_root, legacy=True)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert any(c["name"] == "skill" for c in config["customCommands"])
        assert "systemMessage" not in config

    def test_preserves_existing_config(self, repo_root: Path) -> None:
        existing = {"models": [{"title": "GPT-4"}], "customCommands": []}
        (repo_root / ".continuerc.json").write_text(json.dumps(existing))
        wire_compat("continue-closed", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("manual", port=8000, root=repo_root, legacy=True)
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
        result = wire_compat("claude-code", port=8000, root=repo_root)
        assert result["schema_version"] == 1
        assert "harness" in result
        assert "integration_vector" in result
        assert "files_written" in result

    def test_file_entry_shape(self, repo_root: Path) -> None:
        result = wire_compat("claude-code", port=8000, root=repo_root)
        entry = result["files_written"][0]
        assert "path" in entry
        assert "action" in entry
        assert "content_sha256" in entry


# ---------------------------------------------------------------------------
# State recording
# ---------------------------------------------------------------------------


class TestState:
    def test_records_harness_in_state(
        self,
        repo_root: Path,
        mock_home: Path,  # noqa: ARG001
    ) -> None:
        # Schema v2: each harness_files_written entry carries its own
        # `harness` field (state may span multiple repos with different
        # harnesses wired). No top-level `harness` field exists.
        wire_compat("claude-code", port=8000, root=repo_root)
        st = install_state.load_state(repo_root)
        assert "harness" not in st
        assert st["harness_files_written"][0]["harness"] == "claude-code"
        assert st["harness_files_written"][0]["repo_root"] == str(repo_root)
        assert install_state.is_step_completed(st, STEP_NAME)

    def test_records_files_written(self, repo_root: Path, mock_home: Path) -> None:
        wire_compat("claude-code", port=8000, root=repo_root)
        st = install_state.load_state(repo_root)
        assert len(st["harness_files_written"]) == 1  # env file only
        # Verify it wrote to the mocked home, not the real one
        assert (mock_home / ".agentalloy" / "claude-code-env.sh").exists()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_harness_exits(self, repo_root: Path) -> None:
        with pytest.raises(SystemExit):
            wire_compat("nonexistent", root=repo_root)

    def test_all_valid_harnesses_accepted(self, repo_root: Path) -> None:
        """Smoke test: every registered harness produces a result without error.

        ``mcp-only`` and harnesses without legacy support (codex, openclaw)
        are excluded from the legacy path test.
        """
        # Harnesses that do not support the legacy (markdown-injection) path
        legacy_excluded = {"mcp-only", "codex", "openclaw"}
        for harness in VALID_HARNESSES:
            # Reset state for each
            state_file = repo_root / ".agentalloy" / "install-state.json"
            if state_file.exists():
                state_file.unlink()
            if harness in legacy_excluded:
                continue
            if harness == "mcp-only":
                with pytest.raises(SystemExit):
                    wire_compat(harness, port=8000, root=repo_root)
                continue
            result = wire_compat(harness, port=8000, root=repo_root, legacy=True)
            assert result["harness"] == harness


class TestRewireMerge:
    def test_rewire_different_harness_preserves_prior_files(self, repo_root: Path) -> None:
        """Switching harness must merge harness_files_written, not overwrite —
        otherwise uninstall can't clean up the prior harness's sentinel block."""
        from agentalloy.install.state import load_state

        # Wire gemini-cli first (uses markdown injection)
        wire_compat("gemini-cli", port=8000, root=repo_root, legacy=True)
        st = load_state(repo_root)
        first_paths = {f["path"] for f in st["harness_files_written"]}
        assert any("GEMINI.md" in p for p in first_paths)

        # Now wire cursor — gemini-cli's GEMINI.md entry must remain
        wire_compat("cursor", port=8000, root=repo_root, legacy=True)
        st = load_state(repo_root)
        merged_paths = {f["path"] for f in st["harness_files_written"]}
        assert any("GEMINI.md" in p for p in merged_paths)
        assert any(".cursor" in p for p in merged_paths)
        # Each entry records which harness wrote it.
        harnesses = {f["harness"] for f in st["harness_files_written"]}
        assert harnesses == {"gemini-cli", "cursor"}

    def test_rewire_same_harness_replaces_entry_in_place(self, repo_root: Path) -> None:
        """Re-wiring the same harness must not duplicate the same path entry."""
        from agentalloy.install.state import load_state

        wire_compat("gemini-cli", port=8000, root=repo_root, force=True, legacy=True)
        wire_compat("gemini-cli", port=9000, root=repo_root, force=True, legacy=True)
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

        result = wire_compat("aider", port=8000, scope="user", legacy=True)
        for entry in result["files_written"]:
            assert str(fake_home) in entry["path"], entry

    def test_scope_repo_uses_repo_root(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scope='repo' falls back to the discovered repo root (cwd)."""
        monkeypatch.chdir(repo_root)
        result = wire_compat("aider", port=8000, scope="repo", legacy=True)
        for entry in result["files_written"]:
            assert str(repo_root) in entry["path"], entry

    def test_scope_invalid_raises(self) -> None:
        with pytest.raises(SystemExit):
            wire_compat("aider", port=8000, scope="global")


# ---------------------------------------------------------------------------
# Proxy wiring - aider
# ---------------------------------------------------------------------------


class TestAiderProxyWiring:
    """Proxy-mode wiring for aider writes .aider.conf.yml with proxy fields."""

    def test_writes_aider_conf_yml(self, repo_root: Path) -> None:
        result = wire_compat("aider", port=8000, root=repo_root)
        assert result["integration_vector"] == "proxy"
        conf = repo_root / ".aider.conf.yml"
        assert conf.exists()
        content = conf.read_text()
        assert "openai-api-base: http://localhost:8000/v1" in content
        assert "openai-api-key: agentalloy" in content
        assert "model: agentalloy-proxy" in content
        # Proxy mode does NOT create a separate instructions file — context
        # injection is handled server-side by the proxy
        assert ".agentalloy-aider-instructions.md" not in content

    def test_merges_existing_aider_conf(self, repo_root: Path) -> None:
        (repo_root / ".aider.conf.yml").write_text("model: gpt-4\nread:\n  - my-docs.md\n")
        wire_compat("aider", port=7777, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        # User settings preserved above sentinel
        assert "model: gpt-4" in content
        assert "my-docs.md" in content
        # Proxy block appended
        assert "localhost:7777" in content
        assert "# <!-- BEGIN agentalloy install -->" in content

    def test_idempotent_rewire(self, repo_root: Path) -> None:
        wire_compat("aider", port=8000, root=repo_root)
        wire_compat("aider", port=9000, root=repo_root)
        content = (repo_root / ".aider.conf.yml").read_text()
        assert "localhost:9000" in content
        assert "localhost:8000" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1

    def test_proxy_does_not_create_instructions_file(self, repo_root: Path) -> None:
        """Proxy mode writes .aider.conf.yml only, not the instructions file."""
        wire_compat("aider", port=8000, root=repo_root)
        assert not (repo_root / ".agentalloy-aider-instructions.md").exists()


# ---------------------------------------------------------------------------
# Proxy wiring - opencode
# ---------------------------------------------------------------------------


class TestOpenCodeProxyWiring:
    """Proxy-mode wiring for opencode writes env file + system prompt."""

    def test_writes_env_file(self, repo_root: Path) -> None:
        result = wire_compat("opencode", port=8000, root=repo_root)
        assert result["integration_vector"] == "proxy"
        env_path = repo_root / ".opencode" / ".agentalloy-env"
        assert env_path.exists()
        content = env_path.read_text()
        assert "OPENAI_API_BASE=http://localhost:8000/v1" in content
        assert "OPENAI_API_KEY" in content

    def test_writes_system_prompt(self, repo_root: Path) -> None:
        wire_compat("opencode", port=8000, root=repo_root)
        prompt = repo_root / ".opencode" / "system-prompt.md"
        assert prompt.exists()
        content = prompt.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content

    def test_idempotent_rewire(self, repo_root: Path) -> None:
        wire_compat("opencode", port=8000, root=repo_root)
        wire_compat("opencode", port=9000, root=repo_root)
        prompt = (repo_root / ".opencode" / "system-prompt.md").read_text()
        env = (repo_root / ".opencode" / ".agentalloy-env").read_text()
        assert "localhost:9000" in prompt
        assert "localhost:8000" not in prompt
        assert prompt.count(SENTINEL_BEGIN) == 1
        assert "localhost:9000" in env

    def test_prints_activation_guidance(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wire_compat("opencode", port=8000, root=repo_root)
        captured = capsys.readouterr()
        assert "source" in captured.err
        assert ".agentalloy-env" in captured.err


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
        wire_compat("hermes-agent", port=8000, root=tmp_path, scope="user", legacy=True)
        content = (tmp_path / ".hermes" / "SOUL.md").read_text()
        for marker in self._INTAKE_MARKERS:
            assert marker in content, f"Missing marker: {marker}"

    def test_gemini_cli_has_intake_markers(self, repo_root: Path) -> None:
        """gemini-cli uses markdown injection (not hooks like claude-code)."""
        wire_compat("gemini-cli", port=8000, root=repo_root, legacy=True)
        content = (repo_root / "GEMINI.md").read_text()
        for marker in self._INTAKE_MARKERS:
            assert marker in content, f"Missing marker: {marker}"

    def test_all_harnesses_have_phase_reference(self, repo_root: Path) -> None:
        """Smoke test: every instruction-bearing harness file references .agentalloy/phase.

        Only checks .md and .mdc files — structured config files (.json, .yml, .toml)
        may encode phase references differently and are not required to contain the
        literal string.
        """
        instruction_extensions = {".md", ".mdc"}
        # Harnesses that do not support legacy markdown injection
        legacy_excluded = {"mcp-only", "codex", "openclaw"}
        for harness in VALID_HARNESSES:
            state_file = repo_root / ".agentalloy" / "install-state.json"
            if state_file.exists():
                state_file.unlink()
            if harness in legacy_excluded:
                continue
            result = wire_compat(harness, port=8000, root=repo_root, legacy=True)
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

        result = wire_compat("claude-code", port=9999, mcp_fallback=True)
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
        result = wire_compat("cursor", port=8888, root=repo_root, mcp_fallback=True)
        assert result["integration_vector"] == "mcp_server_config"

        mcp_config = repo_root / ".cursor" / "mcp.json"
        assert mcp_config.exists()
        config = json.loads(mcp_config.read_text())
        assert "agentalloy" in config["mcpServers"]
        entry = config["mcpServers"]["agentalloy"]
        assert entry["args"] == ["-m", "agentalloy.install.mcp_server", "--port", "8888"]

    def test_continue_closed_mcp_writes_continuerc(self, repo_root: Path) -> None:
        """continue-closed --mcp-fallback writes MCP entry to .continuerc.json."""
        result = wire_compat("continue-closed", port=7777, root=repo_root, mcp_fallback=True)
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
        wire_compat("continue-local", port=7777, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert config["_agentalloy_install_marker"]["variant"] == "mcp-local"

    def test_unsupported_harness_raises(self, repo_root: Path) -> None:
        """--mcp-fallback on unsupported harness raises SystemExit(1)."""
        with pytest.raises(SystemExit, match=".*"):
            wire_compat("hermes-agent", root=repo_root, mcp_fallback=True)

    def test_preserves_existing_mcp_servers(self, repo_root: Path) -> None:
        """Existing MCP server entries survive re-wiring."""
        (repo_root / ".cursor").mkdir()
        existing: dict[str, Any] = {
            "mcpServers": {"other-server": {"command": "other", "args": []}}
        }
        (repo_root / ".cursor" / "mcp.json").write_text(json.dumps(existing))
        wire_compat("cursor", port=8000, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".cursor" / "mcp.json").read_text())
        assert "other-server" in config["mcpServers"]
        assert "agentalloy" in config["mcpServers"]

    def test_uses_sys_executable(self, repo_root: Path) -> None:
        """MCP server entry uses sys.executable, not bare 'python'."""
        import sys

        wire_compat("cursor", port=8000, root=repo_root, mcp_fallback=True)
        config = json.loads((repo_root / ".cursor" / "mcp.json").read_text())
        entry = config["mcpServers"]["agentalloy"]
        assert entry["command"] == sys.executable


# ---------------------------------------------------------------------------
# Proxy wiring
# ---------------------------------------------------------------------------


class TestProxyWiring:
    """Tests for default proxy wiring path."""

    def test_continue_closed_proxy_writes_models(self, repo_root: Path) -> None:
        """continue-closed default wiring adds a proxy model to .continuerc.json."""
        result = wire_compat("continue-closed", port=9999, root=repo_root)
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
        """continue-local default wiring uses proxy-local variant marker."""
        wire_compat("continue-local", port=8888, root=repo_root)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        assert config["_agentalloy_install_marker"]["variant"] == "proxy-local"

    def test_proxy_idempotent(self, repo_root: Path) -> None:
        """Re-wiring proxy removes old entry and adds new one."""
        wire_compat("continue-closed", port=9999, root=repo_root)
        wire_compat("continue-closed", port=7777, root=repo_root)
        config = json.loads((repo_root / ".continuerc.json").read_text())
        models = config.get("models", [])
        proxy_models = [m for m in models if m.get("agentalloy_proxy")]
        assert len(proxy_models) == 1
        assert proxy_models[0]["apiBase"] == "http://localhost:7777/v1"

    def test_claude_code_proxy_writes_env_file(
        self, repo_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude-code default wiring writes ~/.agentalloy/claude-code-env.sh."""
        fake_home = repo_root / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("claude-code", port=5555, root=repo_root)
        assert result["integration_vector"] == "proxy"

        env_path = fake_home / ".agentalloy" / "claude-code-env.sh"
        assert env_path.exists()
        content = env_path.read_text()
        assert SENTINEL_BEGIN in content
        assert "ANTHROPIC_BASE_URL=http://localhost:5555" in content
        assert "5555/v1" not in content  # SDK appends /v1/messages
        assert "ANTHROPIC_API_KEY=agentalloy" in content

    def test_manual_proxy_prints_to_stderr(
        self, repo_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """manual default wiring emits proxy instruction to stderr."""
        result = wire_compat("manual", port=4444, root=repo_root)
        assert result["integration_vector"] == "proxy"
        assert result["files_written"] == []
        captured = capsys.readouterr()
        assert SENTINEL_BEGIN in captured.err
        assert "localhost:4444" in captured.err

    def test_cursor_proxy_instruction(self, repo_root: Path) -> None:
        """cursor default wiring writes proxy instruction block."""
        (repo_root / ".cursor").mkdir()
        result = wire_compat("cursor", port=6666, root=repo_root)
        assert result["integration_vector"] == "proxy"
        mdc = repo_root / ".cursor" / "rules" / "agentalloy.mdc"
        assert mdc.exists()
        content = mdc.read_text()
        assert "localhost:6666" in content
        assert "proxy" in content.lower()

    def test_hermes_agent_proxy_user_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hermes-agent default proxy wiring with user scope writes to ~/.hermes/config.yaml."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = wire_compat("hermes-agent", port=3333, root=tmp_path, scope="user")
        assert result["integration_vector"] == "proxy"
        config = fake_home / ".hermes" / "config.yaml"
        assert config.exists()
        content = config.read_text()
        assert "localhost:3333" in content
        assert "custom_providers" in content

    def test_mcp_only_with_proxy_rejected(self, repo_root: Path) -> None:
        """mcp-only harness is rejected (blocked by top-level check)."""
        with pytest.raises(SystemExit):
            wire_compat("mcp-only", port=8000, root=repo_root)


class TestLegacyFlag:
    """Verify --legacy flag routes to markdown-injection wiring."""

    def test_legacy_flag_uses_markdown_injection(self, repo_root: Path) -> None:
        """--legacy writes the markdown-injection block (gemini-cli, not claude-code)."""
        # claude-code legacy path uses hooks; gemini-cli uses markdown injection
        result = wire_compat("gemini-cli", port=8000, root=repo_root, legacy=True)
        assert result["integration_vector"] == "markdown_injection"
        gemini_md = repo_root / "GEMINI.md"
        assert gemini_md.exists()
        content = gemini_md.read_text()
        assert SENTINEL_BEGIN in content
        assert "localhost:8000" in content

    def test_default_is_proxy_not_legacy(self, repo_root: Path) -> None:
        """Calling without --legacy uses proxy by default."""
        result = wire_compat("claude-code", port=8000, root=repo_root)
        assert result["integration_vector"] == "proxy"

    def test_legacy_false_is_proxy(self, repo_root: Path) -> None:
        """Explicitly passing legacy=False gives proxy wiring."""
        result = wire_compat("claude-code", port=8000, root=repo_root, legacy=False)
        assert result["integration_vector"] == "proxy"

    def test_mcp_fallback_ignores_legacy(self, repo_root: Path) -> None:
        """--mcp-fallback takes precedence; legacy=True is ignored."""
        result = wire_compat(
            "claude-code", port=8000, root=repo_root, mcp_fallback=True, legacy=True
        )
        assert result["integration_vector"] == "mcp_server_config"


# ---------------------------------------------------------------------------
# Uninstall proxy
# ---------------------------------------------------------------------------


class TestUninstallProxy:
    """Tests for uninstall_proxy module functions."""

    def test_unwire_proxy_aider_removes_block(self, repo_root: Path) -> None:
        """Unwire removes sentinel block from .aider.conf.yml."""
        # Create a .aider.conf.yml with proxy block
        conf = repo_root / ".aider.conf.yml"
        conf.write_text(f"# Before\n{SENTINEL_BEGIN}\nproxy config here\n{SENTINEL_END}\n# After\n")
        # Also create the instructions file (so it gets removed)
        instr = repo_root / ".agentalloy-aider-instructions.md"
        instr.write_text("# Instructions\n")

        removed = uninstall_proxy._unwire_proxy_aider(repo_root)
        assert conf.exists()
        content = conf.read_text()
        assert SENTINEL_BEGIN not in content
        assert "proxy config here" not in content
        assert "# Before" in content
        assert "# After" in content
        # Both files should be removed
        assert not instr.exists()
        assert len(removed) == 2

    def test_unwire_proxy_aider_no_file(self, repo_root: Path) -> None:
        """Unwire no-ops if .aider.conf.yml doesn't exist."""
        removed = uninstall_proxy._unwire_proxy_aider(repo_root)
        assert removed == []

    def test_unwire_proxy_hermes_agent_user_scope(self, tmp_path: Path) -> None:
        """Unwire user-scope hermes-agent from ~/.hermes/config.yaml."""
        import os as _os

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        _os.environ["HOME"] = str(fake_home)

        # Mock Path.home by replacing the function on the Path class
        from pathlib import Path as _Path

        original_home = _Path.home
        _Path.home = lambda: fake_home

        try:
            config = fake_home / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                f"# Before\n{SENTINEL_BEGIN}\nhermes proxy config\n{SENTINEL_END}\n# After\n"
            )
            removed = uninstall_proxy._unwire_proxy_hermes_agent("user", fake_home)
            assert config.exists()
            content = config.read_text()
            assert SENTINEL_BEGIN not in content
            assert len(removed) == 1
        finally:
            _Path.home = original_home

    def test_unwire_proxy_hermes_agent_repo_scope(self, repo_root: Path) -> None:
        """Unwire repo-scope hermes-agent from AGENTS.md."""
        agents = repo_root / "AGENTS.md"
        agents.write_text(
            f"# Before\n{SENTINEL_BEGIN}\nhermes proxy config\n{SENTINEL_END}\n# After\n"
        )
        removed = uninstall_proxy._unwire_proxy_hermes_agent("repo", repo_root)
        assert agents.exists()
        content = agents.read_text()
        assert SENTINEL_BEGIN not in content
        assert len(removed) == 1

    def test_unwire_proxy_opencode_removes_files(self, repo_root: Path) -> None:
        """Unwire removes opencode env file and sentinel block from prompt."""
        opencode_dir = repo_root / ".opencode"
        opencode_dir.mkdir()
        env_file = opencode_dir / ".agentalloy-env"
        prompt_file = repo_root / ".opencode" / "system-prompt.md"
        env_file.write_text("ENV_VAR=value\n")
        # Prompt file has user content + sentinel block
        prompt_file.write_text(
            "My custom prompt\n\n"
            f"{SENTINEL_BEGIN}\nproxy block\n{SENTINEL_END}\n"
            "More user content\n"
        )

        removed = uninstall_proxy._unwire_proxy_opencode(repo_root)
        assert not env_file.exists()
        # Prompt file preserved, sentinel block removed
        assert prompt_file.exists()
        content = prompt_file.read_text()
        assert "My custom prompt" in content
        assert "More user content" in content
        assert SENTINEL_BEGIN not in content
        assert len(removed) == 2

    def test_unwire_proxy_opencode_deletes_prompt_if_only_sentinel(self, repo_root: Path) -> None:
        """Unwire deletes prompt file if it contains only the sentinel block."""
        opencode_dir = repo_root / ".opencode"
        opencode_dir.mkdir()
        env_file = opencode_dir / ".agentalloy-env"
        prompt_file = repo_root / ".opencode" / "system-prompt.md"
        env_file.write_text("ENV_VAR=value\n")
        # Prompt file has only the sentinel block
        prompt_file.write_text(f"{SENTINEL_BEGIN}\nproxy block\n{SENTINEL_END}\n")

        removed = uninstall_proxy._unwire_proxy_opencode(repo_root)
        assert not env_file.exists()
        assert not prompt_file.exists()
        assert len(removed) == 2

    def test_unwire_proxy_claude_code_removes_env(self, tmp_path: Path) -> None:
        """Unwire removes claude-code env file sentinel block."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        from pathlib import Path as _Path

        original_home = _Path.home
        _Path.home = lambda: fake_home

        try:
            import os as _os

            _os.environ["HOME"] = str(fake_home)
            env_file = fake_home / ".agentalloy" / "claude-code-env.sh"
            env_file.parent.mkdir(parents=True)
            # Env file has user content + sentinel block
            env_file.write_text(
                "# Existing env\nexport FOO=bar\n"
                f"{SENTINEL_BEGIN}\nproxy block\n{SENTINEL_END}\n"
                "# More user content\n"
            )

            removed = uninstall_proxy._unwire_proxy_claude_code(fake_home)
            # Env file preserved, sentinel block removed
            assert env_file.exists()
            content = env_file.read_text()
            assert "Existing env" in content
            assert "More user content" in content
            assert SENTINEL_BEGIN not in content
            assert len(removed) == 1
        finally:
            _Path.home = original_home

    def test_unwire_proxy_cline_removes_settings(self, tmp_path: Path) -> None:
        """Unwire removes .cline/settings.json if only proxy fields exist."""
        settings_dir = tmp_path / ".cline"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        # Write only proxy fields
        settings_file.write_text(
            json.dumps(
                {
                    "apiProvider": "openai",
                    "apiBaseUrl": "http://localhost:8000/v1",
                    "apiKey": "agentalloy",
                    "model": "agentalloy-proxy",
                },
                indent=2,
            )
        )

        removed = uninstall_proxy._unwire_proxy_cline(tmp_path)
        assert not settings_file.exists()
        assert len(removed) == 1

    def test_unwire_proxy_cline_preserves_other_settings(self, tmp_path: Path) -> None:
        """Unwire removes only proxy fields, keeps other settings."""
        settings_dir = tmp_path / ".cline"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        # Write with both proxy and user settings
        settings_file.write_text(
            json.dumps(
                {
                    "apiProvider": "openai",
                    "apiBaseUrl": "http://localhost:8000/v1",
                    "apiKey": "agentalloy",
                    "model": "agentalloy-proxy",
                    "modelId": "claude-3-sonnet",
                    "someUserSetting": "keep this",
                },
                indent=2,
            )
        )

        _removed = uninstall_proxy._unwire_proxy_cline(tmp_path)
        assert settings_file.exists()
        config = json.loads(settings_file.read_text())
        # Proxy fields removed
        assert "apiProvider" not in config
        assert "apiBaseUrl" not in config
        assert "apiKey" not in config
        assert "model" not in config
        # User fields preserved
        assert config["modelId"] == "claude-3-sonnet"
        assert config["someUserSetting"] == "keep this"

    def test_unwire_proxy_cline_no_file(self, tmp_path: Path) -> None:
        """Unwire no-ops if .cline/settings.json doesn't exist."""
        removed = uninstall_proxy._unwire_proxy_cline(tmp_path)
        assert removed == []

    def test_unwire_proxy_cline_invalid_json(self, tmp_path: Path) -> None:
        """Unwire handles invalid JSON gracefully without crashing."""
        settings_dir = tmp_path / ".cline"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        # Write invalid JSON
        settings_file.write_text("{ invalid json }")

        removed = uninstall_proxy._unwire_proxy_cline(tmp_path)
        # Should return empty list (no crash)
        assert removed == []
        # File should still exist (not deleted)
        assert settings_file.exists()

    def test_unwire_proxy_cline_preserves_non_proxy_settings(self, tmp_path: Path) -> None:
        """Unwire only removes keys with AgentAlloy proxy values."""
        settings_dir = tmp_path / ".cline"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        # Write with user's own settings that happen to use same keys
        # but different values
        settings_file.write_text(
            json.dumps(
                {
                    "apiProvider": "anthropic",  # Not "openai"
                    "apiBaseUrl": "https://api.anthropic.com",  # Not localhost
                    "apiKey": "sk-ant-***",  # Not "***" or "agentalloy"
                    "model": "claude-3-sonnet",  # Not "agentalloy-proxy"
                },
                indent=2,
            )
        )

        removed = uninstall_proxy._unwire_proxy_cline(tmp_path)
        # Should not remove anything — values don't match AgentAlloy proxy
        assert removed == []
        # File should still exist with original content
        assert settings_file.exists()
        config = json.loads(settings_file.read_text())
        assert config["apiProvider"] == "anthropic"
        assert config["model"] == "claude-3-sonnet"

    def test_remove_sentinel_block_commented_variants(self, tmp_path: Path) -> None:
        """_remove_sentinel_block handles both raw and commented sentinels."""
        # Test with commented sentinels (YAML/shell style)
        content = (
            "# Before\n"
            "# <!-- BEGIN agentalloy install -->\n"
            "# proxy config\n"
            "# <!-- END agentalloy install -->\n"
            "# After\n"
        )
        result = uninstall_proxy._remove_sentinel_block(content)
        # Commented sentinel block should be removed
        assert "proxy config" not in result
        assert "# Before" in result
        assert "# After" in result
        # No dangling '#' fragments
        assert result.count("# ") <= 2  # Only "Before" and "After"

    def test_remove_sentinel_block_no_op_without_sentinels(self, tmp_path: Path) -> None:
        """_remove_sentinel_block returns content unchanged when no sentinels found."""
        content = "# Normal content\n\n# More content\n\n\n# Final line\n"
        result = uninstall_proxy._remove_sentinel_block(content)
        # Should be identical — no reformatting
        assert result == content
