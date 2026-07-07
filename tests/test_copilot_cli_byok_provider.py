"""Unit tests for the copilot-cli provider (standalone Copilot CLI, BYOK proxy wiring).

Covers:
  - HarnessSpec creation and registration (PROXY, distinct from the
    markdown-only ``github-copilot`` IDE harness)
  - env_builder returns COPILOT_PROVIDER_* vars with the per-repo token
  - install_writer writes .copilot/.agentalloy-env with the BYOK vars
  - Idempotency (re-running replaces the file, records replaced_file)
  - Wiring through wire_compat routes to the same carrier
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.providers import REGISTRY, Capability, Protocol


def test_copilot_cli_registered() -> None:
    assert "copilot-cli" in REGISTRY


def test_copilot_cli_spec_fields() -> None:
    spec = REGISTRY["copilot-cli"]
    assert spec.name == "copilot-cli"
    assert spec.binary == "copilot"
    assert spec.capabilities == (Capability.PROXY,)
    assert spec.protocol == Protocol.OPENAI


def test_github_copilot_ide_harness_is_dual_carrier() -> None:
    """The IDE surface is PROXY (BYOK custom endpoint) + MARKDOWN_ONLY (ambient
    instructions) — distinct from this standalone-CLI harness."""
    spec = REGISTRY["github-copilot"]
    assert spec.capabilities == (Capability.PROXY, Capability.MARKDOWN_ONLY)


def test_env_builder_returns_byok_vars() -> None:
    env = REGISTRY["copilot-cli"].env_builder(47950)
    assert env["COPILOT_PROVIDER_TYPE"] == "openai"
    assert env["COPILOT_PROVIDER_API_KEY"] == "agentalloy"
    assert env["COPILOT_MODEL"] == "agentalloy-proxy"
    base = env["COPILOT_PROVIDER_BASE_URL"]
    assert base.startswith("http://localhost:47950/proj/")
    assert base.endswith("/v1")


def test_install_writer_writes_env_file(tmp_path: Path) -> None:
    writer = REGISTRY["copilot-cli"].install_writer
    assert writer is not None
    records = writer(6666, tmp_path, False)

    env_path = tmp_path / ".copilot" / ".agentalloy-env"
    assert [r.path for r in records] == [str(env_path)]
    assert records[0].action == "wrote_new_file"
    assert records[0].marker_key == "copilot-cli.env"

    content = env_path.read_text()
    token = encode_proj_token(tmp_path)
    assert f'export COPILOT_PROVIDER_BASE_URL="http://localhost:6666/proj/{token}/v1"' in content
    assert 'export COPILOT_PROVIDER_TYPE="openai"' in content
    assert 'export COPILOT_PROVIDER_API_KEY="agentalloy"' in content
    assert 'export COPILOT_MODEL="agentalloy-proxy"' in content


def test_install_writer_idempotent_replace(tmp_path: Path) -> None:
    writer = REGISTRY["copilot-cli"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    records = writer(7777, tmp_path, False)

    assert records[0].action == "replaced_file"
    content = (tmp_path / ".copilot" / ".agentalloy-env").read_text()
    assert "localhost:7777" in content
    assert "localhost:6666" not in content
    # Prior content captured for uninstall's record walk.
    assert records[0].original_content is not None
    assert "localhost:6666" in records[0].original_content


def test_wire_compat_routes_to_env_carrier(tmp_path: Path) -> None:
    """`agentalloy wire --harness copilot-cli` produces the BYOK env carrier."""
    from tests._wire_compat import wire_compat

    result = wire_compat("copilot-cli", port=6666, root=tmp_path)

    assert result["integration_vector"] == "proxy"
    assert (tmp_path / ".copilot" / ".agentalloy-env").exists()


def test_env_builder_and_install_writer_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrap-time env and the persistent carrier must carry identical vars."""
    monkeypatch.chdir(tmp_path)
    env = REGISTRY["copilot-cli"].env_builder(6666)

    writer = REGISTRY["copilot-cli"].install_writer
    assert writer is not None
    writer(6666, tmp_path, False)
    content = (tmp_path / ".copilot" / ".agentalloy-env").read_text()

    for key, value in env.items():
        assert f'export {key}="{value}"' in content
