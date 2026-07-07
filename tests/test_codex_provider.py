"""Unit tests for the codex provider (repo-local CODEX_HOME, Responses wire).

Modern codex is Responses-API-only (e2e-matrix finding): it ignores
OPENAI_BASE_URL, and custom model_providers require wire_api="responses".
Wiring is a repo-local CODEX_HOME with config.toml pointing at the proxy's
/proj/<token>/v1 base (the Responses SDK appends /responses).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.providers import REGISTRY, Capability, Protocol


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _read_config(root: Path) -> dict[str, object]:
    data = tomllib.loads((root / ".codex" / "config.toml").read_text())
    assert isinstance(data, dict)
    return data


def test_codex_spec_fields() -> None:
    spec = REGISTRY["codex"]
    assert spec.name == "codex"
    assert spec.binary == "codex"
    assert spec.capabilities == (Capability.PROXY,)
    assert spec.protocol == Protocol.OPENAI


def test_env_builder_sets_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = REGISTRY["codex"].env_builder(47950)
    assert env == {"CODEX_HOME": str(tmp_path / ".codex")}


def test_install_writer_writes_responses_provider(tmp_path: Path) -> None:
    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    config = _read_config(tmp_path)
    assert config["model_provider"] == "agentalloy"
    providers = config["model_providers"]
    assert isinstance(providers, dict)
    agentalloy = providers["agentalloy"]
    assert isinstance(agentalloy, dict)
    token = encode_proj_token(tmp_path)
    assert agentalloy["base_url"] == f"http://localhost:6666/proj/{token}/v1"
    assert agentalloy["wire_api"] == "responses"
    assert agentalloy["env_key"] == "OPENAI_API_KEY"

    paths = {r.path for r in records}
    assert str(tmp_path / ".codex" / "config.toml") in paths
    assert str(tmp_path / ".codex" / ".agentalloy-env") in paths
    assert str(tmp_path / ".codex" / ".gitignore") in paths


def test_env_file_exports_codex_home(tmp_path: Path) -> None:
    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    env_text = (tmp_path / ".codex" / ".agentalloy-env").read_text()
    assert 'export CODEX_HOME="$PWD/.codex"' in env_text


def test_gitignore_keeps_codex_state_out_of_git(tmp_path: Path) -> None:
    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    assert (tmp_path / ".codex" / ".gitignore").read_text() == "*\n"


def test_global_config_settings_survive(fake_home: Path, tmp_path: Path) -> None:
    """The user's global ~/.codex/config.toml tuning is carried into the repo copy."""
    global_dir = fake_home / ".codex"
    global_dir.mkdir()
    (global_dir / "config.toml").write_text('model = "gpt-5-codex"\napproval_policy = "never"\n')

    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)

    config = _read_config(tmp_path)
    assert config["model"] == "gpt-5-codex"
    assert config["approval_policy"] == "never"
    assert config["model_provider"] == "agentalloy"


def test_auth_json_never_copied(fake_home: Path, tmp_path: Path) -> None:
    """OAuth secrets stay in ~/.codex — never copied into the repo."""
    global_dir = fake_home / ".codex"
    global_dir.mkdir()
    (global_dir / "auth.json").write_text('{"token": "secret"}')

    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)

    assert not (tmp_path / ".codex" / "auth.json").exists()


def test_rewire_replaces_and_captures_original(tmp_path: Path) -> None:
    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    records = writer(7777, tmp_path, False)

    config = _read_config(tmp_path)
    providers = config["model_providers"]
    assert isinstance(providers, dict)
    agentalloy = providers["agentalloy"]
    assert isinstance(agentalloy, dict)
    assert "localhost:7777" in str(agentalloy["base_url"])
    config_record = next(r for r in records if r.path.endswith("config.toml"))
    assert config_record.action == "replaced_file"
    assert config_record.original_content is not None
    assert "localhost:6666" in config_record.original_content


def test_malformed_global_config_does_not_block_wiring(fake_home: Path, tmp_path: Path) -> None:
    global_dir = fake_home / ".codex"
    global_dir.mkdir()
    (global_dir / "config.toml").write_text("{not toml")

    writer = REGISTRY["codex"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    assert records
    config = _read_config(tmp_path)
    assert config["model_provider"] == "agentalloy"
