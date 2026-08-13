"""Unit tests for the github-copilot (VS Code) provider — dual-carrier wiring.

BYOK proxy carrier: a customendpoint provider group in the VS Code
user-profile chatLanguageModels.json (BYOK GA Apr 2026), plus the ambient
.github/copilot-instructions.md sidecar block. NOT machine-verified end to
end (no headless VS Code) — the manual smoke checklist gates the claim; these
tests pin the carriers we write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentalloy.providers import REGISTRY, Capability, Protocol
from agentalloy.providers.github_copilot.install import (
    PROVIDER_NAME,
    apply_byok_config,
    render_provider_group,
)


@pytest.fixture
def fake_vscode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake linux home with a VS Code user profile dir."""
    home = tmp_path / "home"
    user_dir = home / ".config" / "Code" / "User"
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("sys.platform", "linux")
    return user_dir


def test_spec_is_dual_carrier() -> None:
    spec = REGISTRY["github-copilot"]
    assert spec.capabilities == (Capability.PROXY, Capability.MARKDOWN_ONLY)
    assert spec.protocol == Protocol.OPENAI


def test_provider_group_shape() -> None:
    group = render_provider_group(47950)
    assert group["vendor"] == "customendpoint"
    assert group["apiType"] == "chat-completions"
    model = group["models"][0]
    # Full endpoint path (the BYOK schema's url is not a base URL).
    assert model["url"] == "http://localhost:47950/v1/chat/completions"
    # Required for the model to appear in agent mode.
    assert model["toolCalling"] is True


def test_byok_writes_provider_group(fake_vscode: Path) -> None:
    records = apply_byok_config(6666)

    path = fake_vscode / "chatLanguageModels.json"
    assert [r.path for r in records] == [str(path)]
    groups = json.loads(path.read_text())
    assert [g["name"] for g in groups] == [PROVIDER_NAME]


def test_byok_merges_and_replaces_own_group(fake_vscode: Path) -> None:
    (fake_vscode / "chatLanguageModels.json").write_text(
        json.dumps(
            [
                {"name": "Ollama", "vendor": "customendpoint", "models": []},
                {"name": PROVIDER_NAME, "vendor": "customendpoint", "models": []},
            ]
        )
    )

    records = apply_byok_config(7777)

    groups = json.loads((fake_vscode / "chatLanguageModels.json").read_text())
    names = [g["name"] for g in groups]
    assert names == ["Ollama", PROVIDER_NAME]
    agentalloy = groups[1]
    assert "localhost:7777" in agentalloy["models"][0]["url"]
    assert records[0].original_content is not None


def test_byok_skips_without_vscode_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No VS Code on the machine → BYOK skipped with guidance, no crash."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("sys.platform", "linux")

    assert apply_byok_config(6666) == []
    assert "skipped the BYOK proxy carrier" in capsys.readouterr().err


def test_install_writer_writes_both_carriers(
    fake_vscode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    writer = REGISTRY["github-copilot"].install_writer
    assert writer is not None
    records = writer(6666, repo, False)

    paths = {r.path for r in records}
    assert str(repo / ".github" / "copilot-instructions.md") in paths
    assert str(fake_vscode / "chatLanguageModels.json") in paths
    # The instructions block carries the watcher's markers, and the record
    # keeps the sentinel overrides through the WireRecord round-trip.
    instructions = (repo / ".github" / "copilot-instructions.md").read_text()
    assert "AGENTALLOY-CONTEXT" in instructions
    md_record = next(r for r in records if r.path.endswith("copilot-instructions.md"))
    assert md_record.sentinel_begin == "<!-- BEGIN AGENTALLOY-CONTEXT -->"


def test_invalid_existing_byok_json_is_a_hard_error(fake_vscode: Path) -> None:
    (fake_vscode / "chatLanguageModels.json").write_text("{not json")
    with pytest.raises(SystemExit):
        apply_byok_config(6666)
