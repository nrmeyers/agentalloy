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

from fastapi import APIRouter, HTTPException, Request
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
        _intake_route_hint,
        _load_workflow_skill_for_phase,
        _read_lifecycle_mode,
        _read_phase,
        _write_phase_atomic,
    )

    # Per-repo lifecycle mode gates the workflow-scaffold path. Only `full`
    # composes/advances phases; `assist` and `off` both skip it (a deferring
    # repo drives its own workflow — the additive system/domain skill injection
    # in the pre/post-tool-use hooks is what `assist` keeps).
    mode = _read_lifecycle_mode(cwd)
    current_phase = phase or _read_phase(cwd)
    if mode != "full" or current_phase is None:
        return {"composed_block": "", "phase": current_phase, "should_compose": False}

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

    from agentalloy.signals.classifier import check_transition_trigger
    from agentalloy.signals.gates import INTAKE_PHASE
    from agentalloy.signals.prefilter import PreFilterMatch

    # Embed/reranker client (soft-fail). Built before the trigger because the
    # reranker-primary trigger uses it for semantic intent scoring.
    lm_client = None
    try:
        from agentalloy.config import get_settings
        from agentalloy.embed_provider import get_embed_client

        cfg = get_settings()
        lm_client = get_embed_client(cfg)
    except Exception:
        pass

    # Intake is the entry phase: it must compose on the very first prompt,
    # before any signal keyword exists. Bypass the trigger so the intent-interview
    # workflow engages immediately; normal gating resumes once intake -> spec.
    match: PreFilterMatch | None
    if current_phase == INTAKE_PHASE:
        match = PreFilterMatch(name="intake_entry", detail="intake phase composes unconditionally")
    else:
        match = check_transition_trigger(signal_keywords, gate_spec, ctx, lm_client)
    if match is None:
        return {"composed_block": "", "phase": current_phase, "should_compose": False}

    # Trigger matched — evaluate gates.
    from agentalloy.signals.gates import decide_transition

    # Leaving intake branches the graph on the contract's route: fast → sdd-fast,
    # else the linear intake → spec. Only intake reads a route hint.
    route_hint = _intake_route_hint(cwd) if current_phase == INTAKE_PHASE else None
    decision = decide_transition(
        current_phase=current_phase,
        gate_spec=gate_spec,
        ctx=ctx,
        lm_client=lm_client,
        next_phase_hint=route_hint,
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

    blocks: list[str] = []
    if prose:
        blocks.append(f"[agentalloy-workflow]\n{prose}\n[/agentalloy-workflow]")
    # Surface gate advisories (e.g. "intent fired but the exit artifact is
    # missing") so the agent knows what to produce to advance.
    if decision.advisories:
        advisory_text = "\n".join(decision.advisories)
        blocks.append(f"[agentalloy-eval]\n{advisory_text}\n[/agentalloy-eval]")

    composed_block = "\n\n".join(blocks)

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

    cwd_str = body.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Per-repo lifecycle: `off` mutes all injection; `assist`/`full` keep the
    # additive system-skill path (this is the value a deferring repo retains).
    from agentalloy.signals.skill_loader import _read_lifecycle_mode  # noqa: PLC0415

    if _read_lifecycle_mode(cwd) == "off":
        return JSONResponse(
            content={"status": "disabled", "system_skills": [], "cache_hit": False},
        )

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

    # Evaluate applicable system skills for the current phase.
    system_skills: list[str] = []
    try:
        from agentalloy.signals.skill_loader import _read_phase

        current_phase = _read_phase(cwd)

        # Retrieve applicable system skills from the corpus via the canonical
        # scope-based applicability model (always_apply / phase_scope /
        # category_scope) — the same path the /compose route uses. The previous
        # profile_skills `applies_when` gate was never populated for shipped
        # system skills (the scope fields were dropped on profile ingest), so
        # system skills silently never injected through the hook.
        store = _get_skill_store(request)
        if store is not None:
            from agentalloy.reads.models import ActiveFragment
            from agentalloy.retrieval.system import retrieve_system_fragments

            result = retrieve_system_fragments(store, phase=current_phase, category=None)
            by_skill: dict[str, list[ActiveFragment]] = {}
            for frag in result.candidates:
                by_skill.setdefault(frag.skill_id, []).append(frag)
            for skill_id in result.applied_skill_ids:
                frags = sorted(by_skill.get(skill_id, []), key=lambda f: f.sequence)
                prose = "\n".join(f.content for f in frags)
                if prose:
                    system_skills.append(
                        f"[agentalloy-system:{skill_id}]\n{prose}\n[/agentalloy-system]"
                    )
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


def _get_compose_orchestrator(request: Request) -> Any:
    """Return the app's compose orchestrator instance, or None if unavailable.

    Reuses the same orchestrator the ``/compose`` route uses (built in the
    app.py lifespan and registered via ``dependency_overrides``), so its
    telemetry writer records the composition trace automatically. Returns None
    when the runtime didn't load — the caller fails open.
    """
    from agentalloy.api.compose_router import get_orchestrator

    override = request.app.dependency_overrides.get(get_orchestrator)
    if override is None:
        return None
    try:
        return override()
    except Exception:
        return None


def _get_skill_store(request: Request) -> Any:
    """Return the app's LadybugStore (registered via ``dependency_overrides``), or None.

    Same store the inspection/compose routes use, so PreToolUse selects system
    skills from the live corpus via the canonical scope-based applicability.
    Returns None when the runtime didn't load — the caller fails open.
    """
    from agentalloy.api.skill_router import get_skill_store

    override = request.app.dependency_overrides.get(get_skill_store)
    if override is None:
        return None
    try:
        return override()
    except Exception:
        return None


@router.post("/v1/hook/post-tool-use")
async def hook_post_tool_use(request: Request) -> JSONResponse:
    """Handle a PostToolUse hook event.

    When the agent writes a contract, compose the domain skill fragments that
    match the contract's ``domain_tags`` and return them as ``composed_block``,
    so the hook script can inject them into Claude (PostToolUse additionalContext).
    This is the contract -> domain-skill bridge: the workflow scaffold arrives on
    the prompt, the matching domain skills arrive once the contract declares its
    scope. Telemetry is recorded automatically by the orchestrator.

    Fail-open everywhere: any error, an unsafe/invalid contract, or a missing
    runtime returns a no-compose response — the hook never blocks the agent.
    """
    start = time.monotonic()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    def _done(content: dict[str, Any]) -> JSONResponse:
        content.setdefault("latency_ms", int((time.monotonic() - start) * 1000))
        return JSONResponse(content=content)

    tool_name = body.get("tool_name", "")
    # Claude Code nests the edited path under tool_input.file_path; older/test
    # payloads use a flat tool_path. Accept both.
    tool_input = body.get("tool_input")
    nested_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    tool_path = body.get("tool_path") or nested_path or ""
    cwd_str = body.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    # Per-repo lifecycle: `off` mutes domain injection; `assist`/`full` keep it.
    from agentalloy.signals.skill_loader import _read_lifecycle_mode  # noqa: PLC0415

    if _read_lifecycle_mode(cwd) == "off":
        return _done({"status": "no_action"})

    # Only act on writes to a contract file.
    if tool_name not in ("Edit", "Write", "MultiEdit") or ".agentalloy/contracts/" not in tool_path:
        return _done({"status": "no_action"})

    tp = Path(tool_path)
    contract_path = str(tp if tp.is_absolute() else (cwd / tp))

    orchestrator = _get_compose_orchestrator(request)
    if orchestrator is None:
        return _done({"status": "no_action"})  # runtime not loaded — fail open

    try:
        from agentalloy.api.compose_router import FromContractRequest, compose_from_contract

        result = await compose_from_contract(
            FromContractRequest(contract_path=contract_path), orchestrator
        )
    except HTTPException:
        # unsafe / malformed / invalid contract — nothing to inject
        return _done({"status": "contract_invalid"})
    except Exception:
        logger.warning("Hook post-tool-use compose failed", exc_info=True)
        return _done({"status": "no_action"})

    block = getattr(result, "output", "") or ""
    if not block:
        return _done({"status": "no_action"})  # empty domain result (e.g. no domain_tags)
    return _done({"status": "composed", "composed_block": block})


def _detect_active_contract(cwd: Path) -> str | None:
    """Most-recently-modified contract under ``.agentalloy/contracts/``, or None."""
    contracts_dir = cwd / ".agentalloy" / "contracts"
    if not contracts_dir.is_dir():
        return None
    try:
        # Contracts live in per-phase subdirs (contracts/<phase>/<slug>.md), so recurse.
        md = sorted(contracts_dir.glob("**/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return str(md[0].relative_to(cwd)) if md else None


@router.post("/v1/hook/session-start")
async def hook_session_start(request: Request) -> JSONResponse:
    """Handle a SessionStart hook event — intake is the session front door.

    Every session opens with the **intake** workflow skill, regardless of the
    current phase (so a mid-project session is greeted with "resume where you
    left off, or start something new?" rather than silently dropping into the
    old phase). The injected state below tells intake's prose whether there's
    work in flight, so it can offer a precise resume instead of guessing.

    Gated by ``session_intake_enabled`` (default off): until the full workflow
    redesign lands — the remaining phase prose and the sys-* skills — the wired
    hook still calls this endpoint but we inject nothing, so an incomplete
    workflow isn't forced on users.
    """
    from agentalloy.config import get_settings

    if not get_settings().session_intake_enabled:
        return JSONResponse(content={"status": "disabled", "composed_block": ""})

    try:
        body = await request.json()
    except Exception:
        body = {}
    cwd_str = body.get("cwd", "")
    cwd = Path(cwd_str) if cwd_str else Path.cwd()

    from agentalloy.signals.skill_loader import (
        _load_workflow_skill_for_phase,
        _read_lifecycle_mode,
        _read_phase,
    )

    # Per-repo deferral: only `full` gets the intake front-door. A repo with
    # its own agents/workflows (`assist`) — or one fully muted (`off`) — must
    # not be greeted with the intake interview on every session open.
    if _read_lifecycle_mode(cwd) != "full":
        return JSONResponse(content={"status": "disabled", "composed_block": ""})

    intake = _load_workflow_skill_for_phase("intake", cwd)
    prose = (intake or {}).get("raw_prose", "")
    if not prose:
        return JSONResponse(content={"status": "no_intake_skill", "composed_block": ""})

    phase = _read_phase(cwd)
    contract = _detect_active_contract(cwd)
    in_progress = bool(phase and phase != "intake")

    if in_progress:
        state = f"[agentalloy] Session state — work in progress · phase: {phase}"
        if contract:
            state += f" · active contract: {contract}"
        state += (
            "\nAsk whether to resume here or start something new. Resume → continue in this "
            "phase (don't re-interview). New → `agentalloy phase set intake`, then run intake."
        )
    else:
        state = (
            "[agentalloy] Session state — fresh (no work in progress). Run the intake interview."
        )

    return JSONResponse(
        content={
            "status": "intake",
            "composed_block": f"{prose}\n\n{state}",
            "phase": phase or "intake",
            "in_progress": in_progress,
            "active_contract": contract,
        }
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
