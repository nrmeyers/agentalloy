"""Cross-encoder rerank stage for the domain retrieval pipeline (v5 — Stage A).

A pluggable scorer reorders the top candidate skills by a (query, passage)
relevance score. Two backends, selected via ``RUNTIME_RERANK_MODE``:

* ``onnx`` — a tiny cross-encoder (ms-marco MiniLM class) run in-process via
  onnxruntime + tokenizers. No sidecar; identical behavior native/container.
* ``http`` — POSTs to a llama-server ``/v1/rerank`` endpoint. The quality
  escalation path; requires a sidecar.

Reranking is strictly best-effort: every failure degrades to the un-reranked
order. A process-local failure latch (mirroring ``embedding_errors``'
circuit-breaker philosophy) disables the stage for a cooldown after repeated
failures so a dead backend never adds per-request latency. onnxruntime and
tokenizers are optional dependencies (the ``rerank`` extra) imported lazily.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Protocol, cast, runtime_checkable

logger = logging.getLogger(__name__)

# Token cap per (query, passage) pair fed to the cross-encoder. 512 is the
# context window of the MiniLM-class models and the conventional rerank cap.
_MAX_TOKENS = 512

# Failure latch tuning — disable reranking for a cooldown after repeated
# failures, then allow one retry. Process-local, never persisted.
_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 60.0
# Long-open escalation cap. A permanently-dead backend that re-fails its retry
# every cooldown would otherwise be probed forever on a fixed 60s cadence (one
# guaranteed timeout per minute, every minute). Each successive re-open doubles
# the cooldown up to this cap, so a dead reranker quiesces instead of thrashing.
_MAX_COOLDOWN_SECONDS = 600.0

# 600ms budget before the rerank stage trips its circuit-breaker and falls
# through to the deterministic order. Raised from 150ms: a cold/loaded CPU
# reranker (llama-server) routinely crossed it, disabling the stage.
# Override with RUNTIME_RERANK_TIMEOUT_MS.
_DEFAULT_TIMEOUT_MS = 600
_DEFAULT_MAX_PAIRS = 32


@runtime_checkable
class Reranker(Protocol):
    """Scores (query, passage) pairs. Sync — the pipeline runs in a thread."""

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class _FailureLatch:
    """Process-local latch: open after N consecutive failures, auto-reset after a cooldown.

    Mirrors the embedding circuit-breaker philosophy but minimal — reranking is
    never on the critical path, so a half-open probe state is unnecessary; the
    cooldown simply elapses and the next call is allowed through.

    Long-open escalation: a backend that keeps re-failing its post-cooldown retry
    is permanently dead, so each successive re-open doubles the cooldown (capped at
    ``_MAX_COOLDOWN_SECONDS``) instead of probing it on a fixed cadence forever. The
    first open keeps the base cooldown, so single-blip recovery is unchanged. A
    ``record_success`` resets the escalation. Shared by Stage A, Stage B, and the
    intent scorer — a dead reranker long-opens everywhere, which is intended.
    """

    def __init__(self, threshold: int = _FAILURE_THRESHOLD, cooldown: float = _COOLDOWN_SECONDS):
        self._threshold = threshold
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        # Number of open episodes since the last success (drives the backoff).
        self._open_cycles = 0
        # Cooldown that applies to the CURRENT open episode (escalates per cycle).
        self._current_cooldown = cooldown

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self._current_cooldown:
                # Cooldown elapsed — allow one retry and reset the counter. The
                # open-cycle count is preserved so a re-failure escalates further.
                self._opened_at = None
                self._failures = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._open_cycles = 0
            self._current_cooldown = self._cooldown

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold and self._opened_at is None:
                # Escalate: first open uses the base cooldown (cycle 0), each
                # subsequent re-open doubles it up to the cap.
                self._current_cooldown = min(
                    self._cooldown * (2**self._open_cycles), _MAX_COOLDOWN_SECONDS
                )
                self._open_cycles += 1
                self._opened_at = time.monotonic()
                logger.warning(
                    "rerank disabled for %.0fs after %d consecutive failures (open cycle %d)",
                    self._current_cooldown,
                    self._failures,
                    self._open_cycles,
                )


class _LatchedReranker:
    """Wraps a scorer with the failure latch. Returns [] when the latch is open
    or the scorer fails — the caller treats an empty score list as "no rerank"."""

    def __init__(self, inner: Reranker) -> None:
        self._inner = inner
        self._latch = _FailureLatch()

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        if not self._latch.allow():
            return []
        try:
            scores = self._inner.score(query, passages)
        except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
            self._latch.record_failure()
            logger.warning("rerank scorer failed: %s", exc)
            return []
        if len(scores) != len(passages):
            self._latch.record_failure()
            logger.warning(
                "rerank scorer returned %d scores for %d passages; ignoring",
                len(scores),
                len(passages),
            )
            return []
        self._latch.record_success()
        return scores


class OnnxReranker:
    """In-process cross-encoder via onnxruntime + tokenizers.

    Loads ``model.onnx`` + ``tokenizer.json`` from ``model_dir`` (the
    Xenova/cross-encoder ms-marco-MiniLM-L-6-v2 ONNX export layout). Encodes
    (query, passage) pairs, truncates to 512 tokens, runs one batched session
    call. onnxruntime/tokenizers are imported lazily on first ``score``.
    """

    def __init__(self, model_dir: str) -> None:
        self._model_dir = model_dir
        self._session: Any = None
        self._tokenizer: Any = None

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime  # type: ignore[import-untyped]
            from tokenizers import Tokenizer  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "OnnxReranker requires the optional 'rerank' dependencies. "
                "Install them with: uv pip install 'agentalloy[rerank]' "
                "(provides onnxruntime + tokenizers)."
            ) from exc

        model_path = os.path.join(self._model_dir, "model.onnx")
        tokenizer_path = os.path.join(self._model_dir, "tokenizer.json")
        tokenizer = Tokenizer.from_file(tokenizer_path)
        tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        tokenizer.enable_padding()
        self._tokenizer = tokenizer
        self._session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        self._ensure_loaded()
        tokenizer = self._tokenizer
        session = self._session

        encodings = tokenizer.encode_batch([(query, p) for p in passages])
        input_ids = [list(e.ids) for e in encodings]
        attention_mask = [list(e.attention_mask) for e in encodings]
        feed: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        input_names = {i.name for i in session.get_inputs()}
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = [list(e.type_ids) for e in encodings]
        # Drop any feed key the model does not declare (export variance).
        feed = {k: v for k, v in feed.items() if k in input_names}

        outputs = session.run(None, feed)
        logits = outputs[0]
        # Classification head shape is (batch, 1) or (batch, 2). A single-logit
        # head is the relevance score directly; a 2-logit head uses the
        # positive class. Read element-wise to avoid a hard numpy dependency.
        scores: list[float] = []
        for row in logits:
            values = list(row)
            scores.append(float(values[-1]) if len(values) > 1 else float(values[0]))
        return scores


class HttpReranker:
    """POSTs to a llama-server rerank endpoint (``{base_url}/v1/rerank``).

    Request: ``{"model": ..., "query": ..., "documents": [...]}``.
    Response: ``{"results": [{"index": i, "relevance_score": s}, ...]}``.
    Uses the same httpx client the embed/LM client uses, with a tight timeout
    so a slow sidecar degrades to the un-reranked order quickly.
    """

    def __init__(self, base_url: str, model: str, *, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_ms / 1000.0),
            headers={"Authorization": "Bearer not-needed"},
        )

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        payload: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": passages,
        }
        resp = self._client.post("/v1/rerank", json=payload)
        resp.raise_for_status()
        data: Any = resp.json()
        results: Any = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ValueError(f"rerank response missing 'results' list: {data!r}")
        scores = [0.0] * len(passages)
        for item in cast(list[Any], results):
            if not isinstance(item, dict):
                raise ValueError(f"rerank result is not a mapping: {item!r}")
            item_dict = cast(dict[str, Any], item)
            idx = item_dict.get("index")
            raw = item_dict.get("relevance_score")
            if not isinstance(idx, int) or not isinstance(raw, (int, float)):
                raise ValueError(f"rerank result malformed: {item!r}")
            if 0 <= idx < len(scores):
                scores[idx] = float(raw)
        return scores

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Factory + process-wide cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached: Reranker | None = None
_cache_built = False


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def _build_reranker_from_env() -> Reranker | None:
    """Construct the configured reranker, or None when reranking is disabled.

    Invalid/incomplete config logs one warning and returns None — the stage
    must never raise at import or startup.
    """
    mode = os.environ.get("RUNTIME_RERANK_MODE", "off").strip().lower()
    if mode in ("", "off"):
        return None

    timeout_ms = _env_int("RUNTIME_RERANK_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS)

    if mode == "onnx":
        model_dir = os.environ.get("RUNTIME_RERANK_ONNX_DIR", "").strip()
        if not model_dir or not os.path.isdir(model_dir):
            logger.warning(
                "RUNTIME_RERANK_MODE=onnx but RUNTIME_RERANK_ONNX_DIR=%r is not a directory; "
                "rerank disabled",
                model_dir,
            )
            return None
        return _LatchedReranker(OnnxReranker(model_dir))

    if mode == "http":
        base_url = os.environ.get("RUNTIME_RERANK_BASE_URL", "").strip()
        model = os.environ.get("RUNTIME_RERANK_MODEL", "").strip()
        if not base_url or not model:
            logger.warning(
                "RUNTIME_RERANK_MODE=http requires RUNTIME_RERANK_BASE_URL and "
                "RUNTIME_RERANK_MODEL; rerank disabled",
            )
            return None
        return _LatchedReranker(HttpReranker(base_url, model, timeout_ms=timeout_ms))

    logger.warning("unknown RUNTIME_RERANK_MODE=%r; rerank disabled", mode)
    return None


def build_reranker_from_env() -> Reranker | None:
    """Return the process-wide reranker, building it once. None = stage disabled."""
    global _cached, _cache_built
    with _cache_lock:
        if not _cache_built:
            _cached = _build_reranker_from_env()
            _cache_built = True
        return _cached


def reset_reranker_cache() -> None:
    """Drop the cached reranker so the next call rebuilds from env (tests)."""
    global _cached, _cache_built
    with _cache_lock:
        _cached = None
        _cache_built = False


def rerank_max_pairs() -> int:
    """Cap on the number of skills (one passage each) sent to the scorer."""
    return _env_int("RUNTIME_RERANK_MAX_PAIRS", _DEFAULT_MAX_PAIRS)
