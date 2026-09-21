"""Local agent mode (v2) — env-driven stage configuration.

Follows the LM_ASSIST pattern (``retrieval/lm_assist.py``): bare env names,
defaults pinned in code, invalid values fall back with one warning (this
module must never raise at import or request time). The process-wide config
is cached on first read; tests call :func:`reset_local_agent_cache` after
mutating the environment.

Temperature is deliberately NOT an env knob: it is fixed at 0 in code.
Determinism is a property of the protocol, not a setting
(see ``docs/local-agent-design.md``).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)

# llama-server + DSpark compressor sidecar. Note: :50001 is also where some
# deployments run the fastModel/compactionModel; sharing works only when the
# model tag matches — the failure cooldown latch (client stage) protects
# against the other being up instead.
_DEFAULT_URL = "http://127.0.0.1:50001"
# Pinned model tag — recorded in every trace for provenance (reranker-tag style).
_DEFAULT_MODEL = "minicpm5-2b"
# Per-stage timeout. The spike's 300 s was a cold-server wall number; warm
# stages are seconds. The "thinks before answering" reasoning budget is inside
# max_tokens, not the timeout.
_DEFAULT_TIMEOUT_MS = 30000
_DEFAULT_MAX_STEPS = 2
# Hard cap — the loop must terminate; 3 leaves room for a classify/fill retry
# without turning one ask into a minute-long request on a warm server.
_MAX_STEPS_HARD_CAP = 3
# Spike finding: 512 is exhausted by internal reasoning on abstention cases
# (the model "thinks" its way to an empty completion).
_DEFAULT_MAX_TOKENS = 2048
# Per-result transcript cap (mirrors LM_ASSIST_DOC_CAP_CHARS): the cap, not the
# serving compressor, is what keeps multi-step requests inside the window.
_DEFAULT_RESULT_CAP_CHARS = 2400


class LocalAgentMode(StrEnum):
    OFF = "off"
    ON = "on"


@dataclass(frozen=True)
class LocalAgentConfig:
    mode: LocalAgentMode
    url: str
    model: str
    timeout_ms: int
    max_steps: int
    max_tokens: int
    result_cap_chars: int
    # Fixed in code, never read from env — see module docstring. Exposed here
    # so the loop has a single place to read it from.
    temperature: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.mode is LocalAgentMode.ON


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default


def load_config() -> LocalAgentConfig:
    """Resolve the local-agent config from the environment.

    Unknown ``LOCAL_AGENT`` values fall back to ``off`` with one warning.
    Numeric knobs that land outside their sane bounds are clamped with one
    warning each — the module degrades, it never raises.
    """
    raw_mode = os.environ.get("LOCAL_AGENT", "off").strip().lower()
    try:
        mode = LocalAgentMode(raw_mode)
    except ValueError:
        logger.warning("unknown LOCAL_AGENT=%r; treating as off", raw_mode)
        mode = LocalAgentMode.OFF

    max_steps = _env_int("LOCAL_AGENT_MAX_STEPS", _DEFAULT_MAX_STEPS)
    if max_steps > _MAX_STEPS_HARD_CAP:
        logger.warning(
            "LOCAL_AGENT_MAX_STEPS=%d exceeds the hard cap %d; using %d",
            max_steps,
            _MAX_STEPS_HARD_CAP,
            _MAX_STEPS_HARD_CAP,
        )
        max_steps = _MAX_STEPS_HARD_CAP
    elif max_steps < 1:
        logger.warning("LOCAL_AGENT_MAX_STEPS=%d is below 1; using 1", max_steps)
        max_steps = 1

    timeout_ms = _env_int("LOCAL_AGENT_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS)
    if timeout_ms <= 0:
        logger.warning(
            "LOCAL_AGENT_TIMEOUT_MS=%d is not positive; using %d",
            timeout_ms,
            _DEFAULT_TIMEOUT_MS,
        )
        timeout_ms = _DEFAULT_TIMEOUT_MS

    max_tokens = _env_int("LOCAL_AGENT_MAX_TOKENS", _DEFAULT_MAX_TOKENS)
    if max_tokens <= 0:
        logger.warning(
            "LOCAL_AGENT_MAX_TOKENS=%d is not positive; using %d",
            max_tokens,
            _DEFAULT_MAX_TOKENS,
        )
        max_tokens = _DEFAULT_MAX_TOKENS

    result_cap_chars = _env_int("LOCAL_AGENT_RESULT_CAP_CHARS", _DEFAULT_RESULT_CAP_CHARS)
    if result_cap_chars <= 0:
        logger.warning(
            "LOCAL_AGENT_RESULT_CAP_CHARS=%d is not positive; using %d",
            result_cap_chars,
            _DEFAULT_RESULT_CAP_CHARS,
        )
        result_cap_chars = _DEFAULT_RESULT_CAP_CHARS

    return LocalAgentConfig(
        mode=mode,
        url=os.environ.get("LOCAL_AGENT_URL", _DEFAULT_URL).strip().rstrip("/") or _DEFAULT_URL,
        model=os.environ.get("LOCAL_AGENT_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL,
        timeout_ms=timeout_ms,
        max_steps=max_steps,
        max_tokens=max_tokens,
        result_cap_chars=result_cap_chars,
    )


# ---------------------------------------------------------------------------
# Process-wide cache (mirrors lm_assist's scorer cache): read env once, reset
# for tests.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached: LocalAgentConfig | None = None


def get_config() -> LocalAgentConfig:
    """Return the cached config, building it from env on first read."""
    global _cached
    with _cache_lock:
        if _cached is None:
            _cached = load_config()
        return _cached


def reset_local_agent_cache() -> None:
    """Drop the cached config so the next read rebuilds from env (tests)."""
    global _cached
    with _cache_lock:
        _cached = None
