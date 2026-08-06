# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Cline install module — apply_persistent_config / install_writer.

Proxy wiring via Cline's real provider store. The previously shipped
repo-local ``.cline/settings.json`` was **inert** — Cline reads provider
config from VS Code globalState and (since CLI 2.0) the user-scoped
``~/.cline/data/settings/providers.json``; the project ``.cline/`` directory
explicitly excludes provider settings. This module writes/merges the
``openai-compatible`` provider entry into ``providers.json`` — the exact
schema ``cline auth -p openai-compatible`` produces (captured from a live
binary by the harness e2e matrix):

    providers["openai-compatible"] = {
        "settings": {provider, apiKey, model, baseUrl},
        "updatedAt": ISO-8601, "tokenSource": "manual"}
    lastUsedProvider = "openai-compatible"

User-scoped (one store per machine), so the base URL targets the proxy's bare
``/v1`` surface — a per-repo ``/proj/<token>`` in a global store would
misattribute every other repo's traffic.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.api.proxy_context import Upstream
from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def providers_json_path(data_dir: Path | None = None) -> Path:
    """Cline's provider store (``--data-dir`` layout: <dir>/settings/providers.json;
    default user scope nests under ``~/.cline/data/``)."""
    if data_dir is not None:
        return data_dir / "settings" / "providers.json"
    return Path.home() / ".cline" / "data" / "settings" / "providers.json"


def _is_proxy_entry(settings: dict[str, Any]) -> bool:
    """True when a provider entry is the one agentalloy itself injected.

    ``render_providers`` always writes the synthetic model name
    ``agentalloy-proxy`` pointing at the local proxy's bare ``/v1`` surface.
    That model name is the canonical marker (also what the proxy's
    ``_resolve_model`` maps back to the real upstream), so it reliably
    identifies *our* entry regardless of which host/port the proxy runs on.
    """
    return settings.get("model") == "agentalloy-proxy"


def extract_upstream(root: Path, data_dir: Path | None = None) -> Upstream | None:
    """Recover the upstream LLM from cline's user-scoped provider store.

    Cline reads ``~/.cline/data/settings/providers.json`` (the store ``cline
    auth`` writes; the repo-local ``.cline/settings.json`` is inert). Each
    provider entry carries ``{settings: {provider, apiKey, model, baseUrl}}``
    plus a top-level ``lastUsedProvider`` naming the active one. ``root`` is
    unused — cline config is home-scoped, not per-repo (reading the repo-local
    store back would adopt the proxy as its own upstream).

    Selection mirrors qwen's guard: agentalloy's own wiring writes an
    ``openai-compatible`` entry pointing at the proxy with the synthetic model
    ``agentalloy-proxy``, which we skip so we never adopt the proxy as its own
    upstream. Prefer ``lastUsedProvider`` when it isn't the proxy; otherwise
    adopt the sole remaining non-proxy provider; return ``None`` when the store
    is absent, malformed, or the non-proxy set is empty/ambiguous (the user
    then passes ``--upstream-url``).

    ``key_env`` is always ``None``: cline stores the upstream API key as a
    *literal* in ``settings.apiKey``, which can't be mapped to the env-var
    *name* ``Upstream.key_env`` requires the proxy to resolve at request time.
    So adoption is auth-transparent — correct for keyless local upstreams
    (llama-server / ollama), the primary ``add cline`` case; the global
    ``UPSTREAM`` / ``--key-env`` path still covers keyed cloud providers.
    """
    _ = root
    path = providers_json_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    providers = parsed.get("providers")
    last_used = parsed.get("lastUsedProvider")
    if not isinstance(providers, dict) or not providers:
        return None

    def _settings_of(key: object) -> dict[str, Any] | None:
        entry = cast(dict[str, Any], providers.get(key))  # type: ignore[arg-type]
        settings = entry.get("settings")
        return settings if isinstance(settings, dict) else None

    def _to_upstream(settings: dict[str, Any]) -> Upstream | None:
        url = settings.get("baseUrl")
        model = settings.get("model")
        if not (isinstance(url, str) and url) or not (isinstance(model, str) and model):
            return None
        return Upstream(url=url.rstrip("/"), model=model, key_env=None)

    # Prefer the active provider unless it is our own proxy entry.
    if isinstance(last_used, str):
        settings = _settings_of(last_used)
        if settings is not None and not _is_proxy_entry(settings):
            return _to_upstream(settings)

    # Otherwise fall back to the sole remaining non-proxy provider.
    real = [
        settings
        for settings in (_settings_of(k) for k in cast(dict[object, dict[str, Any]], providers))
        if settings is not None and not _is_proxy_entry(settings)
    ]
    if len(real) == 1:
        return _to_upstream(real[0])
    return None


def render_providers(port: int, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge the agentalloy openai-compatible provider into a providers store."""
    store: dict[str, Any] = dict(existing or {})
    store.setdefault("version", 1)
    providers = store.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers = cast("dict[str, Any]", dict(providers))
    providers["openai-compatible"] = {
        "settings": {
            "provider": "openai-compatible",
            "apiKey": "agentalloy",
            "model": "agentalloy-proxy",
            "baseUrl": f"http://localhost:{port}/v1",
        },
        # Match `cline auth`'s JS-style timestamp exactly (milliseconds + Z):
        # a strict parse of this field silently invalidates the whole store.
        "updatedAt": datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "tokenSource": "manual",
    }
    store["providers"] = providers
    store["lastUsedProvider"] = "openai-compatible"
    return store


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for cline by merging into the user-scoped providers.json.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (unused — cline's provider store is user-scoped).
        force: Unused — the provider entry is idempotently replaced.

    Returns:
        List of WireRecord describing files written.
    """
    _ = root, force
    path = providers_json_path()

    original_content = path.read_text(encoding="utf-8") if path.exists() else None
    existing: dict[str, Any] = {}
    if original_content is not None:
        try:
            parsed: Any = json.loads(original_content)
            if isinstance(parsed, dict):
                existing = cast("dict[str, Any]", parsed)
        except json.JSONDecodeError as exc:
            print(f"ERROR: {path} is not valid JSON", file=sys.stderr)
            print("FIX:   Fix the JSON syntax or remove the file.", file=sys.stderr)
            raise SystemExit(1) from exc

    store = render_providers(port, existing)
    content = json.dumps(store, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    print(
        "[AgentAlloy] cline wired via ~/.cline/data/settings/providers.json "
        "(openai-compatible provider → proxy /v1; becomes the last-used provider "
        "for the VS Code extension and the cline CLI). User-scoped: cline traffic "
        "routes through the proxy's global /v1 surface.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(content),
            original_content=original_content,
            marker_key="cline.providers.openai-compatible",
        )
    ]
