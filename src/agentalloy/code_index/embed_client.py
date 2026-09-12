"""Embedding client for the local LFM embedding server.

OpenAI-compatible ``/v1/embeddings`` against llama-server (default
``http://localhost:48951``, model ``LFM2.5-Embedding-350M``). Ports v1's
client (``agentalloy/lm_client.py``) trimmed to the embeddings surface, with
the Rust-side vector contract applied on the Python side:

* **MRL truncation** — models that natively emit wider than the target dim
  (LFM2.5-Embedding-350M: 1024-d) are truncated to the first 768 components
  and L2-renormalized (mirrors Rust ``dense.rs`` ``truncate_to_dim`` +
  ``l2_normalize``); a native 768-d vector (nomic-embed-text-v1.5) passes
  through unchanged.
* **Progressive halving** — a batch the server rejects (input too large)
  falls back to per-item embedding, halving length on each rejection, so one
  pathological symbol degrades to a shorter embed instead of failing the job
  (v1 ``pipeline._embed_one_with_halving``).

Sync on purpose: the v2 service handlers are sync (``def``), so the ingest
pipeline calls this directly from its worker thread context.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx

from .protocols import EMBEDDING_DIM, l2_normalize

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 16
MIN_HALVING_CHARS = 256
"""Below this length a rejected embed is a server problem, not an input
problem — re-raise instead of halving further."""

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=30.0, pool=5.0)


class EmbedError(Exception):
    """Base for embedding-client errors."""


class EmbedUnavailableError(EmbedError):
    """Endpoint unreachable (connect error, DNS, 5xx)."""


class EmbedTimeoutError(EmbedError):
    """Read/connect timeout exceeded."""


class EmbedBadResponseError(EmbedError):
    """2xx response with malformed or unexpected payload."""


def truncate_to_dim(vec: list[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """MRL truncate + L2-renormalize (Rust dense.rs contract)."""
    if len(vec) < dim:
        raise EmbedBadResponseError(f"embedding shorter than target dim: {len(vec)} < {dim}")
    return l2_normalize(vec[:dim])


class EmbedClient:
    """Minimal OpenAI-compatible embeddings client with the MRL contract."""

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "LFM2.5-Embedding-350M",
        api_key: str = "not-needed",
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EmbedClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def is_available(self) -> bool:
        """Liveness probe: ``/v1/models`` must list at least one loaded model.

        llama-server answers ``/embeddings`` with whatever single model it
        loaded, regardless of the request's ``model`` field, so exact id
        matching would false-negative on any name mismatch. Never raises —
        retrieval/capability code decides "lexical-only" from a False return.
        """
        try:
            return bool(self.list_models())
        except EmbedError:
            return False

    def list_models(self) -> list[str]:
        try:
            resp = self._client.get("/v1/models")
        except httpx.TimeoutException as e:
            raise EmbedTimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise EmbedUnavailableError(str(e)) from e
        if resp.status_code >= 500:
            raise EmbedUnavailableError(
                f"HTTP {resp.status_code} from /v1/models: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise EmbedError(f"HTTP {resp.status_code} from /v1/models: {resp.text[:200]}")
        try:
            data: Any = resp.json()
        except ValueError as e:
            raise EmbedBadResponseError(f"non-JSON /v1/models response: {e}") from e
        items: Any = data.get("data") if isinstance(data, dict) else None
        if items is None:
            return []
        if not isinstance(items, list):
            raise EmbedBadResponseError(f"unexpected /v1/models shape: {data!r}")
        return [
            str(item["id"])
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed; returns one 768-d unit vector per text, in order.

        Transport errors raise the :class:`EmbedError` taxonomy; callers
        either retry the whole job (parse phase failures) or fall back to
        :func:`embed_texts`'s per-item halving (rejection of one input).
        """
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        data = self._post_json("/v1/embeddings", payload)
        items: Any = data.get("data")
        if not isinstance(items, list) or len(cast(list[Any], items)) != len(texts):
            length = len(cast(list[Any], items)) if isinstance(items, list) else "non-list"
            raise EmbedBadResponseError(f"expected {len(texts)} embeddings, got {length}")
        out: list[list[float]] = []
        for i, item in enumerate(cast(list[Any], items)):
            if not isinstance(item, dict):
                raise EmbedBadResponseError(f"embedding[{i}] is not a mapping")
            vec: Any = cast(dict[str, Any], item).get("embedding")
            if not isinstance(vec, list) or not vec:
                raise EmbedBadResponseError(f"embedding[{i}] missing/empty vector")
            out.append(truncate_to_dim([float(x) for x in cast(list[Any], vec)]))
        return out

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=payload)
        except httpx.TimeoutException as e:
            raise EmbedTimeoutError(str(e)) from e
        except httpx.HTTPError as e:
            raise EmbedUnavailableError(str(e)) from e
        if resp.status_code >= 500:
            raise EmbedUnavailableError(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise EmbedError(f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise EmbedBadResponseError(f"non-JSON response from {path}: {e}") from e
        if not isinstance(data, dict):
            raise EmbedBadResponseError(f"expected object from {path}, got {type(data).__name__}")
        return cast("dict[str, Any]", data)


def embed_one_with_halving(client: EmbedClient, text: str) -> list[float]:
    """Embed one text, halving its length on each server rejection.

    Re-raises the last error once the text is at ``MIN_HALVING_CHARS`` — by
    then the failure is the server, not the input.
    """
    attempt = text
    while True:
        try:
            return client.embed([attempt])[0]
        except EmbedError:
            if len(attempt) <= MIN_HALVING_CHARS:
                raise
            logger.warning("embed rejected a %d-char input; retrying at half length", len(attempt))
            attempt = attempt[: len(attempt) // 2]


def embed_texts(
    client: EmbedClient,
    texts: list[str],
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    on_batch: Any = None,
) -> list[list[float]]:
    """Batch-embed a list; a rejected batch degrades to per-item halving.

    ``on_batch`` (optional callable) is invoked after each successful batch —
    the pipeline uses it as a progress heartbeat.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        try:
            vectors.extend(client.embed(batch))
        except EmbedError:
            for text in batch:
                vectors.append(embed_one_with_halving(client, text))
        if on_batch is not None:
            on_batch()
    return vectors
