"""OpenAI-compatible HTTP client shared by LM Studio (generation) and
FastFlowLM (embeddings). One shape, two endpoints.

Sync, httpx-based, with a stable error taxonomy so pipeline code can treat
transport failures uniformly.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=30.0, pool=5.0)


class LMClientError(Exception):
    """Base error for the authoring-pipeline LLM clients."""


class LMUnavailable(LMClientError):
    """Endpoint unreachable (connect error, DNS, 5xx)."""


class LMTimeout(LMClientError):
    """Read/connect timeout exceeded."""


class LMBadResponse(LMClientError):
    """2xx response with malformed or unexpected payload."""


class LMModelNotLoaded(LMClientError):
    """Requested model id is not in LM Studio's /v1/models list.

    The v5.3 directive requires surfacing this as a structured 503 to the
    caller rather than silent retry or fallback to a different tier.
    """

    def __init__(self, model: str, loaded: list[str]) -> None:
        self.model = model
        self.loaded = loaded
        super().__init__(f"model {model!r} is not loaded in LM Studio; loaded: {loaded}")


class OpenAICompatClient:
    """Minimal OpenAI-compatible client: chat completions + embeddings."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "not-needed",
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": timeout,
            "headers": {"Authorization": f"Bearer {api_key}"},
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_models(self) -> list[str]:
        """Return the ids of models currently loaded in LM Studio via /v1/models.

        Used by :meth:`ensure_model_loaded` for the directive's precheck
        requirement. Surfaces transport errors through the normal taxonomy.
        """
        try:
            resp = self._client.get("/v1/models")
        except httpx.TimeoutException as e:
            raise LMTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise LMUnavailable(str(e)) from e
        if resp.status_code >= 500:
            raise LMUnavailable(f"HTTP {resp.status_code} from /v1/models: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise LMClientError(f"HTTP {resp.status_code} from /v1/models: {resp.text[:200]}")
        try:
            data: Any = resp.json()
        except ValueError as e:
            raise LMBadResponse(f"non-JSON /v1/models response: {e}") from e
        items: Any = cast(dict[str, Any], data).get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            if items is None:
                return []
            raise LMBadResponse(f"unexpected /v1/models shape: {data!r}")
        ids: list[str] = []
        for item in cast(list[Any], items):
            if isinstance(item, dict):
                item_dict = cast(dict[str, Any], item)
                item_id = item_dict.get("id")
                if isinstance(item_id, str):
                    ids.append(item_id)
        return ids

    def ensure_model_loaded(self, model: str) -> None:
        """Raise :class:`LMModelNotLoaded` if ``model`` is not in /v1/models.

        No silent fallback, no retry — the caller (the service's request
        handler, usually) should map this directly to a 503 with a structured
        error payload including the loaded list.
        """
        loaded = self.list_models()
        if model not in loaded:
            raise LMModelNotLoaded(model, loaded)

    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 16384,
    ) -> str:
        """Single-turn chat completion. Returns the assistant message content.

        ``max_tokens`` defaults high (16k) because reasoning models spend a
        large portion of their budget in ``reasoning_content`` before any
        ``content`` is produced. Under-budgeting here silently returns an
        empty string with finish_reason=length.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        data = self._post_json("/v1/chat/completions", payload)
        choices: Any = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LMBadResponse(f"no choices in response: {data!r}")
        first: Any = cast(list[Any], choices)[0]
        if not isinstance(first, dict):
            raise LMBadResponse(f"malformed choice: {first!r}")
        first_dict = cast(dict[str, Any], first)
        msg: Any = first_dict.get("message")
        if not isinstance(msg, dict):
            raise LMBadResponse(f"malformed message: {first!r}")
        msg_dict = cast(dict[str, Any], msg)
        content: Any = msg_dict.get("content")
        if not isinstance(content, str):
            raise LMBadResponse(f"non-string content: {msg!r}")
        if not content.strip():
            finish: Any = first_dict.get("finish_reason")
            raise LMBadResponse(
                f"empty content (finish_reason={finish!r}); "
                "likely max_tokens exhausted by reasoning_content — raise max_tokens"
            )
        return content

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """Batch embedding. Returns one vector per input text in order."""
        if not texts:
            return []
        payload: dict[str, Any] = {"model": model, "input": texts}
        data = self._post_json("/v1/embeddings", payload)
        items: Any = data.get("data")
        if not isinstance(items, list) or len(cast(list[Any], items)) != len(texts):
            length = len(cast(list[Any], items)) if isinstance(items, list) else "non-list"
            raise LMBadResponse(f"expected {len(texts)} embeddings, got {length}")
        out: list[list[float]] = []
        for i, item in enumerate(cast(list[Any], items)):
            if not isinstance(item, dict):
                raise LMBadResponse(f"embedding[{i}] is not a mapping")
            vec: Any = cast(dict[str, Any], item).get("embedding")
            if not isinstance(vec, list) or not vec:
                raise LMBadResponse(f"embedding[{i}] missing/empty vector")
            out.append([float(x) for x in cast(list[Any], vec)])
        return out

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=payload)
        except httpx.TimeoutException as e:
            raise LMTimeout(str(e)) from e
        except httpx.HTTPError as e:
            raise LMUnavailable(str(e)) from e

        if resp.status_code >= 500:
            raise LMUnavailable(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise LMClientError(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise LMBadResponse(f"non-JSON response from {path}: {e}") from e
        if not isinstance(data, dict):
            raise LMBadResponse(f"expected object from {path}, got {type(data).__name__}")
        return cast("dict[str, Any]", data)
