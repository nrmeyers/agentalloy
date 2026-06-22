"""Tests for Claude Code proxy wiring. Maps to Step 8.

Auth-transparent native passthrough (task A10): the carrier is a per-repo
``<root>/.agentalloy/claude-code-env.sh`` that exports ONLY
``ANTHROPIC_BASE_URL`` (with the repo's ``/proj/<token>`` discriminator) and
never ``ANTHROPIC_API_KEY`` — setting an API key would break account/OAuth auth.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agentalloy.api.proxy_context import decode_proj_token, encode_proj_token
from tests._wire_compat import wire_compat

# Uninstall module namespace for patching state/proxy seams.
_UNINSTALL = "agentalloy.install.subcommands.uninstall"


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

    def test_claude_code_proxy_no_envrc_auto_wires_no_hint(self, tmp_path: Path) -> None:
        """With no .envrc, settings.local.json is the carrier — no must-source hint.

        Previously (env-file only) a carrier hint rode on the env-file record. Now
        the settings.local.json ``env`` block auto-loads, so the hint is suppressed.
        """
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        assert not (tmp_path / ".envrc").exists()
        # settings.local.json carrier was written and auto-loads.
        assert (tmp_path / ".claude" / "settings.local.json").exists()

        env_entries = [
            f
            for f in result["files_written"]
            if str(f.get("path", "")).endswith(".agentalloy/claude-code-env.sh")
        ]
        assert len(env_entries) == 1
        assert env_entries[0].get("carrier_hint") is None

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


class TestClaudeCodeSettingsCarrier:
    """The primary auto-load carrier: .claude/settings.local.json `env` block.

    Claude Code reads `env` natively, so the proxy URL loads with no shell/direnv
    step. Auth-transparent (never an API key) and the merge preserves user keys.
    """

    @staticmethod
    def _settings(root: Path) -> dict:
        return json.loads((root / ".claude" / "settings.local.json").read_text())

    def test_wire_writes_settings_env_base_url(self, tmp_path: Path) -> None:
        wire_compat("claude-code", port=7070, root=tmp_path)
        token = encode_proj_token(tmp_path)
        data = self._settings(tmp_path)
        assert data["env"]["ANTHROPIC_BASE_URL"] == f"http://localhost:7070/proj/{token}"
        # round-trips back to this repo
        assert str(decode_proj_token(token)) == os.path.realpath(tmp_path)

    def test_wire_settings_never_sets_api_key(self, tmp_path: Path) -> None:
        wire_compat("claude-code", port=7070, root=tmp_path)
        raw = (tmp_path / ".claude" / "settings.local.json").read_text()
        assert "ANTHROPIC_API_KEY" not in raw

    def test_wire_settings_preserves_existing_keys(self, tmp_path: Path) -> None:
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "env": {"FOO": "bar"}})
        )
        wire_compat("claude-code", port=7070, root=tmp_path)
        data = self._settings(tmp_path)
        assert data["permissions"]["allow"] == ["Bash(ls)"]
        assert data["env"]["FOO"] == "bar"
        assert "ANTHROPIC_BASE_URL" in data["env"]

    def test_wire_settings_idempotent(self, tmp_path: Path) -> None:
        wire_compat("claude-code", port=7070, root=tmp_path)
        wire_compat("claude-code", port=8080, root=tmp_path)
        data = self._settings(tmp_path)
        assert data["env"]["ANTHROPIC_BASE_URL"].endswith(
            ":8080/proj/" + encode_proj_token(tmp_path)
        )

    def test_wire_malformed_settings_falls_back_to_hint(self, tmp_path: Path) -> None:
        """A malformed settings.local.json is left untouched; the env-file hint returns."""
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.local.json").write_text("{ not valid json")
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        # Not clobbered.
        assert (claude / "settings.local.json").read_text() == "{ not valid json"
        # No .envrc + no settings carrier ⇒ must-source hint is restored.
        env_entries = [
            f
            for f in result["files_written"]
            if str(f.get("path", "")).endswith(".agentalloy/claude-code-env.sh")
        ]
        assert env_entries and env_entries[0].get("carrier_hint") is not None


class TestClaudeCodeUnwireCleanup:
    """`unwire` strips only our settings env key and leaves no empty .agentalloy husk."""

    def test_unwire_strips_only_our_base_url(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import uninstall_proxy

        claude = tmp_path / ".claude"
        claude.mkdir()
        token = encode_proj_token(tmp_path)
        (claude / "settings.local.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls)"]},
                    "env": {
                        "ANTHROPIC_BASE_URL": f"http://localhost:7070/proj/{token}",
                        "FOO": "bar",
                    },
                }
            )
        )
        removed = uninstall_proxy._unwire_proxy_claude_code_settings(tmp_path)
        assert removed == [claude / "settings.local.json"]
        data = json.loads((claude / "settings.local.json").read_text())
        assert "ANTHROPIC_BASE_URL" not in data["env"]
        assert data["env"]["FOO"] == "bar"  # sibling env key preserved
        assert data["permissions"]["allow"] == ["Bash(ls)"]  # other keys preserved

    def test_unwire_drops_empty_env_block(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import uninstall_proxy

        claude = tmp_path / ".claude"
        claude.mkdir()
        token = encode_proj_token(tmp_path)
        (claude / "settings.local.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls)"]},
                    "env": {"ANTHROPIC_BASE_URL": f"http://localhost:7070/proj/{token}"},
                }
            )
        )
        uninstall_proxy._unwire_proxy_claude_code_settings(tmp_path)
        data = json.loads((claude / "settings.local.json").read_text())
        assert "env" not in data  # emptied env block dropped entirely
        assert "permissions" in data  # rest of the file survives

    def test_unwire_preserves_users_own_base_url(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import uninstall_proxy

        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my-own-proxy.example.com"}})
        )
        removed = uninstall_proxy._unwire_proxy_claude_code_settings(tmp_path)
        assert removed == []  # not our /proj/ URL → untouched
        data = json.loads((claude / "settings.local.json").read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://my-own-proxy.example.com"

    def test_unwire_unlinks_file_when_only_our_content(self, tmp_path: Path) -> None:
        from agentalloy.install.subcommands import uninstall_proxy

        claude = tmp_path / ".claude"
        claude.mkdir()
        token = encode_proj_token(tmp_path)
        (claude / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://localhost:7070/proj/{token}"}})
        )
        uninstall_proxy._unwire_proxy_claude_code_settings(tmp_path)
        assert not (claude / "settings.local.json").exists()  # nothing left → removed

    def _run_unwire(self, tmp_path: Path, st: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentalloy.install.subcommands.uninstall import uninstall

        fake_home = tmp_path / "home"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        with (
            patch(f"{_UNINSTALL}.install_state.load_state", return_value=st),
            patch(f"{_UNINSTALL}.install_state.save_state"),
            patch(f"{_UNINSTALL}.install_state.is_inside_root", return_value=True),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_aider", return_value=[]),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_opencode", return_value=[]),
            patch(f"{_UNINSTALL}.uninstall_proxy._unwire_proxy_cline", return_value=[]),
        ):
            uninstall(
                remove_data=False,
                force=False,
                root=tmp_path,
                remove_user_state=False,
                remove_env=False,
                all_repos=False,
                remove_models=False,
                remove_wiring=True,
                stop_services=False,
            )

    def test_unwire_removes_empty_agentalloy_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        agentalloy = tmp_path / ".agentalloy"
        (agentalloy / "phase").write_text("build\n")  # lifecycle state wire seeds
        assert (agentalloy / "claude-code-env.sh").exists()

        self._run_unwire(tmp_path, {"harness_files_written": result["files_written"]}, monkeypatch)
        assert not agentalloy.exists()  # empty husk removed, no trace left

    def test_unwire_preserves_agentalloy_dir_with_contracts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = wire_compat("claude-code", port=7070, root=tmp_path)
        agentalloy = tmp_path / ".agentalloy"
        contract = agentalloy / "contracts" / "spec.md"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text("# user work\n")

        self._run_unwire(tmp_path, {"harness_files_written": result["files_written"]}, monkeypatch)
        assert agentalloy.exists()  # preserved — contracts/ is user work
        assert contract.read_text() == "# user work\n"
        assert not (agentalloy / "claude-code-env.sh").exists()  # but our carrier is gone
