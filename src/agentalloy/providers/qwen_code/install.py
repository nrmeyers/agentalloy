# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
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

    The top-level ``model`` block carries the active provider — both ``name``
    and ``baseUrl`` — so we read it directly. The proxy writes its own entry
    with a ``/proj/<token>`` path, so we skip if the baseUrl contains ``/proj/``
    (avoiding adopting the proxy as its own upstream).

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

    settings_data = cast("dict[str, Any]", parsed)
    model_block = settings_data.get("model")
    if not isinstance(model_block, dict):
        return None

    # Read the active model name.
    model_name = cast("str | None", model_block.get("name")) or cast(
        "str | None",
        model_block.get("default"),
    )
    if not model_name:
        return None

    # Read the baseUrl directly from the model block.
    base_url = cast("str | None", model_block.get("baseUrl"))
    if not base_url:
        return None

    # Skip the proxy entry (its baseUrl contains /proj/<token>).
    if "/proj/" in base_url:
        return None

    return Upstream(url=base_url.rstrip("/"), model=model_name, key_env="OPENAI_API_KEY")


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
    from agentalloy.install.subcommands.wire_harness import (
        _wire_proxy_qwen_code,
    )

    _ = force
    records = _wire_proxy_qwen_code(port, root, scope="repo")
    records = [WireRecord.from_dict(r) for r in records]

    return records
