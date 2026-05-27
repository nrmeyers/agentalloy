"""Embedding error taxonomy and circuit-breaker for the retrieval pipeline.

Sprint 1 — embedding error handling:
- EmbeddingErrorCode enum for structured error classification
- EmbeddingError exception with code + original error
- EmbeddingErrorResult sentinel for graceful degradation (BM25-only fallback)
- CircuitBreaker to avoid hammering a failing embedding service
- safe_embed() helper function that wraps embedding calls with the breaker

Per v5.4: embedding failures must NOT crash the compose pipeline.
When the embedding service is unavailable, fall back to BM25-only retrieval.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentalloy.lm_client import OpenAICompatClient
    from agentalloy.reads.models import ActiveFragment

logger = logging.getLogger(__name__)


def _empty_candidates() -> list[ActiveFragment]:
    return []


def _empty_scores_by_id() -> dict[str, float]:
    return {}


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class EmbeddingErrorCode(enum.Enum):
    """Structured error codes for embedding failures.

    Each code maps to a specific error_code string used in telemetry traces
    and the compose API error response.
    """

    # Client-side errors — the embedding call was never made or was invalid
    INVALID_MODEL = "embedding_model_invalid"
    EMPTY_INPUT = "embedding_empty_input"

    # Transport errors — embedding service unreachable
    UNAVAILABLE = "embedding_unavailable"
    TIMEOUT = "embedding_timeout"

    # Server-side errors — service returned an error
    BAD_RESPONSE = "embedding_bad_response"
    MODEL_NOT_LOADED = "embedding_model_not_loaded"

    # Circuit-breaker state
    CIRCUIT_OPEN = "embedding_circuit_open"

    @property
    def error_code(self) -> str:
        """Return the error_code string for telemetry / API responses."""
        return self.value


class EmbeddingError(Exception):
    """Raised when an embedding call fails.

    Wraps the original exception (if any) so callers can inspect it,
    while the code field drives structured handling (fallback, telemetry, etc.).
    """

    def __init__(
        self,
        code: EmbeddingErrorCode,
        message: str | None = None,
        original: Exception | None = None,
        **extra: Any,
    ) -> None:
        self.code = code
        self.message = message or code.value
        self.original = original
        self.extra = extra  # arbitrary metadata (model, retry_count, etc.)
        super().__init__(f"[{code.value}] {self.message}")


# ---------------------------------------------------------------------------
# Sentinel result for graceful degradation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingErrorResult:
    """Sentinel returned from retrieve_domain_candidates() when embedding fails.

    Signals to the caller that the vector (dense) leg failed and only BM25
    results are available. The compose pipeline should treat this as a
    partial result, not a hard failure.

    Attributes:
        error: The EmbeddingError that caused this fallback.
        bm25_only: True when the retrieval path fell back to lexical-only search.
        candidates: Hydrated fallback candidates, if any.
    """

    error: EmbeddingError
    bm25_only: bool = False
    candidates: list[ActiveFragment] = field(default_factory=_empty_candidates)
    eligible_count: int = 0
    retrieval_ms: int = 0
    scores_by_id: dict[str, float] = field(default_factory=_empty_scores_by_id)
    bm25_source: str = "rule-extracted"

    @property
    def error_code(self) -> str:
        return self.error.code.value


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitState:
    """Immutable snapshot of the circuit-breaker state."""

    state: str  # "closed" | "open" | "half_open"
    failure_count: int
    last_failure_ts: float | None = None
    opened_at: float | None = None  # when the circuit opened


class CircuitBreaker:
    """Simple circuit breaker for the embedding service.

    States:
    - closed: normal operation. Failures increment counter.
    - open: all calls fail immediately. After timeout, transitions to half_open.
    - half_open: one probe call allowed. Success → closed; failure → open again.

    Thresholds (tunable via constructor):
    - failure_threshold: number of failures before opening circuit
    - recovery_timeout: seconds to wait before trying half_open
    - success_threshold: consecutive successes in half_open before closing

    Per v5.4: when the circuit is open, retrieve_domain_candidates() should
    skip the embedding call entirely and return a BM25-only result.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._success_threshold = success_threshold

        self._lock = threading.Lock()
        self._state = "closed"
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_ts: float | None = None
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def _maybe_transition_to_half_open_locked(self) -> None:
        # Caller must hold self._lock.
        if (
            self._state == "open"
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._recovery_timeout
        ):
            self._state = "half_open"
            self._probe_in_flight = False
            self._success_count = 0

    @property
    def state(self) -> str:
        """Current circuit state, with automatic half_open transition check."""
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return self._state

    @property
    def is_open(self) -> bool:
        """True if the circuit is open or half_open (calls should be blocked or probed)."""
        with self._lock:
            return self._state in ("open", "half_open")

    def get_state(self) -> CircuitState:
        """Return an immutable snapshot of the current state."""
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return CircuitState(
                state=self._state,
                failure_count=self._failure_count,
                last_failure_ts=self._last_failure_ts,
                opened_at=self._opened_at,
            )

    def record_success(self) -> None:
        """Record a successful embedding call."""
        with self._lock:
            if self._state == "half_open":
                self._success_count += 1
                self._probe_in_flight = False
                if self._success_count >= self._success_threshold:
                    self._state = "closed"
                    self._failure_count = 0
                    self._success_count = 0
                    self._opened_at = None
                    logger.info(
                        "embedding circuit breaker: closed after %d successes",
                        self._success_threshold,
                    )
            elif self._state == "closed":
                self._failure_count = 0
                self._success_count = 0

    def record_failure(self) -> None:
        """Record a failed embedding call."""
        with self._lock:
            self._last_failure_ts = time.monotonic()
            if self._state == "half_open":
                # Probe failed — go back to open
                self._state = "open"
                self._opened_at = time.monotonic()
                self._success_count = 0
                self._failure_count += 1
                self._probe_in_flight = False
                logger.warning("embedding circuit breaker: half_open probe failed, reopening")
            elif self._state == "closed":
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._state = "open"
                    self._opened_at = time.monotonic()
                    logger.warning(
                        "embedding circuit breaker: opened after %d failures",
                        self._failure_count,
                    )

    def allow_request(self) -> bool:
        """Check if a call is allowed through, reserving the half_open probe slot.

        Returns True if the circuit is closed, or if it's the single allowed
        half_open probe (subsequent concurrent callers are blocked until
        record_success() / record_failure() releases the slot).
        Returns False if the circuit is open and recovery timeout hasn't elapsed.
        """
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            if self._state == "closed":
                return True
            if self._state == "half_open":
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return False

    def reset(self) -> None:
        """Reset the circuit breaker to initial closed state."""
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_ts = None
            self._opened_at = None
            self._probe_in_flight = False
            logger.info("embedding circuit breaker: reset")


