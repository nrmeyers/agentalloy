"""Hook router — /v1/hook/* endpoints for Claude Code hook scripts.

Provides a synchronous, low-latency signal-layer entry point that hook
scripts call instead of shelling out to the CLI.  Key design points:

- **Signal-first short-circuit**: the handler checks a process-local cache
  first.  If the cached signal result is younger than the stale-while-revalidate
  window (2.5 s by default), the cached value is returned immediately — no
  gate evaluation, no DB lookup.  This keeps per-turn latency at ~50 ms
  (just the HTTP round-trip).

- **Stale-while-revalidate**: when the cache is stale the handler fires the
  full signal pipeline *in the background* and returns the stale value right
  away.  The next request will get the fresh result.

- **2.5 s timeout**: the background revalidation is capped at 2.5 s so a
  slow compose run never blocks the hook script.

The endpoint is intentionally synchronous (no async) because Claude Code
hooks run in a tight per-turn loop and must return within milliseconds.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Cache data structures
# ---------------------------------------------------------------------------

SWR_TIMEOUT_MS = 2500  # stale-while-revalidate window (2.5 seconds)


@dataclass
class _CachedSignalResult:
    """A cached signal evaluation result."""

    composed_block: str
    phase: str | None
    should_compose: bool
    cache_ts: float  # monotonic time when this was cached


# Module-level cache — process-local, thread-safe via a lock. Keyed by
# (cwd, phase): a result computed for one repo/phase must never be served to
# another within the SWR window. Crucially, keying on the *effective* phase
# means a None->intake transition (phase file created after wiring) busts the
# cache instead of serving a stale "should_compose=False".
_CacheKey = tuple[str, str | None]
_cache_lock = threading.Lock()
_cache: dict[_CacheKey, _CachedSignalResult] = {}

# In-flight guard: prevents thundering herd when a cache entry is stale.
# At most one background revalidation per cache key runs at a time; concurrent
# requests for the same key that see a stale entry return the stale value
# without spawning another background thread.
_inflight_guard = threading.Lock()
_inflight: set[_CacheKey] = set()


def _cache_key(cwd: Path, phase: str | None) -> _CacheKey:
    """Canonical cache key: resolved cwd + effective phase."""
    try:
        cwd_key = str(cwd.resolve())
    except OSError:
        cwd_key = str(cwd)
    return (cwd_key, phase)


def _get_cached(key: _CacheKey) -> _CachedSignalResult | None:
    """Return the cache entry for *key* (may be stale)."""
    with _cache_lock:
        return _cache.get(key)


def _set_cached(key: _CacheKey, result: _CachedSignalResult) -> None:
    """Store the cache entry for *key*."""
    with _cache_lock:
        _cache[key] = result


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class HookUserPromptRequest(BaseModel):
    """Payload received from the Claude Code hook script."""

    prompt: str
    phase: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    tool_path: str | None = None


# ---------------------------------------------------------------------------
# Sync signal evaluation (runs in the foreground or background thread)
# ---------------------------------------------------------------------------


def _evaluate_sync(
    prompt: str,
    cwd: Path,
    phase: str | None = None,
) -> dict[str, Any]:
    """Run the full signal pipeline synchronously.

    This is the same logic as the proxy's signal layer but adapted for
    the hook script's simpler input model.
    """
    from agentalloy.signals.skill_loader import (
        _build_predicate_context,
        _load_workflow_skill_for_phase,
        _read_phase,
        _write_phase_atomic,
    )

    current_phase = phase or _read_phase(cwd)
    if current_phase is None:
        return {"composed_block": "", "phase": None, "should_compose": False}

    skill = _load_workflow_skill_for_phase(current_phase, cwd)
    if skill is None:
        return {"composed_block": "", "phase": current_phase, "should_compose": False}

    signal_keywords: list[str] = list(skill.get("signal_keywords") or [])
    gate_spec: dict[str, Any] = skill.get("exit_gates") or {}

    # Extract tool_name from the request body (not the FastAPI Request class).
    # The hook script sends tool_name in the JSON payload for PreToolUse events.
    ctx = _build_predicate_context(
        project_root=cwd,
        phase=current_phase,
        prompt_text=prompt,
        tool_name=None,  # UserPromptSubmit does not include tool_name
    )

    from agentalloy.signals.gates import INTAKE_PHASE
    from agentalloy.signals.prefilter import PreFilterMatch, check_prefilter

    # Intake is the entry phase: it must compose on the very first prompt,
    # before any signal keyword exists. Bypass the keyword/artifact pre-filter
    # so the intent-interview workflow engages immediately; normal gating
    # resumes once intake transitions to spec.
    match: PreFilterMatch | None
    if current_phase == INTAKE_PHASE:
        match = PreFilterMatch(name="intake_entry", detail="intake phase composes unconditionally")
    else:
        match = check_prefilter(signal_keywords, gate_spec, ctx)
    if match is None:
        return {"composed_block": "", "phase": current_phase, "should_compose": False}

    # Pre-filter matched — evaluate gates.
    from agentalloy.signals.gates import decide_transition

    # Try to get an embed client (soft-fail).
    lm_client = None
    try:
        from agentalloy.config import get_settings
        from agentalloy.embed_provider import get_embed_client

        cfg = get_settings()
        lm_client = get_embed_client(cfg)
    except Exception:
        pass

    decision = decide_transition(
        current_phase=current_phase,
        gate_spec=gate_spec,
        ctx=ctx,
        lm_client=lm_client,
    )

    # Phase transition
    if decision.should_transition and decision.to_phase:
        try:
            _write_phase_atomic(cwd, decision.to_phase)
            current_phase = decision.to_phase
        except OSError as e:
            logger.warning("Failed to write phase file: %s", e)

    # Compose the next skill's prose
    next_skill = _load_workflow_skill_for_phase(current_phase, cwd)
    prose = (next_skill or {}).get("raw_prose", "")

    composed_block = ""
    if prose:
        composed_block = f"[agentalloy-workflow]\n{prose}\n[/agentalloy-workflow]"

    return {
        "composed_block": composed_block,
        "phase": current_phase,
        "should_compose": bool(composed_block),
        "transition": decision.should_transition,
        "to_phase": decision.to_phase,
        "gates_met": [g.gate_name for g in decision.gates_met],
        "gates_unmet": [g.gate_name for g in decision.gates_unmet],
    }


# ---------------------------------------------------------------------------
# Background revalidation
# ---------------------------------------------------------------------------


def _revalidate_background(
    prompt: str,
    cwd: Path,
    phase: str | None,
    key: _CacheKey,
) -> None:
    """Run signal evaluation in the background and update the cache for *key*."""
    try:
        result = _evaluate_sync(prompt, cwd, phase)
        block = result.get("composed_block", "")
        _set_cached(
            key,
            _CachedSignalResult(
                composed_block=block,
                phase=result.get("phase"),
                should_compose=result.get("should_compose", False),
                cache_ts=time.monotonic(),
            ),
        )
    except Exception:
        logger.warning("Hook revalidation failed", exc_info=True)
    finally:
        with _inflight_guard:
            _inflight.discard(key)


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------


@router.post("/v1/hook/user-prompt-submit")
async def hook_user_prompt_submit(request: Request) -> JSONResponse:
    """Handle a UserPromptSubmit hook event.

    The Claude Code hook script POSTs JSON to this endpoint.  The endpoint
    uses signal-first caching:
      1. If the cache is fresh (< SWR_TIMEOUT_MS), return immediately.
      2. If stale, start background revalidation and return the stale value.
      3. If no cache exists, run the full pipeline and return.
    """
    start = time.monotonic()

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid JSON body"},
        )

    prompt = body.get("prompt", "")
    phase = body.get("phase")
    cwd_str = body.get("cwd", "")

    # Resolve working directory
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Cache key is (cwd, effective phase). The hook script rarely passes
    # `phase`, so read the phase file: this makes the key change (and the
    # cache bust) the moment a phase file appears or transitions — fixing
    # the stale "should_compose=False" served after wiring.
    from agentalloy.signals.skill_loader import _read_phase  # noqa: PLC0415

    effective_phase = phase or _read_phase(cwd)
    key = _cache_key(cwd, effective_phase)

    # Signal-first cache check (per-key)
    cached = _get_cached(key)
    if cached is not None:
        age_ms = (time.monotonic() - cached.cache_ts) * 1000
        if age_ms < SWR_TIMEOUT_MS:
            # Cache fresh — return immediately (short-circuit)
            latency_ms = int((time.monotonic() - start) * 1000)
            return JSONResponse(
                content={
                    "status": "cached",
                    "composed_block": cached.composed_block,
                    "phase": cached.phase,
                    "should_compose": cached.should_compose,
                    "latency_ms": latency_ms,
                    "cache_hit": True,
                },
            )
        else:
            # Cache stale — start background revalidation for this key.
            # In-flight guard: at most one revalidator per key runs at a
            # time to prevent thundering herd on cache miss.
            with _inflight_guard:
                start_bg = key not in _inflight
                if start_bg:
                    _inflight.add(key)
            if start_bg:
                threading.Thread(
                    target=_revalidate_background,
                    args=(prompt, cwd, phase, key),
                    daemon=True,
                ).start()
            latency_ms = int((time.monotonic() - start) * 1000)
            return JSONResponse(
                content={
                    "status": "stale",
                    "composed_block": cached.composed_block,
                    "phase": cached.phase,
                    "should_compose": cached.should_compose,
                    "latency_ms": latency_ms,
                    "cache_hit": True,
                    "stale": True,
                },
            )

    # No cache — run the full pipeline synchronously
    result = _evaluate_sync(prompt, cwd, phase)
    block = result.get("composed_block", "")

    # Update cache (per-key)
    _set_cached(
        key,
        _CachedSignalResult(
            composed_block=block,
            phase=result.get("phase"),
            should_compose=result.get("should_compose", False),
            cache_ts=time.monotonic(),
        ),
    )

    latency_ms = int((time.monotonic() - start) * 1000)
    return JSONResponse(
        content={
            "status": "fresh",
            "composed_block": block,
            "phase": result.get("phase"),
            "should_compose": result.get("should_compose", False),
            "latency_ms": latency_ms,
            "cache_hit": False,
            **{
                k: v
                for k, v in result.items()
                if k not in ("composed_block", "phase", "should_compose")
            },
        },
    )


@router.post("/v1/hook/pre-tool-use")
async def hook_pre_tool_use(request: Request) -> JSONResponse:
    """Handle a PreToolUse hook event.

    Evaluates system skills for the given tool name and emits matching
    skill prose.  Uses the same signal-first caching as the prompt handler.
    """
    start = time.monotonic()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid JSON body"},
        )

    tool_name = body.get("tool_name", "")
    cwd_str = body.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Check cache (same per-key cache as the prompt handler: keyed on
    # (cwd, effective phase) so recent compose activity for THIS repo/phase
    # short-circuits, never another repo's).
    from agentalloy.signals.skill_loader import _read_phase  # noqa: PLC0415

    cached = _get_cached(_cache_key(cwd, _read_phase(cwd)))
    if cached is not None:
        age_ms = (time.monotonic() - cached.cache_ts) * 1000
        if age_ms < SWR_TIMEOUT_MS:
            latency_ms = int((time.monotonic() - start) * 1000)
            return JSONResponse(
                content={
                    "status": "cached",
                    "system_skills": [],
                    "latency_ms": latency_ms,
                    "cache_hit": True,
                },
            )

    # Evaluate system skills for this tool
    system_skills: list[str] = []
    try:
        from agentalloy.signals.gates import evaluate_node
        from agentalloy.signals.predicates import PredicateResult
        from agentalloy.signals.skill_loader import (
            _build_predicate_context,
            _read_phase,
        )

        current_phase = _read_phase(cwd)
        ctx = _build_predicate_context(
            project_root=cwd,
            phase=current_phase,
            tool_name=tool_name,
        )

        # Query profile skills database
        try:
            import duckdb

            from agentalloy.profiles import detect_profile, profile_datastore_path

            profile = detect_profile(cwd=cwd)
            db_path = profile_datastore_path(profile.name if profile else "default")
            if db_path.exists():
                import yaml as _yaml

                with duckdb.connect(str(db_path), read_only=True) as con:
                    rows = con.execute(
                        "SELECT skill_id, raw_prose, applies_when FROM profile_skills WHERE skill_class = 'system'"
                    ).fetchall()

                for skill_id, raw_prose, applies_when_raw in rows:
                    if not applies_when_raw:
                        continue
                    try:
                        gate_spec = _yaml.safe_load(applies_when_raw) or {}
                    except Exception:
                        continue
                    qwen_calls: list[int] = [0]
                    result, _ = evaluate_node(gate_spec, ctx, None, qwen_calls)
                    if result == PredicateResult.MET:
                        system_skills.append(
                            f"[agentalloy-system:{skill_id}]\n{raw_prose}\n[/agentalloy-system]"
                        )
        except Exception:
            pass

    except Exception:
        logger.warning("Hook pre-tool-use evaluation failed", exc_info=True)

    latency_ms = int((time.monotonic() - start) * 1000)
    return JSONResponse(
        content={
            "status": "fresh",
            "system_skills": system_skills,
            "latency_ms": latency_ms,
            "cache_hit": False,
        },
    )


@router.post("/v1/hook/post-tool-use")
async def hook_post_tool_use(request: Request) -> JSONResponse:
    """Handle a PostToolUse hook event.

    Validates contracts and triggers compose when relevant files are modified.
    """
    start = time.monotonic()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid JSON body"},
        )

    tool_name = body.get("tool_name", "")
    tool_path = body.get("tool_path", "")
    cwd_str = body.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Only fire on writes inside .agentalloy/contracts/
    if tool_name in ("Edit", "Write", "MultiEdit") and ".agentalloy/contracts/" in tool_path:
        try:
            from agentalloy.contracts import parse_contract, validate_contract

            contract = parse_contract(Path(tool_path))
            issues = validate_contract(contract, cwd)
            if not issues:
                return JSONResponse(
                    content={
                        "status": "contract_valid",
                        "contract_phase": contract.phase,
                        "latency_ms": int((time.monotonic() - start) * 1000),
                    },
                )
        except Exception:
            pass

    latency_ms = int((time.monotonic() - start) * 1000)
    return JSONResponse(
        content={
            "status": "no_action",
            "latency_ms": latency_ms,
        },
    )


@router.get("/v1/hook/cache-status")
async def hook_cache_status() -> JSONResponse:
    """Return the current cache state for diagnostics.

    The cache is now keyed by (cwd, phase), so report the entry count plus
    the freshest entry's age (the SWR window is shared across keys).
    """
    with _cache_lock:
        entries = list(_cache.values())
    if not entries:
        return JSONResponse(
            content={
                "cache_enabled": False,
                "entries": 0,
                "cached_at": None,
                "age_ms": None,
            },
        )
    freshest = max(entries, key=lambda c: c.cache_ts)
    age_ms = (time.monotonic() - freshest.cache_ts) * 1000
    return JSONResponse(
        content={
            "cache_enabled": True,
            "entries": len(entries),
            "cached_at": freshest.cache_ts,
            "age_ms": age_ms,
            "stale": age_ms >= SWR_TIMEOUT_MS,
            "phase": freshest.phase,
            "should_compose": freshest.should_compose,
        },
    )
