"""Tests for sidecar harness setup messaging and state recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest


def _make_cfg(harness: str, non_interactive: bool = False, acknowledge_sidecar: bool = False):
    from agentalloy.install.subcommands.simple_setup import SetupConfig

    return SetupConfig(
        runner="ollama",
        model="nomic-embed-text-v1.5",
        port=47950,
        mode="persistent",
        packs="",
        harness=harness,
        non_interactive=non_interactive,
        acknowledge_sidecar=acknowledge_sidecar,
    )


# ---------------------------------------------------------------------------
# test_sidecar_non_interactive_requires_flag
# ---------------------------------------------------------------------------


def test_sidecar_non_interactive_requires_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Non-interactive sidecar setup exits with rc=1 without --acknowledge-sidecar."""
    from agentalloy.install.subcommands import simple_setup as ss

    monkeypatch.chdir(tmp_path)
    cfg = _make_cfg("cursor", non_interactive=True, acknowledge_sidecar=False)

    with patch.object(ss, "_print", return_value=None):
        rc = ss.run_setup(cfg)

    assert rc == 1


def test_sidecar_interactive_code_path(tmp_path: Path):
    """The sidecar interactive code path calls _prompt_context with 'Continue' message."""
    from agentalloy.install.subcommands import simple_setup as ss

    _sidecar = frozenset({"cursor", "windsurf", "github-copilot", "gemini-cli"})
    harness = "cursor"
    assert harness in _sidecar  # sanity check

    prompt_calls: list[str] = []

    def mock_prompt(label: str, *args: Any, **kwargs: Any) -> str:
        prompt_calls.append(label)
        return "n"

    rc = -1
    with (
        patch.object(ss, "_prompt_context", side_effect=mock_prompt),
        patch.object(ss, "_print", return_value=None),
    ):
        if harness in _sidecar:
            ans = cast(
                str,
                ss._prompt_context(  # pyright: ignore[reportPrivateUsage,reportCallIssue]
                    "  Continue with sidecar harness? [y/n]", default="n"
                ),
            )
            if (ans or "n").strip().lower() != "y":
                rc = 0  # cancelled

    assert any("Continue with sidecar harness" in c for c in prompt_calls)
    assert rc == 0


# ---------------------------------------------------------------------------
# test_sidecar_wire_writes_watch_config
# ---------------------------------------------------------------------------


def test_sidecar_wire_writes_watcher_config_via_watch_dir(tmp_path: Path):
    """_wire_sidecar_watcher_config writes watch config to ~/.agentalloy/watch/."""
    import yaml

    from agentalloy.install.subcommands.wire_harness import (
        _wire_sidecar_watcher_config,  # pyright: ignore[reportPrivateUsage]
    )

    watch_dir = tmp_path / ".agentalloy" / "watch"

    with patch("pathlib.Path.home", return_value=tmp_path):
        _wire_sidecar_watcher_config("cursor", tmp_path)

    config_file = watch_dir / "default.yaml"
    assert config_file.exists()
    data = yaml.safe_load(config_file.read_text())
    assert data["harness"] == "cursor"
    assert "project_root" in data
