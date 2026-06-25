"""Shared port classification + reserved-port derivation for install.

Two install steps need to reason about ports:

* ``write-env`` must refuse a ``--port`` that collides with the embed/rerank
  servers (a pure value check, since those servers are not running yet) or that
  is already held by a *foreign* process.
* ``verify`` must confirm the runtime port is either free or held by
  agentalloy's own service.

Both questions reduce to "what is on this port?" — answered here once via
``classify_port`` so the two callers share a single source of truth.
"""

from __future__ import annotations

import json
import socket
from typing import Literal
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agentalloy.install.runtime_artifacts import EMBED_PORT, RERANK_PORT

PortStatus = Literal[
    "free",
    "ours",
    "foreign",
    "foreign_nonjson",
    "foreign_nonhttp",
]


def classify_port(port: int, *, timeout: float = 5.0) -> tuple[PortStatus, str]:
    """Classify what is listening on ``127.0.0.1:port``.

    Returns ``(status, detail)`` where ``status`` is one of:

    * ``free``            — nothing accepted the TCP connect.
    * ``ours``            — ``/health`` JSON ``status`` is ``healthy``/``degraded``
                            (a agentalloy service holds the port).
    * ``foreign``         — ``/health`` returned JSON with some other status.
    * ``foreign_nonjson`` — ``/health`` responded but the body wasn't JSON.
    * ``foreign_nonhttp`` — TCP connect succeeded but no usable HTTP ``/health``.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        result = s.connect_ex(("127.0.0.1", port))
    if result != 0:
        return "free", f"Port {port} is available"

    # Port is in use — ask /health whether it's agentalloy.
    try:
        req = Request(f"http://localhost:{port}/health", method="GET")
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read())
        # /health uses a three-state status: "healthy" (all-green), "degraded"
        # (embed/telemetry down but service is bound), or "unavailable". Both
        # healthy and degraded mean a agentalloy server holds the port.
        if body.get("status") in ("healthy", "degraded"):
            return "ours", f"Port {port} is bound by agentalloy (status={body.get('status')!r})"
        return (
            "foreign",
            f"Port {port} is bound by a service that returned status={body.get('status')!r}",
        )
    except json.JSONDecodeError as exc:
        # /health responded but the body wasn't JSON — likely a foreign service
        # returning HTML, or agentalloy serving an older/error response.
        return "foreign_nonjson", f"Port {port} /health returned non-JSON response: {exc}"
    except Exception as exc:  # noqa: BLE001 - URLError/timeout/conn-refused/etc.
        # Something accepted the TCP connect but isn't speaking HTTP the way we
        # expect.
        return "foreign_nonhttp", f"Port {port} is bound by a non-agentalloy process ({exc})"


def _port_from_url(url: str | None) -> int | None:
    """Extract the port from an ``http://host:port`` URL, or None if absent."""
    if not url:
        return None
    try:
        return urlparse(url).port
    except ValueError:
        return None


def reserved_ports(values: dict[str, str]) -> dict[int, str]:
    """Map each port reserved by the install to a human-readable role label.

    Derived from the *rendered* env values so that an operator who relocates a
    server via ``--overrides RUNTIME_EMBED_BASE_URL=...`` reserves the new port,
    not the stale constant. Falls back to the ``EMBED_PORT``/``RERANK_PORT``
    constants when a URL is missing or unparseable.
    """
    embed = _port_from_url(values.get("RUNTIME_EMBED_BASE_URL")) or EMBED_PORT
    rerank = _port_from_url(values.get("SIGNAL_INTENT_RERANK_URL")) or RERANK_PORT
    return {embed: "embed", rerank: "reranker"}
