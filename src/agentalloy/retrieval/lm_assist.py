"""Stage B — LM fragment re-ranker (sub-1B intent layer).

Stage B shows a small instruct-tuned reranker (qwen3-reranker-0.6b) the task
plus the top fused fragments and asks, per fragment, "does this document meet
the query's requirements?" The yes-probability becomes a relevance score; the
pipeline keeps the fragments above a calibrated threshold and drops the rest.
This is the fragment-level arbiter the 2026-06-12 design settled on (see
``docs/lm-assist-design.md`` — "Stage B — fragment re-rank").

Why a sibling module to ``rerank.py`` (Stage A) rather than a new backend:

* Stage A scores *skills* by reordering; its ``Reranker`` protocol returns a
  flat ``list[float]`` from one synchronous call. Stage B scores *fragments*
  concurrently (up to 12 documents under a 300 ms budget — sequential was
  ~470 ms) and needs its own prompt template + softmax-over-logprobs scoring.
* It DOES reuse Stage A's fail-open machinery: ``_FailureLatch`` (the
  process-local circuit breaker) is imported, not re-implemented.

llama.cpp's ``/v1/rerank`` endpoint does NOT work for this GGUF (it skips the
instruction template and returns ~0 for everything), so we score via
``/v1/completions`` with the official Qwen3-Reranker chat template, asking for
one token with ``n_probs`` logprobs, and take ``softmax(yes, no)``.

Fail-open is the contract: every failure path — disabled flag, connection
refused, timeout, malformed logprobs, length mismatch — yields a disabled
result that the caller treats as "Stage B did not run", falling through to the
deterministic selection byte-for-byte. This module never raises to its caller.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from agentalloy.retrieval.rerank import _FailureLatch  # pyright: ignore[reportPrivateUsage]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env-driven). Read once per build; reset_lm_assist_cache() for tests.
# ---------------------------------------------------------------------------

# AgentAlloy's reranker (llama-server) listens on 47952; the old 60001 default
# pointed at an unrelated local service. Stage B is off by default (LM_ASSIST),
# but when enabled it shares the same reranker as the signal intent scorer.
_DEFAULT_URL = "http://127.0.0.1:47952"
_DEFAULT_TIMEOUT_MS = 300
_DEFAULT_KEEP_THRESHOLD = 0.05
_DEFAULT_MODEL = "Qwen3-Reranker-0.6B-Q8_0.gguf"
# Cap on fragments scored per composition — the design's "top ~12 fragments".
_MAX_CANDIDATES = 12

# Official Qwen3-Reranker template. The model was trained to answer "yes"/"no"
# to whether the Document meets the Query's requirements. Verified against the
# live /v1/completions endpoint before being hardcoded (see PR notes).
_SYSTEM = (
    "Judge whether the Document meets the requirements based on the Query and "
    'the Instruct provided. Note that the answer can only be "yes" or "no".'
)
_PREFIX = f"<|im_start|>system\n{_SYSTEM}<|im_end|>\n<|im_start|>user\n"
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_DEFAULT_INSTRUCT = (
    "Given a software engineering task, judge whether the skill instruction "
    "fragment provides guidance the task needs."
)


class LMAssistMode(StrEnum):
    OFF = "off"
    ARBITRATE = "arbitrate"


class LMAssistOutcome(StrEnum):
    """Per-composition Stage B outcome recorded in telemetry."""

    DISABLED = "disabled"
    HIT = "hit"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class LMAssistConfig:
    mode: LMAssistMode
    url: str
    timeout_ms: int
    keep_threshold: float
    model: str
    # Instruct line shown to the reranker. Defaults to the Stage-B fragment
    # instruct; the signal-layer intent classifier overrides it (see
    # ``signals/classifier.py``) so the same FragmentScorer can pair-score
    # utterances against intent task descriptions.
    instruct: str = _DEFAULT_INSTRUCT

    @property
    def enabled(self) -> bool:
        return self.mode is LMAssistMode.ARBITRATE


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def load_config() -> LMAssistConfig:
    """Resolve the Stage B config from the environment.

    Unknown ``LM_ASSIST`` values fall back to ``off`` with one warning — the
    stage must never raise at import or request time.
    """
    raw_mode = os.environ.get("LM_ASSIST", "off").strip().lower()
    try:
        mode = LMAssistMode(raw_mode)
    except ValueError:
        logger.warning("unknown LM_ASSIST=%r; treating as off", raw_mode)
        mode = LMAssistMode.OFF
    return LMAssistConfig(
        mode=mode,
        url=os.environ.get("LM_ASSIST_RERANK_URL", _DEFAULT_URL).strip().rstrip("/")
        or _DEFAULT_URL,
        timeout_ms=_env_int("LM_ASSIST_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS),
        keep_threshold=_env_float("LM_ASSIST_KEEP_THRESHOLD", _DEFAULT_KEEP_THRESHOLD),
        model=os.environ.get("LM_ASSIST_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL,
    )


# ---------------------------------------------------------------------------
# Prompt + scoring math
# ---------------------------------------------------------------------------


def build_prompt(task: str, document: str, *, instruct: str = _DEFAULT_INSTRUCT) -> str:
    """Render the Qwen3-Reranker completion prompt for one (task, document) pair."""
    body = f"<Instruct>: {instruct}\n<Query>: {task}\n<Document>: {document}"
    return f"{_PREFIX}{body}{_SUFFIX}"


def score_from_logprobs(top_logprobs: dict[str, float]) -> float:
    """softmax over the yes/no token logprobs → P(yes) in [0, 1].

    ``top_logprobs`` maps a generated token (already stripped of leading
    whitespace by the caller) to its logprob. We sum the probability mass of
    yes-class and no-class tokens (case-insensitive, tolerating the leading
    space llama.cpp emits) and return yes / (yes + no). A pair with neither
    token present is treated as score 0.0 — the model did not commit to "yes".
    """
    yes_mass = 0.0
    no_mass = 0.0
    for token, logprob in top_logprobs.items():
        norm = token.strip().lower()
        if norm == "yes":
            yes_mass += math.exp(logprob)
        elif norm == "no":
            no_mass += math.exp(logprob)
    total = yes_mass + no_mass
    if total <= 0.0:
        return 0.0
    return yes_mass / total


def _parse_completion_logprobs(data: Any) -> dict[str, float]:
    """Extract the first generated token's {token: logprob} map from a
    llama.cpp /v1/completions response. Raises ValueError on any shape it does
    not recognise — the caller converts that into a fail-open score."""
    if not isinstance(data, dict):
        raise ValueError(f"completion response not an object: {data!r}")
    choices: Any = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"completion response missing choices: {data!r}")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError(f"completion choice not an object: {first!r}")
    logprobs = cast("dict[str, Any]", first).get("logprobs")
    if not isinstance(logprobs, dict):
        raise ValueError(f"completion choice missing logprobs: {first!r}")
    # llama.cpp emits content[0].top_logprobs: [{token, logprob}, ...].
    content = cast("dict[str, Any]", logprobs).get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        top = cast("dict[str, Any]", content[0]).get("top_logprobs")
        if isinstance(top, list):
            out: dict[str, float] = {}
            for entry in cast("list[Any]", top):
                if isinstance(entry, dict):
                    tok = cast("dict[str, Any]", entry).get("token")
                    lp = cast("dict[str, Any]", entry).get("logprob")
                    if isinstance(tok, str) and isinstance(lp, (int, float)):
                        out[tok] = float(lp)
            if out:
                return out
    raise ValueError(f"no top_logprobs in completion response: {logprobs!r}")


# ---------------------------------------------------------------------------
# Scorer client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of scoring a candidate batch. ``scores`` aligns 1:1 with the
    input documents when ``outcome is HIT``; otherwise it is empty and the
    caller falls through to deterministic selection."""

    outcome: LMAssistOutcome
    scores: list[float]


