"""Hermes Agent install module — apply_persistent_config / install_writer.

Writes .hermes/SOUL.md (user scope) or AGENTS.md (repo scope) with a
sentinel-bounded block containing the AgentAlloy skill-context prose.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import yaml

from agentalloy.api.proxy_context import Upstream
from agentalloy.install.sentinel_utils import replace_marked_block
from agentalloy.providers.base import WireRecord, sdd_instructions_markdown

_SENTINEL_BEGIN = "<!-- BEGIN agentalloy install -->"
_SENTINEL_END = "<!-- END agentalloy install -->"


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _inject_sentinel_block(existing: str, block: str) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    Delegates to the shared ``replace_marked_block`` helper which
    validates BEGIN-before-END ordering and duplicate counts.
    """
    return replace_marked_block(existing, block, _SENTINEL_BEGIN, _SENTINEL_END)


def extract_upstream(root: Path) -> Upstream | None:
    """Recover the upstream LLM from the user's global ``~/.hermes/config.yaml``.

    Hermes stores its active endpoint as ``model.base_url`` + ``model.default``
    (its ``provider: custom`` OpenAI-wire path — e.g. a local llama-server). The
    proxy adopts that so ``agentalloy add hermes-agent`` needs no upstream
    re-entry. ``root`` is unused: hermes config is home-scoped, not per-repo.

    Returns ``None`` when the config is absent/malformed or lacks a usable
    ``model.base_url`` / model name.
    """
    config_path = Path.home() / ".hermes" / "config.yaml"
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    model = cast("dict[str, object]", parsed).get("model")
    if not isinstance(model, dict):
        return None
    model_block = cast("dict[str, object]", model)
    base_url = model_block.get("base_url")
    name = model_block.get("default") or model_block.get("model")
    if not isinstance(base_url, str) or not base_url:
        return None
    if not isinstance(name, str) or not name:
        return None
    # Hermes' custom/openai-api providers authenticate via OPENAI_API_KEY; keyless
    # local servers (llama-server) leave it unset, which the proxy tolerates.
    return Upstream(url=base_url.rstrip("/"), model=name, key_env="OPENAI_API_KEY")


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for hermes-agent.

    Writes .hermes/SOUL.md (user scope) or AGENTS.md (repo scope) with
    a sentinel-bounded block containing the AgentAlloy skill-context prose.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    # Determine scope: use .hermes/ directory as indicator for user scope
    hermes_dir = Path.home() / ".hermes"
    target_path = hermes_dir / "SOUL.md" if hermes_dir.exists() else root / "AGENTS.md"

    target_path.parent.mkdir(parents=True, exist_ok=True)

    instruction_content = sdd_instructions_markdown(port)

    original_content = _capture_original(target_path)

    if target_path.exists():
        content = target_path.read_text(encoding="utf-8")
        content = _inject_sentinel_block(content, instruction_content)
    else:
        content = f"{_SENTINEL_BEGIN}\n{instruction_content}\n{_SENTINEL_END}\n"

    target_path.write_text(content, encoding="utf-8")

    return [
        WireRecord(
            path=str(target_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(instruction_content),
            original_content=original_content,
            marker_key="hermes-agent.instructions",
        )
    ]
