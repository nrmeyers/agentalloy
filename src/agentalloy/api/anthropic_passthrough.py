"""Native Anthropic passthrough HTTP client.

Forwards an inbound Anthropic Messages request to a configurable upstream
*verbatim*, carrying the caller's own credentials. This module adds, stores,
and rewrites **no** Anthropic credential of its own — authentication is a
transparent passthrough: whatever ``authorization`` / ``x-api-key`` the caller
supplied is relayed unchanged to the upstream.

Header handling mirrors the Headroom proxy's denylist approach (RFC 7230 §6.1
hop-by-hop headers stripped, plus the client-managed ``host`` and
``content-length``), reimplemented over ``httpx`` for the AgentAlloy proxy.
Both a non-streaming :meth:`AnthropicPassthroughClient.forward` and an
SSE-relaying :meth:`AnthropicPassthroughClient.stream` are provided.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

import httpx

__all__ = ["HOP_BY_HOP", "AnthropicPassthroughClient", "forward_headers"]

# Hop-by-hop header names that must not be forwarded across a proxy boundary
# (RFC 7230 §6.1). Lowercased for case-insensitive matching.
HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Headers managed by the HTTP client itself: ``host`` is rewritten to the
# upstream host and ``content-length`` is recomputed by httpx from the body.
_CLIENT_MANAGED: frozenset[str] = frozenset({"host", "content-length"})

# Default upstream timeouts: short connect/pool, long read for streamed
# responses, moderate write for large request bodies.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)


def forward_headers(inbound: Mapping[str, str], upstream_host: str) -> dict[str, str]:
    """Build the outbound header set for an upstream request.

    Applies a denylist filter to ``inbound``:

    * hop-by-hop headers (:data:`HOP_BY_HOP`) are dropped;
    * ``content-length`` is dropped (httpx recomputes it from the body);
    * ``host`` is dropped from the inbound set and re-added as ``upstream_host``.

    Every other header is preserved with its original casing — critically the
    credential and protocol headers ``authorization``, ``x-api-key``,
    ``anthropic-beta``, ``anthropic-version``, ``x-claude-code-session-id`` and
    ``content-type``. Matching against the denylist is case-insensitive.

    Args:
        inbound: The caller's request headers.
        upstream_host: Value to set as the outbound ``Host`` header (the
            upstream URL's host).

    Returns:
        A new dict of outbound headers, original casing preserved where
        practical, with a single ``Host`` entry pointing at ``upstream_host``.
    """
    out: dict[str, str] = {}
    for name, value in inbound.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in _CLIENT_MANAGED:
            continue
        out[name] = value
    out["Host"] = upstream_host
    return out


class AnthropicPassthroughClient:
    """Forward Anthropic Messages requests verbatim to a configurable upstream.

    The client never injects or stores Anthropic credentials of its own; it
    relays the caller's headers (filtered for hop-by-hop / client-managed
    entries) and body bytes unchanged.
    """

    def __init__(
        self,
        upstream_base_url: str = "https://api.anthropic.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the passthrough client.

        Args:
            upstream_base_url: Base URL the inbound path/query is appended to.
            client: An optional pre-built async client (e.g. one backed by an
                ``httpx.MockTransport`` in tests). When omitted, a client with
                the module default timeouts is constructed and owned by this
                instance.
        """
        self._upstream_base_url = upstream_base_url
        self._upstream_host = httpx.URL(upstream_base_url).host
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    @property
    def upstream_base_url(self) -> str:
        """The configured upstream base URL."""
        return self._upstream_base_url

    def _build_url(self, path: str, query_string: str) -> str:
        """Join the base URL, request path, and optional query string."""
        url = self._upstream_base_url.rstrip("/") + path
        if query_string:
            url = f"{url}?{query_string}"
        return url

    async def forward(
        self,
        *,
        path: str,
        query_string: str,
        inbound_headers: Mapping[str, str],
        body: bytes,
        method: str = "POST",
    ) -> httpx.Response:
        """Forward a request to the upstream and return the full response.

        Non-streaming: the response body is read in full before returning. The
        caller inspects ``.status_code`` / ``.headers`` / ``.content``.

        Args:
            path: Inbound request path (e.g. ``/v1/messages``).
            query_string: Raw query string without the leading ``?`` (may be
                empty).
            inbound_headers: The caller's request headers, filtered via
                :func:`forward_headers` before sending.
            body: Raw request body bytes, forwarded unchanged.
            method: HTTP method (default ``POST``).

        Returns:
            The upstream :class:`httpx.Response`.
        """
        url = self._build_url(path, query_string)
        headers = forward_headers(inbound_headers, self._upstream_host)
        return await self._client.request(method, url, headers=headers, content=body)

    def stream(
        self,
        *,
        path: str,
        query_string: str,
        inbound_headers: Mapping[str, str],
        body: bytes,
        method: str = "POST",
    ) -> contextlib.AbstractAsyncContextManager[httpx.Response]:
        """Open a streaming request to the upstream.

        Returns the async context manager from ``httpx.AsyncClient.stream``;
        the caller enters it and relays ``response.aiter_raw()`` byte-for-byte
        (e.g. for SSE relaying).

        Args:
            path: Inbound request path (e.g. ``/v1/messages``).
            query_string: Raw query string without the leading ``?`` (may be
                empty).
            inbound_headers: The caller's request headers, filtered via
                :func:`forward_headers` before sending.
            body: Raw request body bytes, forwarded unchanged.
            method: HTTP method (default ``POST``).

        Returns:
            An async context manager yielding a streaming
            :class:`httpx.Response`.
        """
        url = self._build_url(path, query_string)
        headers = forward_headers(inbound_headers, self._upstream_host)
        return self._client.stream(method, url, headers=headers, content=body)

    async def aclose(self) -> None:
        """Close the underlying client if this instance owns it."""
        if self._owns_client:
            await self._client.aclose()
