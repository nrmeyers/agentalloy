"""Web UI per-repo upstream endpoints — GET/PUT ``/api/upstream``.

Unlike :mod:`config_api` (which edits the *global* user-scoped ``.env`` and
needs a soft reload to reach the process env), this edits the *per-repo*
``.agentalloy/upstream`` file — the YAML map the proxy re-reads on every
request. A write is therefore live on the next proxied request; no reload
endpoint exists here.

The surface edits only the repo's *active chat* entry — the single entry the
proxy forwards chat to (the first non-passthrough-harness entry, or the top
level of a legacy flat file). Passthrough harnesses (``claude-code``/``codex``)
own their own upstream and are never touched.

Mutating endpoints require the ``X-AgentAlloy-CSRF: 1`` header, same guard as
``config_api``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from agentalloy.api.proxy_context import (
    UPSTREAM_FILE,
    read_upstream,
    resolve_chat_upstream_key,
)
from agentalloy.api.state_router import resolve_repo_root

logger = logging.getLogger(__name__)

router = APIRouter()


class UpstreamUpdate(BaseModel):
    url: str
    model: str
    key_env: str | None = None


def _require_csrf(header_value: str | None) -> None:
    if header_value != "1":
        raise HTTPException(
            status_code=403,
            detail="Missing X-AgentAlloy-CSRF: 1 header (browser cross-origin guard).",
        )


def _upstream_path(root: Any) -> Any:
    return root / UPSTREAM_FILE


def _load_map(path: Any) -> dict[str, Any]:
    """Load the per-harness upstream map, migrating a legacy flat file.

    Mirrors ``install.subcommands.add._load_upstream_map``: a flat file
    (``url`` at the top level) is folded under the empty top level so the
    writer can update it in place. A malformed file yields ``{}`` — the
    caller treats that as "nothing to edit" (400), never a crash.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                data = dict(raw)
        except yaml.YAMLError:
            data = {}
    return data


@router.get("/api/upstream", summary="Active chat upstream for a repo")
async def get_upstream(root: Any = Depends(resolve_repo_root)) -> dict[str, Any]:
    """Return the repo's active chat upstream, or ``exists=false``.

    A malformed file is reported as ``exists=true`` with a ``detail`` string
    (HTTP 200) — the UI renders an error state rather than the service 500ing.
    """
    result = read_upstream(root)
    if result.kind == "absent":
        return {
            "repo_root": str(root),
            "exists": False,
            "harness": None,
            "url": None,
            "model": None,
            "key_env": None,
            "detail": None,
        }
    if result.kind == "error":
        return {
            "repo_root": str(root),
            "exists": True,
            "harness": None,
            "url": None,
            "model": None,
            "key_env": None,
            "detail": result.detail,
        }
    key = resolve_chat_upstream_key(root)
    up = result.upstream
    return {
        "repo_root": str(root),
        "exists": True,
        "harness": key,
        "url": up.url if up else None,
        "model": up.model if up else None,
        "key_env": up.key_env if up else None,
        "detail": None,
    }


@router.put("/api/upstream", response_model=dict, summary="Edit a repo's active chat upstream")
async def put_upstream(
    body: UpstreamUpdate,
    x_agentalloy_csrf: Annotated[str | None, Header()] = None,
    root: Any = Depends(resolve_repo_root),
) -> dict[str, Any]:
    """Merge ``{url, model, key_env}`` into the repo's active chat entry.

    Edit-only: when the repo has no active chat entry (absent file or a
    passthrough-only map) this is a 400 — the endpoint never invents a harness
    key. Other harness entries are preserved byte-for-byte; the write is
    atomic (temp file + rename) so a crash never leaves a torn file.
    """
    _require_csrf(x_agentalloy_csrf)

    url = (body.url or "").strip()
    model = (body.model or "").strip()
    if not url or not model:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_field", "detail": "url and model must be non-empty"},
        )
    key_env = (body.key_env or "").strip() or None

    path = _upstream_path(root)
    key = resolve_chat_upstream_key(root)
    if key is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_active_upstream",
                "detail": "This repo has no active chat upstream to edit.",
            },
        )

    data = _load_map(path)
    entry: dict[str, str] = {"url": url.rstrip("/"), "model": model}
    if key_env:
        entry["key_env"] = key_env
    if key == "":
        # Legacy flat file: the top level is the chat scope.
        data.update(entry)
    else:
        data[key] = entry

    from agentalloy.install import state as install_state

    install_state._atomic_write(path, yaml.safe_dump(data, sort_keys=False))
    return {"status": "ok", "repo_root": str(root), "harness": key}
