"""Qwen Code install module — apply_persistent_config / install_writer.

Proxy wiring: repo-local ``.qwen/settings.json`` (QWEN_HOME isolation) with
the ``model`` block pointed at the proxy's ``/proj/<token>`` endpoint, plus
activation carriers and a ``.qwen/.agentalloy-env`` file.

Delegates to ``wire_harness._wire_proxy_qwen_code`` so the provider registry
and ``agentalloy wire`` share one code path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from agentalloy.api.proxy_context import Upstream
from agentalloy.providers.base import WireRecord


def extract_upstream(root: Path) -> Upstream | None:
    """Recover the upstream LLM from the user's global ``~/.qwen/settings.json``.

    Qwen Code stores model providers under ``modelProviders.openai[]``, each
    with a ``baseUrl``. The proxy writes its own entry with a ``/proj/<token>``
    path, so we skip any provider whose baseUrl contains ``/proj/`` and pick
    the first remaining one as the user's original upstream.

    The model name is read from the top-level ``model`` field (``model.name``
    or ``model.default``), falling back to the provider's ``name`` display
    label when nothing specific is set.

    ``root`` is unused: qwen-code config is home-scoped, not per-repo (the
    repo-local ``.qwen/settings.json`` is what *we* write — reading it back
    would adopt the proxy as its own upstream).

    Returns ``None`` when the config is absent, malformed, or no non-proxy
    endpoint resolves.
    """
    _ = root
    config_path = Path.home() / ".qwen" / "settings.json"
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    # Find the first non-proxy openai provider baseUrl.
    settings_data = cast("dict[str, Any]", parsed)
    providers = settings_data.get("modelProviders")
    if not isinstance(providers, dict):
        return None
    openai_providers = providers.get("openai")
    if not isinstance(openai_providers, list):
        return None

    upstream_url: str | None = None
    for entry in openai_providers:
        if not isinstance(entry, dict):
            continue
        base = entry.get("baseUrl")
        if not isinstance(base, str):
            continue
        # Skip the proxy entry (its baseUrl contains /proj/<token>).
        if "/proj/" in base:
            continue
        upstream_url = base.rstrip("/")
        break

    if not upstream_url:
        return None

    # Resolve the model name.
    model_block = settings_data.get("model")
    model_name: str | None = None
    if isinstance(model_block, dict):
        model_name = cast("str | None", model_block.get("name")) or cast(
            "str | None", model_block.get("default")
        )
    # Fallback: use the provider's display name.
    if not model_name:
        for entry in openai_providers:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    model_name = name
                    break

    if not model_name:
        return None

    return Upstream(url=upstream_url, model=model_name, key_env="OPENAI_API_KEY")


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent proxy wiring for qwen-code.

    Delegates to the live ``_wire_proxy_qwen_code`` implementation (repo-local
    ``.qwen/settings.json`` + ``QWEN_HOME`` activation carriers) so this
    provider module and ``agentalloy wire`` share one code path.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — wiring replaces only files it owns.

    Returns:
        List of WireRecord describing files written.
    """
    # Lazy import: wire_harness imports the provider registry at module load,
    # so a top-level import here would be circular.
    from agentalloy.install.subcommands.wire_harness import (  # pyright: ignore[reportPrivateUsage]
        _wire_proxy_qwen_code,
    )

    _ = force
    records = _wire_proxy_qwen_code(port, root, scope="repo")
    return [WireRecord.from_dict(r) for r in records]
