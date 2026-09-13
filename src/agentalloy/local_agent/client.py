"""Fail-open OpenAI-compatible client for the local agent (M1b).

Wraps one llama-server (or compatible) endpoint: ``chat`` builds the exact
spike request (``temperature=0``, ``stream=false``, optional strict
``response_format``) and classifies every non-2xx / non-JSON outcome into
``ClientStageError`` so the loop can fail open the LM stage, never the
request.

The failure cooldown latch mirrors the design's one-shot rule: after a
transport failure the endpoint is treated as down for the whole process —
subsequent calls raise without any HTTP attempt. The latch is deliberately
never re-tried (a dead local server stays dead for the process's lifetime;
recovery is a restart) and is reset by ``reset_client_caches()`` for tests.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from agentalloy.local_agent.config import LocalAgentConfig
from agentalloy.local_agent.protocol import StagePrompt

logger = logging.getLogger(__name__)


class ClientStageError(Exception):
    """The LM endpoint failed; the request degrades to the fail-open path."""

    def __init__(
        self,
        stage: str,
        cause: str,
        latency_ms: int | None = None,
        *,
        empty_completion: bool = False,
    ) -> None:
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause
        self.latency_ms = latency_ms
        # The endpoint answered but the completion was empty — the documented
        # ``json_schema``-unsupported signature (and reasoning-exhausted-the-
        # budget). Not an endpoint-down signal: the latch stays clear and the
        # loop gets its one plain-text retry of the stage.
        self.empty_completion = empty_completion


class _EndpointDown:
    """Process-wide cooldown latch — set once, read often, no re-attempts."""

    def __init__(self) -> None:
        self._reason: str | None = None

    @property
    def reason(self) -> str | None:
        return self._reason

    def trip(self, reason: str) -> None:
        if self._reason is None:
            logger.warning("local agent LM endpoint down for this process: %s", reason)
        self._reason = reason

    def reset(self) -> None:
        self._reason = None


_down = _EndpointDown()


def endpoint_down_reason() -> str | None:
    """Non-None while the cooldown latch is tripped (telemetry/tests)."""
    return _down.reason


def reset_client_caches() -> None:
    """Reset the process-wide endpoint latch (tests)."""
    _down.reset()


# Non-JSON bodies up to this many chars are quoted in the warning — anything
# longer is almost certainly an HTML error page and is summarized instead.
_ERROR_BODY_SNIPPET_CHARS = 200


def _describe_body(body: str, content_type: str) -> str:
    if content_type.startswith("application/json") and body:
        return body[:_ERROR_BODY_SNIPPET_CHARS]
    if body:
        return f"{content_type or 'no content-type'} body ({len(body)} chars)"
    return "empty body"


class OpenAICompatClient:
    """Minimal chat-completions client; every failure is :class:`ClientStageError`."""

    def __init__(self, config: LocalAgentConfig) -> None:
        self._config = config

    @property
    def url(self) -> str:
        return f"{self._config.url}/v1/chat/completions"

    def chat(
        self,
        prompt: StagePrompt,
        *,
        stage: str,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        """Run one completion; return ``(content, latency_ms)``.

        Raises :class:`ClientStageError` for any transport, HTTP, or decode
        failure, tripping the process-wide cooldown latch.
        """
        if _down.reason is not None:
            raise ClientStageError(stage, f"endpoint down (cooldown): {_down.reason}")

        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "temperature": self._config.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        started = time.perf_counter()
        try:
            resp = httpx.post(self.url, json=payload, timeout=self._config.timeout_ms / 1000)
        except httpx.TimeoutException as exc:
            latency = int((time.perf_counter() - started) * 1000)
            _down.trip(f"timeout after {self._config.timeout_ms} ms")
            raise ClientStageError(
                stage, f"request timed out after {self._config.timeout_ms} ms", latency
            ) from exc
        except httpx.HTTPError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            _down.trip(f"transport error: {exc.__class__.__name__}")
            raise ClientStageError(stage, f"transport error: {exc}", latency) from exc

        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            detail = _describe_body(resp.text, resp.headers.get("content-type", ""))
            _down.trip(f"HTTP {resp.status_code}")
            logger.warning(
                "local agent LM endpoint %s returned HTTP %d in %d ms: %s",
                self.url,
                resp.status_code,
                latency,
                detail,
            )
            raise ClientStageError(stage, f"HTTP {resp.status_code}", latency)

        try:
            data = resp.json()
        except ValueError as exc:
            _down.trip("non-JSON response body")
            raise ClientStageError(
                stage,
                f"non-JSON response body: {_describe_body(resp.text, resp.headers.get('content-type', ''))}",
                latency,
            ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            _down.trip("malformed chat completion shape")
            raise ClientStageError(
                stage, "malformed chat completion (missing choices[].message.content)", latency
            ) from exc
        if not isinstance(content, str) or not content.strip():
            # No latch: an empty completion is a per-build capability signal
            # (json_schema unsupported) or a reasoning-budget exhaustion, not
            # a dead endpoint. The loop decides (one plain-text retry).
            raise ClientStageError(
                stage, "empty completion content", latency, empty_completion=True
            )
        return content, latency
