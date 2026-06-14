"""Provider factory for embedding backends.

Returns an embedding client that implements the ``embed(texts)`` protocol.
One provider is supported:

* ``"openai_compat"`` (default) — uses the OpenAI-compatible ``/v1/embeddings``
  endpoint (e.g. llama-server in ``--embeddings`` mode).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from agentalloy.config import Settings

if TYPE_CHECKING:
    from agentalloy.lm_client import OpenAICompatClient

logger = logging.getLogger(__name__)


class EmbedClient(Protocol):
    """Minimal embedding interface that both backends implement."""

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]: ...  # pragma: no cover

    def close(self) -> None: ...  # pragma: no cover


def get_embed_client(settings: Settings) -> EmbedClient:
    """Return an embedding client for the configured provider.

    Parameters
    ----------
    settings:
        :class:`Settings` instance (from ``get_settings()`` or tests).

    Returns
    -------
    An object with ``embed(texts)`` and ``close()`` methods.

    Raises
    ------
    ValueError
        If ``settings.embedding_provider`` is not one of the supported values.
    """
    provider = settings.embedding_provider

    if provider in ("openai_compat", ""):
        return _make_openai_compat_client(settings)

    raise ValueError(f"Unknown embedding provider {provider!r}. Supported values: openai_compat")


def _make_openai_compat_client(settings: Settings) -> OpenAICompatClient:
    """Create an OpenAI-compatible embedding client."""
    from agentalloy.lm_client import OpenAICompatClient

    logger.debug(
        "embedding provider=openai_compat url=%s model=%s",
        settings.runtime_embed_base_url,
        settings.runtime_embedding_model,
    )
    return OpenAICompatClient(settings.runtime_embed_base_url)
