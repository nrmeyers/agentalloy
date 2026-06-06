"""Tests for the Ollama SSH key auto-copy workaround (Issue #1)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentalloy.install.subcommands.pull_models import (
    _ensure_ollama_ssh_key,
)


def _write_key(tmp_path: Path, path: str, content: str = "fake-ssh-key-data") -> Path:
    """Write a key file with the same permissions as a real SSH key (0600)."""
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    os.chmod(str(p), 0o600)
    return p


# ---------------------------------------------------------------------------
# _ensure_ollama_ssh_key
# ---------------------------------------------------------------------------


def test_no_source_key_skips_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ~/.ssh/id_ed25519 does NOT exist, the function returns without error."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # No source key — should not raise, should not create anything
    result = _ensure_ollama_ssh_key()
    assert result is False  # no action taken
    assert not (home / ".ollama").exists()


def test_target_already_exists_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ~/.ollama/id_ed25519 already exists, skip the copy."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Pre-create both source and target directories
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ollama").mkdir(parents=True, exist_ok=True)

    # Pre-create the target
    target = home / ".ollama" / "id_ed25519"
    target.write_text("existing-key", encoding="utf-8")
    os.chmod(str(target), 0o600)

    # Also create the source
    _write_key(home, ".ssh/id_ed25519")

    result = _ensure_ollama_ssh_key()
    assert result is False  # no action taken (already present)
    assert target.read_text() == "existing-key"  # unchanged


def test_copy_creates_dir_and_sets_perms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When source exists and target does not, copy with correct permissions."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Create source
    _write_key(home, ".ssh/id_ed25519", content="source-key-content")

    result = _ensure_ollama_ssh_key()
    assert result is True  # action taken

    target = home / ".ollama" / "id_ed25519"
    assert target.exists()
    assert target.read_text() == "source-key-content"
    mode = stat.S_IMODE(os.stat(str(target)).st_mode)
    assert mode == 0o600


def test_copy_preserves_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The copied file content must match the source exactly."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    original = "[REDACTED PRIVATE KEY]"
    _write_key(home, ".ssh/id_ed25519", content=original)

    _ensure_ollama_ssh_key()

    target = home / ".ollama" / "id_ed25519"
    assert target.read_text() == original
