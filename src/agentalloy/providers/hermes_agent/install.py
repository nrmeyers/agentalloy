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

    Hermes' active endpoint takes two shapes:

    - inline: ``model.base_url`` + ``model.default`` (older configs)
    - provider reference (the shipping format): ``model.provider:
      custom[:<key>]`` pointing at a ``custom_providers`` entry, which carries
      ``base_url`` + ``model``. Hermes matches ``<key>`` against the entry's
      ``provider_key`` or ``name`` (case-insensitive).

    The proxy adopts whichever resolves so ``agentalloy add hermes-agent``
    needs no upstream re-entry. ``root`` is unused: hermes config is
    home-scoped, not per-repo (the repo-scoped ``.hermes/config.yaml`` is what
    *we* write — reading it back would adopt the proxy as its own upstream).

    Returns ``None`` when the config is absent/malformed or no endpoint
    resolves.
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
    config = cast("dict[str, object]", parsed)
    model = config.get("model")
    if not isinstance(model, dict):
        return None
    model_block = cast("dict[str, object]", model)
    default_name = model_block.get("default") or model_block.get("model")

    # Inline form: model.base_url + a model name.
    base_url = model_block.get("base_url")
    if isinstance(base_url, str) and base_url and isinstance(default_name, str) and default_name:
        return Upstream(url=base_url.rstrip("/"), model=default_name, key_env="OPENAI_API_KEY")

    # Provider-reference form: model.provider = "custom" or "custom:<key>".
    provider_ref = model_block.get("provider")
    if not isinstance(provider_ref, str):
        return None
    provider_norm = provider_ref.strip().lower()
    if provider_norm != "custom" and not provider_norm.startswith("custom:"):
        return None
    key = provider_norm.split(":", 1)[1].strip() if ":" in provider_norm else ""

    providers = config.get("custom_providers")
    if not isinstance(providers, list):
        return None
    entries = [
        cast("dict[str, object]", e)
        for e in cast("list[object]", providers)
        if isinstance(e, dict) and isinstance(e.get("base_url"), str) and e.get("base_url")
    ]
    entry: dict[str, object] | None = None
    if key:
        for e in entries:
            names = {
                str(e.get("provider_key") or "").strip().lower(),
                str(e.get("name") or "").strip().lower(),
            }
            if key in names:
                entry = e
                break
    if entry is None and len(entries) == 1:
        # Best-effort: a dangling/blank key with exactly one endpoint isn't
        # ambiguous — adopt it rather than warn the user into re-entry.
        entry = entries[0]
    if entry is None:
        return None

    url = cast("str", entry["base_url"]).rstrip("/")
    name = default_name if isinstance(default_name, str) and default_name else entry.get("model")
    if not isinstance(name, str) or not name:
        return None
    # Hermes' custom/openai-api providers authenticate via OPENAI_API_KEY; keyless
    # local servers (llama-server) leave it unset, which the proxy tolerates.
    return Upstream(url=url, model=name, key_env="OPENAI_API_KEY")


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
