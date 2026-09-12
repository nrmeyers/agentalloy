"""CLI tests — the upstream command's offline (proxy-down) paths."""

import socket
import sys
from pathlib import Path

import pytest

from agentalloy import cli


def _free_port() -> int:
    """A port that is definitely closed (bind, read, release)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def upstream_cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """env.sh next to a tmp state.duck + a proxy port with nothing on it."""
    env_sh = tmp_path / "env.sh"
    env_sh.write_text(
        "export AGENTALLOY_UPSTREAM_URL=http://old:8000\n"
        "export AGENTALLOY_MODEL=old-model\n"
    )
    monkeypatch.setenv("AGENTALLOY_PROXY_PORT", str(_free_port()))
    monkeypatch.setenv("AGENTALLOY_STATE_DUCK", str(tmp_path / "state.duck"))
    return env_sh


def test_upstream_set_offline_persists_env_sh(
    upstream_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Proxy down → `upstream set` writes env.sh directly, rc 0."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentalloy",
            "upstream",
            "set",
            "http://new:8000",
            "--model",
            "new-model",
            "--key",
            "sk-new",
        ],
    )
    rc = cli.main()
    assert rc == 0
    text = upstream_cli_env.read_text()
    assert "export AGENTALLOY_UPSTREAM_URL=http://new:8000" in text
    assert "export AGENTALLOY_MODEL=new-model" in text
    assert "export AGENTALLOY_UPSTREAM_KEY=sk-new" in text
    assert "Proxy not running" in capsys.readouterr().out


def test_upstream_set_offline_without_key(
    upstream_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting --key must not fabricate a key line in env.sh."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["agentalloy", "upstream", "set", "http://new:8000", "--model", "new-model"],
    )
    assert cli.main() == 0
    assert "AGENTALLOY_UPSTREAM_KEY" not in upstream_cli_env.read_text()
    assert "Proxy not running" in capsys.readouterr().out


def test_upstream_get_offline_reads_env_sh(
    upstream_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Proxy down → `upstream get` falls back to env.sh, rc 0."""
    monkeypatch.setattr(sys, "argv", ["agentalloy", "upstream", "get"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "proxy not running" in out
    assert "http://old:8000" in out
    assert "old-model" in out


def test_upstream_set_requires_url_and_model(
    upstream_cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`set` with a missing model refuses before touching anything."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["agentalloy", "upstream", "set", "http://new:8000"],
    )
    assert cli.main() == 1
    assert "usage" in capsys.readouterr().out
    assert "new:8000" not in upstream_cli_env.read_text()
