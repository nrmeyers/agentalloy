"""Tests for Continue.dev proxy wiring via the provider registry.

Guards against the provider ``install_writer`` regressing to a no-op stub
(it once returned ``[]`` while the live wiring lived only in wire_harness),
which would silently break Continue if wiring ever routes through the
registry path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.providers import REGISTRY


@pytest.mark.parametrize("harness", ["continue-closed", "continue-local"])
def test_registry_install_writer_writes_modern_agent(tmp_path: Path, harness: str) -> None:
    """The modern carrier: repo .continue/agents/agentalloy.yaml with the
    per-repo tokenized base (verified live via `cn --config`)."""
    from agentalloy.api.proxy_context import encode_proj_token

    writer = REGISTRY[harness].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    agent_path = tmp_path / ".continue" / "agents" / "agentalloy.yaml"
    assert str(agent_path) in {r.path for r in records}
    content = agent_path.read_text()
    token = encode_proj_token(tmp_path)
    assert f"apiBase: http://localhost:6666/proj/{token}/v1" in content
    assert "provider: openai" in content
    assert "model: agentalloy-proxy" in content


@pytest.mark.parametrize("harness", ["continue-closed", "continue-local"])
def test_registry_install_writer_writes_continuerc(tmp_path: Path, harness: str) -> None:
    writer = REGISTRY[harness].install_writer
    assert writer is not None

    records = writer(6666, tmp_path, False)

    assert records, f"{harness} install_writer must not be a no-op stub"
    config_path = tmp_path / ".continuerc.json"
    assert str(config_path) in {r.path for r in records}

    config = json.loads(config_path.read_text())
    proxy_models = [m for m in config["models"] if m.get("agentalloy_proxy") is True]
    assert len(proxy_models) == 1
    assert proxy_models[0]["apiBase"] == "http://localhost:6666/v1"
    assert proxy_models[0]["provider"] == "openai"

    marker = config["_agentalloy_install_marker"]
    variant = "closed" if harness == "continue-closed" else "local"
    assert marker["variant"] == f"proxy-{variant}"
    assert "models.agentalloy-proxy" in marker["added_paths"]


@pytest.mark.parametrize("harness", ["continue-closed", "continue-local"])
def test_registry_install_writer_matches_live_wiring(tmp_path: Path, harness: str) -> None:
    """The registry path and the live wire_harness path produce identical wiring."""
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_continue,  # pyright: ignore[reportPrivateUsage]
    )

    registry_root = tmp_path / "registry"
    live_root = tmp_path / "live"
    registry_root.mkdir()
    live_root.mkdir()

    writer = REGISTRY[harness].install_writer
    assert writer is not None
    writer(6666, registry_root, False)
    _wire_proxy_continue(harness, 6666, live_root)

    registry_config = (registry_root / ".continuerc.json").read_text()
    live_config = (live_root / ".continuerc.json").read_text()
    assert registry_config == live_config
