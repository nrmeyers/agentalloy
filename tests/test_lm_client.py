"""Unit tests for the OpenAI-compatible LM client's new precheck surface.

Covers list_models / ensure_model_loaded / LMModelNotLoaded via
``httpx.MockTransport`` — no live LM Studio required.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from agentalloy.lm_client import (
    LMBadResponse,
    LMModelNotLoaded,
    LMUnavailable,
    OpenAICompatClient,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatClient:
    """Build a client with a MockTransport instead of a real connection."""
    return OpenAICompatClient(
        "http://mock",
        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_returns_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "qwen/qwen3.6-35b-a3b", "object": "model"},
                    {"id": "qwen/qwen2.5-coder-14b", "object": "model"},
                    {"id": "nomic-embed-text-v1.5", "object": "model"},
                ],
            },
        )

    client = _client(handler)
    try:
        ids = client.list_models()
    finally:
        client.close()

    assert ids == [
        "qwen/qwen3.6-35b-a3b",
        "qwen/qwen2.5-coder-14b",
        "nomic-embed-text-v1.5",
    ]


def test_list_models_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    try:
        assert client.list_models() == []
    finally:
        client.close()


def test_list_models_skips_malformed_items() -> None:
    """An item without an id (or non-string id) is silently dropped rather
    than crashing the whole call."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "good"},
                    {"object": "model"},  # no id
                    {"id": 42},  # non-string id
                    {"id": "also-good"},
                ]
            },
        )

    client = _client(handler)
    try:
        assert client.list_models() == ["good", "also-good"]
    finally:
        client.close()


def test_list_models_5xx_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="upstream is toast")

    client = _client(handler)
    try:
        with pytest.raises(LMUnavailable, match="502"):
            client.list_models()
    finally:
        client.close()


def test_list_models_bad_json_maps_to_bad_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = _client(handler)
    try:
        with pytest.raises(LMBadResponse):
            client.list_models()
    finally:
        client.close()


def test_list_models_missing_data_field_returns_empty() -> None:
    """When /v1/models returns no 'data' key, treat it as an empty list
    (graceful handling for LM Studio backends that return data: null)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    client = _client(handler)
    try:
        assert client.list_models() == []
    finally:
        client.close()


# ---------------------------------------------------------------------------
# ensure_model_loaded
# ---------------------------------------------------------------------------


def test_ensure_model_loaded_passes_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "qwen/qwen3.6-35b-a3b"}]})

    client = _client(handler)
    try:
        client.ensure_model_loaded("qwen/qwen3.6-35b-a3b")  # no raise
    finally:
        client.close()


def test_ensure_model_loaded_raises_with_loaded_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "qwen/qwen2.5-coder-14b"},
                    {"id": "nomic-embed-text-v1.5"},
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(LMModelNotLoaded) as exc_info:
            client.ensure_model_loaded("qwen/qwen3.6-35b-a3b")
    finally:
        client.close()

    assert exc_info.value.model == "qwen/qwen3.6-35b-a3b"
    assert "qwen/qwen2.5-coder-14b" in exc_info.value.loaded
    # Error payload carries the loaded list so the caller can surface it in
    # a structured 503 response (per v5.3 directive §3.3).
    assert "not loaded" in str(exc_info.value)


def test_ensure_model_loaded_does_not_retry() -> None:
    """The directive forbids silent retry on model-not-loaded — one call,
    one verdict."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    try:
        with pytest.raises(LMModelNotLoaded):
            client.ensure_model_loaded("missing")
    finally:
        client.close()

    assert call_count["n"] == 1