# Global circuit breaker instance (module-level singleton)
embedding_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    success_threshold=2,
)


# ---------------------------------------------------------------------------
# safe_embed() — context manager for embedding calls
# ---------------------------------------------------------------------------


@dataclass
class _EmbedContext:
    """Internal context for safe_embed()."""

    model: str
    client: OpenAICompatClient
    texts: list[str]
    call_count: int = 0
    last_error: Exception | None = None


def safe_embed(
    client: OpenAICompatClient,
    model: str,
    texts: list[str],
) -> list[list[float]]:
    """Safely call embed(), applying circuit-breaker logic.

    This is the primary entry point for embedding calls throughout the
    retrieval pipeline. It:
    1. Checks the circuit breaker before calling
    2. Catches all exceptions from the embedding call
    3. Records success/failure in the circuit breaker
    4. Raises EmbeddingError with the appropriate code

    Args:
        client: The LM client (OpenAICompatClient)
        model: The embedding model name
        texts: List of text strings to embed

    Returns:
        List of embedding vectors (one per input text)

    Raises:
        EmbeddingError: If the embedding call fails or circuit is open
    """
    # Check circuit breaker
    if embedding_breaker.is_open and not embedding_breaker.allow_request():
        raise EmbeddingError(
            EmbeddingErrorCode.CIRCUIT_OPEN,
            f"circuit breaker is open (state={embedding_breaker.state})",
        )

    ctx = _EmbedContext(model=model, client=client, texts=texts)

    try:
        result = client.embed(model=model, texts=texts)
        ctx.call_count += 1
        embedding_breaker.record_success()
        return result
    except Exception as exc:
        ctx.last_error = exc
        # Map the exception to an EmbeddingErrorCode
        code = _classify_exception(exc)
        embedding_breaker.record_failure()
        raise EmbeddingError(
            code,
            message=str(exc),
            original=exc,
            model=model,
            call_count=ctx.call_count,
        ) from exc


def _classify_exception(exc: Exception) -> EmbeddingErrorCode:
    """Classify a raw exception into an EmbeddingErrorCode.

    Uses isinstance checks and error message patterns to determine
    the appropriate error code.
    """
    from agentalloy.lm_client import (
        LMBadResponse,
        LMClientError,
        LMModelNotLoaded,
        LMTimeout,
        LMUnavailable,
    )

    if isinstance(exc, LMModelNotLoaded):
        return EmbeddingErrorCode.MODEL_NOT_LOADED
    if isinstance(exc, LMTimeout):
        return EmbeddingErrorCode.TIMEOUT
    if isinstance(exc, LMUnavailable):
        return EmbeddingErrorCode.UNAVAILABLE
    if isinstance(exc, LMBadResponse):
        return EmbeddingErrorCode.BAD_RESPONSE
    if isinstance(exc, LMClientError):
        return EmbeddingErrorCode.UNAVAILABLE

    # Unknown exception type — treat as unavailable
    return EmbeddingErrorCode.UNAVAILABLE


# ---------------------------------------------------------------------------
# Re-export for convenience
# ---------------------------------------------------------------------------

__all__ = [
    "EmbeddingErrorCode",
    "EmbeddingError",
    "EmbeddingErrorResult",
    "CircuitBreaker",
    "CircuitState",
    "embedding_breaker",
    "safe_embed",
]
