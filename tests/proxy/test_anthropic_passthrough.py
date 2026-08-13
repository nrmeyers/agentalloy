"""Tests for the native Anthropic passthrough HTTP client.

Uses ``httpx.MockTransport`` to capture the outbound request and assert that
headers are filtered/preserved correctly, body bytes and query strings are
relayed verbatim, and SSE responses stream byte-for-byte.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from agentalloy.api.anthropic_passthrough import (
    HOP_BY_HOP,
    AnthropicPassthroughClient,
    forward_headers,
)


def test_forward_headers_strips_and_rewrites() -> None:
    """Hop-by-hop, host, and content-length are stripped; Host is rewritten."""
    inbound = {
        "Host": "127.0.0.1:47950",
        "Content-Length": "123",
        "Connection": "keep-alive",
        "Keep-Alive": "timeout=5",
        "Proxy-Authenticate": "Basic",
        "Proxy-Authorization": "Basic abc",
        "TE": "trailers",
        "Trailers": "X-Foo",
        "Transfer-Encoding": "chunked",
        "Upgrade": "h2c",
        "Authorization": "Bearer caller-token",
        "X-Api-Key": "sk-ant-caller",
        "Anthropic-Beta": "prompt-caching-2024-07-31",
        "Anthropic-Version": "2023-06-01",
        "X-Claude-Code-Session-Id": "sess-123",
        "Content-Type": "application/json",
    }

    out = forward_headers(inbound, "api.anthropic.com")

    # Hop-by-hop + client-managed stripped (case-insensitive).
    for name in HOP_BY_HOP:
        assert name not in {k.lower() for k in out}
    assert "content-length" not in {k.lower() for k in out}

    # Host rewritten to the upstream host (single entry).
    assert out["Host"] == "api.anthropic.com"

    # Credential / protocol headers preserved verbatim.
    assert out["Authorization"] == "Bearer caller-token"
    assert out["X-Api-Key"] == "sk-ant-caller"
    assert out["Anthropic-Beta"] == "prompt-caching-2024-07-31"
    assert out["Anthropic-Version"] == "2023-06-01"
    assert out["X-Claude-Code-Session-Id"] == "sess-123"
    assert out["Content-Type"] == "application/json"


def test_forward_headers_strip_is_case_insensitive() -> None:
    """Lowercased inbound hop-by-hop / managed headers are also stripped."""
    inbound = {
        "host": "local",
        "content-length": "9",
        "transfer-encoding": "chunked",
        "x-api-key": "sk-ant-caller",
    }
    out = forward_headers(inbound, "api.anthropic.com")
    assert "transfer-encoding" not in {k.lower() for k in out}
    assert "content-length" not in {k.lower() for k in out}
    assert out["Host"] == "api.anthropic.com"
    assert out["x-api-key"] == "sk-ant-caller"


async def test_forward_sends_exact_request_and_returns_response() -> None:
    """forward() sends verbatim body + filtered headers to the expected URL."""
    captured: dict[str, httpx.Request] = {}
    body = b'{"model":"claude-opus-4-8","messages":[{"role":"user","content":"hi"}]}'
    upstream_body = b'{"id":"msg_1","type":"message","role":"assistant"}'

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        captured["req"] = request
        return httpx.Response(
            200,
            content=upstream_body,
            headers={"content-type": "application/json"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    client = AnthropicPassthroughClient(
        upstream_base_url="https://api.anthropic.com",
        client=httpx.AsyncClient(transport=transport),
    )

    resp = await client.forward(
        path="/v1/messages",
        query_string="",
        inbound_headers={
            "Host": "127.0.0.1:47950",
            "Content-Length": "999",
            "Connection": "keep-alive",
            "Authorization": "Bearer caller-token",
            "Anthropic-Version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body=body,
        method="POST",
    )

    req = captured["req"]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.method == "POST"
    # Exact body bytes relayed.
    assert req.content == body
    # Filtered headers: hop-by-hop + content-length dropped, Host rewritten.
    assert req.headers["host"] == "api.anthropic.com"
    # content-length is recomputed by httpx from the body, not the inbound "999".
    assert req.headers["content-length"] == str(len(body))
    assert req.headers["authorization"] == "Bearer caller-token"
    assert req.headers["anthropic-version"] == "2023-06-01"

    # Response returned unchanged.
    assert resp.status_code == 200
    assert resp.content == upstream_body

    await client.aclose()


async def test_forward_preserves_query_string() -> None:
    """The raw query string is appended to the upstream URL unchanged."""
    captured: dict[str, httpx.Request] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, content=b"{}", request=request)

    client = AnthropicPassthroughClient(
        upstream_base_url="https://api.anthropic.com/",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    await client.forward(
        path="/v1/messages",
        query_string="beta=true",
        inbound_headers={"X-Api-Key": "sk-ant-caller"},
        body=b"{}",
    )

    assert str(captured["req"].url) == "https://api.anthropic.com/v1/messages?beta=true"
    await client.aclose()


def test_upstream_base_url_property() -> None:
    """The configured base URL is exposed via the property unchanged."""
    client = AnthropicPassthroughClient(upstream_base_url="https://example.test")
    assert client.upstream_base_url == "https://example.test"


async def test_stream_relays_sse_byte_for_byte() -> None:
    """stream() relays a known SSE byte sequence verbatim with its content-type."""
    sse_chunks = [
        b'event: message_start\ndata: {"type":"message_start"}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    expected = b"".join(sse_chunks)
    captured: dict[str, httpx.Request] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        async def _aiter() -> AsyncIterator[bytes]:
            for chunk in sse_chunks:
                yield chunk

        captured["req"] = request
        return httpx.Response(
            200,
            content=_aiter(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    client = AnthropicPassthroughClient(
        upstream_base_url="https://api.anthropic.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    relayed = bytearray()
    async with client.stream(
        path="/v1/messages",
        query_string="",
        inbound_headers={"X-Api-Key": "sk-ant-caller", "Anthropic-Version": "2023-06-01"},
        body=b'{"stream":true}',
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        async for raw in resp.aiter_raw():
            relayed.extend(raw)

    assert bytes(relayed) == expected
    # Caller credentials were forwarded to the upstream stream request.
    assert captured["req"].headers["x-api-key"] == "sk-ant-caller"
    await client.aclose()


async def test_aclose_does_not_close_injected_client() -> None:
    """An injected client is not owned, so aclose() leaves it usable."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", request=request)

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicPassthroughClient(client=injected)
    await client.aclose()
    assert not injected.is_closed
    await injected.aclose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
