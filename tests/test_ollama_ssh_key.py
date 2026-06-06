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


# ---------------------------------------------------------------------------
# SSH key missing warning (new)
# ---------------------------------------------------------------------------


def test_no_source_key_prints_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """When no source key exists, the function prints a warning to stdout."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # No source key — should print a warning
    _ensure_ollama_ssh_key()

    captured = capsys.readouterr()
    assert "Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519" in captured.out


def test_warning_includes_fix_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The warning message includes the exact fix command."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    _ensure_ollama_ssh_key()

    captured = capsys.readouterr()
    assert "cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519" in captured.out


def test_no_source_key_return_value_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return value is still False when no source key exists."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    result = _ensure_ollama_ssh_key()
    assert result is False


def test_no_source_key_no_dir_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ~/.ollama/ directory is created when no source key exists."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    _ensure_ollama_ssh_key()

    assert not (home / ".ollama").exists()


# ---------------------------------------------------------------------------
# SSH key copied notification (new)
# ---------------------------------------------------------------------------


def test_copy_prints_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """When source exists and target does not, print() is called with the copy confirmation message."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Create source key
    _write_key(home, ".ssh/id_ed25519", content="source-key-content")

    _ensure_ollama_ssh_key()

    captured = capsys.readouterr()
    assert "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." in captured.out


def test_copy_prints_to_stdout_not_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The notification goes to stdout, not stderr."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Create source key
    _write_key(home, ".ssh/id_ed25519", content="source-key-content")

    _ensure_ollama_ssh_key()

    captured = capsys.readouterr()
    assert "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." in captured.out
    assert "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." not in captured.err


def test_copy_return_value_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return value is still True when key was copied."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Create source key
    _write_key(home, ".ssh/id_ed25519", content="source-key-content")

    result = _ensure_ollama_ssh_key()
    assert result is True


def test_no_print_when_key_already_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """When target already exists, no print is called."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # Pre-create both source and target
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ollama").mkdir(parents=True, exist_ok=True)

    target = home / ".ollama" / "id_ed25519"
    target.write_text("existing-key", encoding="utf-8")
    os.chmod(str(target), 0o600)

    _write_key(home, ".ssh/id_ed25519")

    _ensure_ollama_ssh_key()

    captured = capsys.readouterr()
    assert "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." not in captured.out