class FragmentScorer:
    """Concurrent pair-scorer over a llama.cpp /v1/completions endpoint.

    One HTTP call per document, fanned out across a thread pool so 12 docs fit
    the 300 ms budget. The per-batch wall-clock is bounded by ``timeout_ms``;
    exceeding it returns a TIMEOUT outcome (fail-open). A process-local failure
    latch (shared philosophy with Stage A) disables the stage for a cooldown
    after repeated failures so a dead backend never adds latency.
    """

    def __init__(self, config: LMAssistConfig) -> None:
        import httpx

        self._config = config
        self._latch = _FailureLatch()
        # Per-request timeout slightly under the batch budget; the batch-level
        # wall-clock guard below is the hard ceiling.
        per_req_s = config.timeout_ms / 1000.0
        self._client = httpx.Client(
            base_url=config.url,
            timeout=httpx.Timeout(per_req_s),
            headers={"Authorization": "Bearer not-needed"},
        )
        self._pool = ThreadPoolExecutor(max_workers=_MAX_CANDIDATES, thread_name_prefix="lm-assist")

    def _score_one(self, task: str, document: str) -> float:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "prompt": build_prompt(task, document, instruct=self._config.instruct),
            "max_tokens": 1,
            "temperature": 0.0,
            "n_probs": 20,
            "logprobs": 20,
        }
        resp = self._client.post("/v1/completions", json=payload)
        resp.raise_for_status()
        top = _parse_completion_logprobs(resp.json())
        return score_from_logprobs(top)

    def score(self, task: str, documents: list[str]) -> ScoreResult:
        """Score every document; never raises. Empty input → HIT with []."""
        if not documents:
            return ScoreResult(LMAssistOutcome.HIT, [])
        if not self._latch.allow():
            return ScoreResult(LMAssistOutcome.DISABLED, [])

        batch_budget_s = self._config.timeout_ms / 1000.0
        # Single deadline for the whole batch — decrement the per-future timeout
        # against it so total wall-clock is bounded by timeout_ms regardless of
        # future ordering (a fixed per-future timeout let a late hang push total
        # toward ~2x the documented ceiling).
        deadline = time.monotonic() + batch_budget_s
        futures = [self._pool.submit(self._score_one, task, doc) for doc in documents]
        scores: list[float] = []
        try:
            for fut in futures:
                scores.append(fut.result(timeout=max(0.0, deadline - time.monotonic())))
        except FuturesTimeout:
            for fut in futures:
                fut.cancel()
            self._latch.record_failure()
            logger.warning("lm-assist Stage B timed out after %d ms", self._config.timeout_ms)
            return ScoreResult(LMAssistOutcome.TIMEOUT, [])
        except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
            for fut in futures:
                fut.cancel()
            self._latch.record_failure()
            logger.warning("lm-assist Stage B scorer failed: %s", exc)
            return ScoreResult(LMAssistOutcome.ERROR, [])

        if len(scores) != len(documents):
            self._latch.record_failure()
            return ScoreResult(LMAssistOutcome.ERROR, [])
        self._latch.record_success()
        return ScoreResult(LMAssistOutcome.HIT, scores)

    def close(self) -> None:
        self._client.close()
        self._pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Factory + process-wide cache (mirrors rerank.py)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached: FragmentScorer | None = None
_cache_built = False


def _build_scorer_from_env() -> FragmentScorer | None:
    """Construct the scorer, or None when Stage B is disabled / mis-configured.
    Never raises — a build failure logs one warning and disables the stage."""
    config = load_config()
    if not config.enabled:
        return None
    try:
        return FragmentScorer(config)
    except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
        logger.warning("lm-assist Stage B disabled — scorer build failed: %s", exc)
        return None


def build_scorer_from_env() -> FragmentScorer | None:
    """Return the process-wide Stage B scorer, building it once. None = disabled."""
    global _cached, _cache_built
    with _cache_lock:
        if not _cache_built:
            _cached = _build_scorer_from_env()
            _cache_built = True
        return _cached


def reset_lm_assist_cache() -> None:
    """Drop the cached scorer so the next call rebuilds from env (tests)."""
    global _cached, _cache_built
    with _cache_lock:
        if _cached is not None:
            with contextlib.suppress(Exception):
                _cached.close()
        _cached = None
        _cache_built = False


def max_candidates() -> int:
    """Cap on fragments sent to the scorer per composition."""
    return _MAX_CANDIDATES
