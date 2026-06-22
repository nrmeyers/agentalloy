"""Tests for Claude Code proxy wiring. Maps to Step 8.

Auth-transparent native passthrough (task A10): the carrier is a per-repo
``<root>/.agentalloy/claude-code-env.sh`` that exports ONLY
``ANTHROPIC_BASE_URL`` (with the repo's ``/proj/<token>`` discriminator) and
never ``ANTHROPIC_API_KEY`` — setting an API key would break account/OAuth auth.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentalloy.api.proxy_context import decode_proj_token, encode_proj_token
from tests._wire_compat import wire_compat


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


class TestClaudeCodeProxyWiring:
    """Tests for claude-code proxy wiring via <root>/.agentalloy/claude-code-env.sh."""

    def test_claude_code_proxy_writes_per_repo_env_script(self, tmp_path: Path) -> None:
        """Default claude-code wiring writes claude-code-env.sh to <root>/.agentalloy/."""
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        assert result["integration_vector"] == "proxy"
        assert result["harness"] == "claude-code"

        env_path = tmp_path / ".agentalloy" / "claude-code-env.sh"
        assert env_path.exists()

    def test_claude_code_proxy_base_url_carries_proj_token(self, tmp_path: Path) -> None:
        """The base URL embeds /proj/<token> decoding back to realpath(root)."""
        wire_compat("claude-code", port=7070, root=tmp_path)
        content = (tmp_path / ".agentalloy" / "claude-code-env.sh").read_text()

        token = encode_proj_token(tmp_path)
        assert f"ANTHROPIC_BASE_URL=http://localhost:7070/proj/{token}" in content
        # No /v1 suffix: the Anthropic SDK appends /v1/messages to the base URL.
        assert "7070/v1" not in content

        decoded = decode_proj_token(token)
        assert str(decoded) == os.path.realpath(tmp_path)

    def test_claude_code_proxy_never_sets_api_key(self, tmp_path: Path) -> None:
        """Auth transparency: NO ANTHROPIC_API_KEY line is written at all."""
        wire_compat("claude-code", port=7070, root=tmp_path)
        content = (tmp_path / ".agentalloy" / "claude-code-env.sh").read_text()
        assert "ANTHROPIC_API_KEY" not in content

    def test_claude_code_proxy_uses_sentinel_markers(self, tmp_path: Path) -> None:
        """The env script block is bounded by sentinel comments."""
        wire_compat("claude-code", port=7070, root=tmp_path)
        content = (tmp_path / ".agentalloy" / "claude-code-env.sh").read_text()
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_claude_code_proxy_idempotent(self, tmp_path: Path) -> None:
        """Re-running claude-code proxy wiring replaces the existing block."""
        wire_compat("claude-code", port=7070, root=tmp_path)
        wire_compat("claude-code", port=8080, root=tmp_path)
        content = (tmp_path / ".agentalloy" / "claude-code-env.sh").read_text()
        assert "localhost:8080" in content
        assert "localhost:7070" not in content
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1

    def test_claude_code_proxy_no_envrc_emits_hint(self, tmp_path: Path) -> None:
        """With no .envrc, a carrier hint rides on the env-file record; no .envrc created."""
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        assert not (tmp_path / ".envrc").exists()

        env_entries = [
            f
            for f in result["files_written"]
            if str(f.get("path", "")).endswith(".agentalloy/claude-code-env.sh")
        ]
        assert len(env_entries) == 1
        hint = env_entries[0].get("carrier_hint")
        assert hint is not None
        assert ".agentalloy/claude-code-env.sh" in hint
        assert "source_env .agentalloy/claude-code-env.sh" in hint

    def test_claude_code_proxy_appends_source_env_to_existing_envrc(self, tmp_path: Path) -> None:
        """A pre-existing .envrc gets a sentinel-bounded source_env line appended."""
        envrc = tmp_path / ".envrc"
        envrc.write_text("export EXISTING=1\n")

        wire_compat("claude-code", port=7070, root=tmp_path)
        content = envrc.read_text()
        # Pre-existing content preserved.
        assert "export EXISTING=1" in content
        # direnv stdlib source_env, sentinel-bounded.
        assert "source_env .agentalloy/claude-code-env.sh" in content
        assert "# <!-- BEGIN agentalloy install -->" in content
        assert "# <!-- END agentalloy install -->" in content

    def test_claude_code_proxy_envrc_idempotent(self, tmp_path: Path) -> None:
        """Re-wiring does not duplicate the .envrc source_env line."""
        envrc = tmp_path / ".envrc"
        envrc.write_text("export EXISTING=1\n")

        wire_compat("claude-code", port=7070, root=tmp_path)
        wire_compat("claude-code", port=8080, root=tmp_path)
        content = envrc.read_text()
        assert content.count("source_env .agentalloy/claude-code-env.sh") == 1
        assert content.count("# <!-- BEGIN agentalloy install -->") == 1
        # Original content survives both passes.
        assert "export EXISTING=1" in content


class TestClaudeCodeRegistryAuthTransparency:
    """The provider-registry paths (install_writer + env_builder) used by `wrap`
    and registry wiring must be auth-transparent too — they previously set a dummy
    ANTHROPIC_API_KEY + bare URL, which broke account auth and missed the native
    passthrough route.
    """

    def test_install_writer_no_key_carries_proj_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentalloy.providers.claude_code import install

        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("HOME", str(home))

        install.apply_persistent_config(7070, repo)
        content = (home / ".agentalloy" / "claude-code-env.sh").read_text()

        assert "ANTHROPIC_API_KEY" not in content
        assert f"http://localhost:7070/proj/{encode_proj_token(repo)}" in content
        assert "7070/v1" not in content  # no /v1 suffix

    def test_env_builder_and_launch_env_no_key_carry_proj_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agentalloy.providers.claude_code as cc
        from agentalloy.providers.claude_code import runtime

        monkeypatch.chdir(tmp_path)
        token = encode_proj_token(tmp_path)
        for env in (cc._env_builder(7070), runtime.build_launch_env(7070)):
            assert "ANTHROPIC_API_KEY" not in env
            assert env["ANTHROPIC_BASE_URL"] == f"http://localhost:7070/proj/{token}"
