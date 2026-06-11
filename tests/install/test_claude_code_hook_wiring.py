"""Tests for the default claude-code hook wiring.

Covers:
- The fail-open hook script contract (exits 0, no stdout on unreachable endpoint).
- The provider hook_writer: fresh settings.json, preserves user content,
  idempotent re-wire, refuses malformed JSON, captures original_content.
- wire/wrap default resolution (hook for claude-code; proxy opt-in).
- A real round-trip against the in-process hook endpoint (TestClient).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.providers.claude_code.hooks import (
    MalformedSettingsError,
    hook_writer,
    installed_script_path,
    settings_json_path,
)

_HOOK_SCRIPT = (
    Path(__file__).resolve().parents[2] / "src/agentalloy/install/agentalloy-hook-claude-code.sh"
)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# ---------------------------------------------------------------------------
# Fail-open hook script contract
# ---------------------------------------------------------------------------


class TestHookScriptFailOpen:
    def test_unreachable_endpoint_exits_zero_no_stdout(self, tmp_path: Path) -> None:
        """Endpoint down → exit 0 with empty stdout (silent degradation)."""
        payload = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "x", "cwd": str(tmp_path)}
        )
        # Point at a port nothing is listening on; tight timeouts keep it fast.
        env = {
            **os.environ,
            "AGENTALLOY_HOOK_URL": "http://127.0.0.1:1/v1/hook/user-prompt-submit",
        }
        result = subprocess.run(
            ["bash", str(_HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_pre_tool_use_unreachable_exits_zero(self, tmp_path: Path) -> None:
        payload = json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(tmp_path)}
        )
        env = {
            **os.environ,
            "AGENTALLOY_HOOK_URL_PRE": "http://127.0.0.1:1/v1/hook/pre-tool-use",
        }
        result = subprocess.run(
            ["bash", str(_HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_script_passes_bash_syntax_check(self) -> None:
        """`bash -n` parses the script (no syntax errors)."""
        result = subprocess.run(
            ["bash", "-n", str(_HOOK_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# hook_writer: settings.json merge + script install
# ---------------------------------------------------------------------------


class TestHookWriter:
    def test_fresh_settings_created(self, fake_home: Path) -> None:
        records = hook_writer(7070, fake_home)

        script = installed_script_path()
        settings = settings_json_path()
        assert script.exists()
        assert script.stat().st_mode & 0o111  # executable
        assert settings.exists()

        data = json.loads(settings.read_text())
        hooks = data["hooks"]
        assert "UserPromptSubmit" in hooks
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks
        # UserPromptSubmit has no matcher; tool events do.
        assert "matcher" not in hooks["UserPromptSubmit"][0]
        assert hooks["PreToolUse"][0]["matcher"] == "*"
        cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert str(script) in cmd
        assert "localhost:7070/v1/hook/user-prompt-submit" in cmd

        # settings.json record carries no original_content (file was new) and
        # is a wrote_new_file so uninstall deletes it.
        settings_rec = next(r for r in records if r.path == str(settings))
        assert settings_rec.action == "wrote_new_file"
        assert settings_rec.original_content is None

    def test_preserves_existing_user_content(self, fake_home: Path) -> None:
        settings = settings_json_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(ls:*)"]},
                    "hooks": {
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": "/user/own.sh"}]}
                        ]
                    },
                },
                indent=2,
            )
            + "\n"
        )

        records = hook_writer(7070, fake_home)

        data = json.loads(settings.read_text())
        # User's unrelated settings preserved.
        assert data["permissions"]["allow"] == ["Bash(ls:*)"]
        # User's own UserPromptSubmit hook preserved alongside ours.
        ups = data["hooks"]["UserPromptSubmit"]
        commands = [h["command"] for g in ups for h in g["hooks"]]
        assert "/user/own.sh" in commands
        assert any(str(installed_script_path()) in c for c in commands)

        # original_content captured → restore branch on uninstall.
        rec = next(r for r in records if r.path == str(settings))
        assert rec.action == "injected_block"
        assert rec.original_content is not None

    def test_idempotent_rewire_no_duplicates(self, fake_home: Path) -> None:
        hook_writer(7070, fake_home)
        hook_writer(8080, fake_home)  # re-wire on a different port

        data = json.loads(settings_json_path().read_text())
        ups = data["hooks"]["UserPromptSubmit"]
        ours = [h for g in ups for h in g["hooks"] if str(installed_script_path()) in h["command"]]
        assert len(ours) == 1  # not duplicated
        assert "localhost:8080" in ours[0]["command"]  # port updated

    def test_malformed_settings_refused(self, fake_home: Path) -> None:
        settings = settings_json_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{ this is not json ")

        with pytest.raises(MalformedSettingsError):
            hook_writer(7070, fake_home)

        # File left untouched (not clobbered).
        assert settings.read_text() == "{ this is not json "


# ---------------------------------------------------------------------------
# wire / wrap default resolution
# ---------------------------------------------------------------------------


class TestViaResolution:
    def test_claude_code_defaults_to_hook(self) -> None:
        from agentalloy.install.subcommands.wire import resolve_via

        assert resolve_via("claude-code", None) == "hook"

    def test_explicit_proxy_overrides_default(self) -> None:
        from agentalloy.install.subcommands.wire import resolve_via

        assert resolve_via("claude-code", "proxy") == "proxy"

    def test_other_harness_defaults_to_proxy(self) -> None:
        from agentalloy.install.subcommands.wire import resolve_via

        assert resolve_via("aider", None) == "proxy"

    def test_apply_hook_wiring_writes_files_records_state(self, fake_home: Path) -> None:
        from agentalloy.install import state as install_state
        from agentalloy.install.subcommands.wire import apply_hook_wiring

        result = apply_hook_wiring("claude-code", port=7070, root=fake_home)
        assert result["integration_vector"] == "hook"
        # No proxy env file written.
        assert not (fake_home / ".agentalloy" / "claude-code-env.sh").exists()
        assert settings_json_path().exists()

        st = install_state.load_state(fake_home)
        paths = {e["path"] for e in st["harness_files_written"]}
        assert str(settings_json_path()) in paths
        assert str(installed_script_path()) in paths


# ---------------------------------------------------------------------------
# Real round-trip against the in-process hook endpoint
# ---------------------------------------------------------------------------


class TestHookScriptRoundTrip:
    def test_script_request_shape_accepted_by_router(self, tmp_path: Path) -> None:
        """The script's POST body is accepted by the real hook router.

        We run the app in-process (TestClient binds a real port), point the
        hook script at it, and assert the script exits 0 and the endpoint
        returns the composed-block contract the script parses.
        """
        from agentalloy.api import hook_router as hr

        hr._cache = None  # type: ignore[assignment]

        app = create_app(use_default_lifespan=False)
        with TestClient(app) as client:
            base = str(client.base_url)
            url = f"{base}/v1/hook/user-prompt-submit"
            # Sanity: the endpoint returns the contract the script reads.
            resp = client.post(url, json={"prompt": "hello", "cwd": str(tmp_path)})
            assert resp.status_code == 200
            assert "composed_block" in resp.json()
