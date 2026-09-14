"""Minimal OpenAI-compatible chat client (stdlib urllib, no deps)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class LMError(RuntimeError):
    """Endpoint unreachable or returned a non-2xx / malformed response."""


class Client:
    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str,
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout_s: int = 120,
    ):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def chat(self, messages: list[dict], response_format: dict | None = None) -> dict[str, Any]:
        """One chat completion. Returns {content, latency_ms, completion_tokens}."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            # llama.cpp (master) OpenAI-compat shape: json_schema with a named,
            # strict schema, compiled to a GBNF grammar server-side.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "newagent_call",
                    "strict": True,
                    "schema": response_format,
                },
            }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 — local endpoint
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            raise LMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LMError(f"request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - t0) * 1000)

        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            completion_tokens = payload.get("usage", {}).get("completion_tokens", 0)
        except (KeyError, IndexError, TypeError) as exc:
            raise LMError(f"malformed response: {str(payload)[:300]}") from exc
        content = content if isinstance(content, str) else json.dumps(content)
        if not content and finish_reason == "length":
            # Thinking model burned the whole token budget on internal reasoning
            # and never produced an answer — retryable, not a parse problem.
            raise LMError(
                f"empty content (finish_reason=length after {completion_tokens} tokens; "
                "thinking budget exhausted)"
            )
        return {
            "content": content,
            "latency_ms": latency_ms,
            "completion_tokens": int(completion_tokens or 0),
            "finish_reason": finish_reason,
        }
