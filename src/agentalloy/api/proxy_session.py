"""Per-request session-key resolution for the proxy.

The signal layer orients an agent once per *(phase, session)* rather than once
per *(phase, repo)* — otherwise a new session that joins an already-announced
phase never sees the workflow's operating prose. This module resolves the
session key two ways, in priority order:

1. **Explicit session header** — harnesses that send one (Claude Code sends
   ``x-claude-code-session-id``). The set of header names is registry-driven:
   each :class:`~agentalloy.providers.base.HarnessSpec` may declare a
   ``session_header``, and we probe the union, so adding a harness's header is a
   one-line change with no edit here.
2. **Fingerprint fallback** — ``sha1(first user message)`` for harnesses that
   send no header (aider, hermes, codex …). Stable across the turns of one
   session (the opening task message doesn't change) and different across
   sessions, because every supported proxy harness resends full history.

A truly stateless client (resends only the latest turn, no history, no header)
would fingerprint differently every turn; none of the supported proxy harnesses
do that. The explicit header avoids the issue entirely for harnesses that send one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from agentalloy.api.proxy_models import ProxyRequest

# Sources recorded on the trace's ``session_source`` column.
SOURCE_HEADER = "header"
SOURCE_FINGERPRINT = "fingerprint"

_FINGERPRINT_LEN = 16


def session_header_names() -> list[str]:
    """Lowercased union of session-id header names declared across the registry.

    Computed per call (cheap; the registry is small) so a newly-registered
    harness's header is picked up without restart-order assumptions.
    """
    from agentalloy.providers import REGISTRY

    names: list[str] = []
    for spec in REGISTRY.values():
        header = getattr(spec, "session_header", None)
        if header:
            lowered = header.lower()
            if lowered not in names:
                names.append(lowered)
    return names


def extract_session_header(headers: Mapping[str, str] | None) -> str | None:
    """Return the first present, non-empty session-id header value, or None.

    ``headers`` is any case-insensitive mapping (Starlette/httpx ``Headers``).
    """
    if not headers:
        return None
    # Normalize to lowercase keys ourselves so the lookup is case-insensitive even
    # for a plain dict (Starlette/httpx Headers already are, but don't rely on it).
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in session_header_names():
        value = lowered.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _first_user_text(request: ProxyRequest) -> str | None:
    """Text of the first user message (the session's opening task), or None."""
    for msg in request.messages:
        if msg.role != "user" or not msg.content:
            continue
        content = msg.content
        if isinstance(content, str):
            text = content.strip()
            if text:
                return text
            continue
        # Content-block list (Anthropic-style): concatenate the text blocks.
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        joined = "".join(parts).strip()
        if joined:
            return joined
    return None


def fingerprint_request(request: ProxyRequest) -> str | None:
    """``sha1(first user message)[:16]``, or None when there's no user text yet."""
    text = _first_user_text(request)
    if text is None:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]  # noqa: S324 (non-crypto id)


def resolve_session_key(
    request: ProxyRequest, session_id: str | None
) -> tuple[str | None, str | None]:
    """Resolve ``(session_key, session_source)`` for a request.

    Prefers an explicit header value (``session_id``); falls back to the
    conversation fingerprint. Returns ``(None, None)`` when neither is available
    (e.g. a request with no user message yet) — the caller then treats the turn
    as session-unknown and orients only on phase change.
    """
    if session_id and session_id.strip():
        return session_id.strip(), SOURCE_HEADER
    fp = fingerprint_request(request)
    if fp is not None:
        return fp, SOURCE_FINGERPRINT
    return None, None
