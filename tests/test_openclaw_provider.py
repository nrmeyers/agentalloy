"""Unit tests for the openclaw provider (custom model provider in openclaw.json).

The old ~/.openclaw/plugins.json "proxy plugin" entry was never OpenClaw
schema, and OpenClaw ignores OPENAI_BASE_URL (e2e-matrix finding). The real
vector is models.providers.agentalloy in ~/.openclaw/openclaw.json plus
agents.defaults.model.primary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.providers import REGISTRY, Capability, Protocol


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _read_config(home: Path) -> dict[str, object]:
    data = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert isinstance(data, dict)
    return data


def test_openclaw_spec_fields() -> None:
    spec = REGISTRY["openclaw"]
    assert spec.name == "openclaw"
    assert spec.binary == "openclaw"
    assert spec.capabilities == (Capability.PROXY,)
    assert spec.protocol == Protocol.OPENAI


def test_env_builder_is_empty() -> None:
    """OpenClaw ignores OPENAI_BASE_URL — env wiring would be a silent no-op."""
    assert REGISTRY["openclaw"].env_builder(47950) == {}


def test_install_writer_merges_custom_provider(fake_home: Path, tmp_path: Path) -> None:
    writer = REGISTRY["openclaw"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    config = _read_config(fake_home)
    models = config["models"]
    assert isinstance(models, dict)
    providers = models["providers"]
    assert isinstance(providers, dict)
    provider = providers["agentalloy"]
    assert isinstance(provider, dict)
    assert provider["baseUrl"] == "http://localhost:6666/v1"
    assert provider["api"] == "openai-completions"
    model_ids = [m["id"] for m in provider["models"]]
    assert model_ids == ["agentalloy-proxy"]

    agents = config["agents"]
    assert isinstance(agents, dict)
    assert agents["defaults"]["model"]["primary"] == "agentalloy/agentalloy-proxy"

    assert [r.path for r in records] == [str(fake_home / ".openclaw" / "openclaw.json")]
    assert records[0].action == "wrote_new_file"


def test_merges_over_existing_config(fake_home: Path, tmp_path: Path) -> None:
    openclaw_dir = fake_home / ".openclaw"
    openclaw_dir.mkdir()
    (openclaw_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "gateway": {"port": 18789},
                "models": {"providers": {"ollama": {"baseUrl": "http://localhost:11434"}}},
            }
        )
    )

    writer = REGISTRY["openclaw"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    config = _read_config(fake_home)
    assert config["gateway"] == {"port": 18789}
    providers = config["models"]["providers"]
    assert isinstance(providers, dict)
    assert "ollama" in providers and "agentalloy" in providers
    assert records[0].action == "injected_block"
    assert records[0].original_content is not None
    assert "ollama" in records[0].original_content


def test_rewire_is_idempotent(fake_home: Path, tmp_path: Path) -> None:
    writer = REGISTRY["openclaw"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    writer(7777, tmp_path, False)

    config = _read_config(fake_home)
    provider = config["models"]["providers"]["agentalloy"]
    assert "localhost:7777" in provider["baseUrl"]


def test_invalid_existing_json_is_a_hard_error(fake_home: Path, tmp_path: Path) -> None:
    openclaw_dir = fake_home / ".openclaw"
    openclaw_dir.mkdir()
    (openclaw_dir / "openclaw.json").write_text("{not json")

    writer = REGISTRY["openclaw"].install_writer
    assert writer is not None
    with pytest.raises(SystemExit):
        writer(6666, tmp_path, False)


def test_never_writes_dead_plugins_json(fake_home: Path, tmp_path: Path) -> None:
    """The pre-rewrite plugins.json carrier must stay dead."""
    writer = REGISTRY["openclaw"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    assert not (fake_home / ".openclaw" / "plugins.json").exists()
