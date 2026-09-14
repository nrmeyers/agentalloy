"""Client (M1b) — exact request shape, failure classification, cooldown latch.

Every failure class must map to the loop's two postures: transport/HTTP
failures trip the process-wide latch (the router's 503 path); an empty
completion does NOT latch (the loop's one plain-text retry decides).
"""

from __future__ import annotations

import httpx
import pytest

from agentalloy.local_agent import client as la_client
from agentalloy.local_agent.client import (
    ClientStageError,
    OpenAICompatClient,
    endpoint_down_reason,
)
from agentalloy.local_agent.config import LocalAgentConfig, LocalAgentMode
from agentalloy.local_agent.protocol import StagePrompt

PROMPT = StagePrompt(system="sys", user="user")


def _config() -> LocalAgentConfig:
    return LocalAgentConfig(
        mode=LocalAgentMode.ON,
        url="http://127.0.0.1:59999",
        model="test-model",
        timeout_ms=1000,
        max_steps=1,
        max_tokens=64,
        result_cap_chars=100,
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class TestChatSuccess:
    def test_returns_content_and_latency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def fake_post(
            url: str, json: dict | None = None, timeout: float | None = None
        ) -> httpx.Response:
            seen["url"] = url
            seen["json"] = json
            seen["timeout"] = timeout
            return _completion("hello")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        content, ms = OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=64)
        assert content == "hello"
        assert ms >= 0
        assert seen["url"] == "http://127.0.0.1:59999/v1/chat/completions"
        assert seen["timeout"] == pytest.approx(1.0)

    def test_payload_is_the_spike_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def fake_post(
            url: str, json: dict | None = None, timeout: float | None = None
        ) -> httpx.Response:
            seen["json"] = json
            return _completion("ok")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=64)
        payload = seen["json"]
        assert payload["model"] == "test-model"
        assert payload["temperature"] == 0.0
        assert payload["max_tokens"] == 64
        assert payload["stream"] is False
        assert payload["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]
        assert "response_format" not in payload

    def test_response_format_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": "classify", "strict": True, "schema": {}},
        }
        seen: dict = {}

        def fake_post(
            url: str, json: dict | None = None, timeout: float | None = None
        ) -> httpx.Response:
            seen["json"] = json
            return _completion('{"action": "none"}')

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        content, _ = OpenAICompatClient(_config()).chat(
            PROMPT, stage="classify", max_tokens=64, response_format=fmt
        )
        assert content == '{"action": "none"}'
        assert seen["json"]["response_format"] is fmt


class TestFailureClassification:
    def test_5xx_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            la_client.httpx, "post", lambda *a, **k: httpx.Response(500, text="boom")
        )
        with pytest.raises(ClientStageError) as exc:
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert "HTTP 500" in str(exc.value)
        assert exc.value.empty_completion is False
        assert endpoint_down_reason() == "HTTP 500"

    def test_4xx_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            la_client.httpx, "post", lambda *a, **k: httpx.Response(400, text="bad")
        )
        with pytest.raises(ClientStageError):
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert endpoint_down_reason() == "HTTP 400"

    def test_transport_error_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(*a: object, **k: object) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        with pytest.raises(ClientStageError) as exc:
            OpenAICompatClient(_config()).chat(PROMPT, stage="fill", max_tokens=8)
        assert "transport error" in str(exc.value)
        assert "ConnectError" in endpoint_down_reason() or "transport" in endpoint_down_reason()

    def test_timeout_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(*a: object, **k: object) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        with pytest.raises(ClientStageError) as exc:
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert "timed out after 1000 ms" in str(exc.value)
        assert "timeout" in endpoint_down_reason()

    def test_non_json_body_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            la_client.httpx,
            "post",
            lambda *a, **k: httpx.Response(
                200, text="<html>oops</html>", headers={"content-type": "text/html"}
            ),
        )
        with pytest.raises(ClientStageError) as exc:
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert "non-JSON" in str(exc.value)
        assert endpoint_down_reason() == "non-JSON response body"

    def test_malformed_shape_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            la_client.httpx,
            "post",
            lambda *a, **k: httpx.Response(200, json={"choices": []}),
        )
        with pytest.raises(ClientStageError) as exc:
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert "malformed" in str(exc.value)
        assert endpoint_down_reason() == "malformed chat completion shape"


class TestEmptyCompletion:
    def test_empty_completion_does_not_latch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def fake_post(*a: object, **k: object) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        client = OpenAICompatClient(_config())
        with pytest.raises(ClientStageError) as exc:
            client.chat(PROMPT, stage="classify", max_tokens=8)
        assert exc.value.empty_completion is True
        assert endpoint_down_reason() is None
        # The second call must still attempt HTTP — the endpoint is not down.
        with pytest.raises(ClientStageError):
            client.chat(PROMPT, stage="classify", max_tokens=8)
        assert len(calls) == 2


class TestCooldownLatch:
    def test_latch_blocks_all_subsequent_calls_without_http(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = []

        def fake_post(*a: object, **k: object) -> httpx.Response:
            calls.append(1)
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        client = OpenAICompatClient(_config())
        with pytest.raises(ClientStageError):
            client.chat(PROMPT, stage="classify", max_tokens=8)
        assert len(calls) == 1

        # Every later stage fails fast, naming the cooldown, with no HTTP attempt.
        for stage in ("fill", "answer"):
            with pytest.raises(ClientStageError) as exc:
                client.chat(PROMPT, stage=stage, max_tokens=8)
            assert "cooldown" in str(exc.value)
        assert len(calls) == 1

    def test_latch_is_process_wide(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_post(*a: object, **k: object) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(la_client.httpx, "post", fake_post)
        with pytest.raises(ClientStageError):
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        # A fresh client instance observes the same latch.
        with pytest.raises(ClientStageError):
            OpenAICompatClient(_config()).chat(PROMPT, stage="classify", max_tokens=8)
        assert endpoint_down_reason() is not None
