"""Signal layer integration for proxy requests.

Ties together the existing signal-layer components (pre-filter, gate
evaluation, phase transitions) so the proxy path can evaluate whether
a request should trigger skill composition.

Public API
----------
SignalResult
    evaluate_signal
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_session import resolve_session_key
from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.classifier import check_transition_trigger
from agentalloy.signals.gates import INTAKE_PHASE, decide_transition
from agentalloy.signals.predicates import section_completeness
from agentalloy.signals.prefilter import (
    PreFilterMatch,
    _extract_gate_paths,  # type: ignore[reportPrivateUsage]
    _extract_gate_sections,  # type: ignore[reportPrivateUsage]
)
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _MAX_ANNOUNCED_SESSIONS,
    _build_predicate_context,
    _intake_route_hint,
    _load_workflow_skill_for_phase,
    _read_announced_state,
    _read_composed,
    _read_cursor,
    _read_lifecycle_mode,
    _read_phase,
    _write_announced_atomic,
    _write_composed_atomic,
    _write_phase_atomic,
    exit_gates_for_phase,
)

logger = logging.getLogger(__name__)

# Project roots we've already warned about being invisible to the proxy, so the
# WARNING fires at most once per repo per process instead of on every request.
_warned_missing_root: set[str] = set()


@dataclass
class SignalResult:
    """Outcome of evaluating the signal layer for a proxy request."""

    should_compose: bool
    phase: str | None = None
    task: str | None = None
    domain_tags: list[str] | None = field(default_factory=lambda: list[str]())

    # Tier 1 (phase-entry announce). True when this request is the first one in
    # *phase* (the phase changed since we last announced), so the workflow + system
    # prose for the phase is emitted exactly once. `workflow_prose` is that phase's
    # operating instructions (the workflow skill's raw_prose). See `_read_announced`.
    announce: bool = False
    workflow_prose: str | None = None

    # Tier 2 (per-work-item domain). `current_contract` is the absolute path to the
    # work-item contract whose domain skills should be composed (body → prompt,
    # domain_tags → BM25 steer). `announce_cursor` is True when the cursor changed
    # since we last composed it (phase entry, or an `agentalloy task next`), so the
    # task's domain block fires exactly once per work-item. See `_read_composed`.
    current_contract: str | None = None
    announce_cursor: bool = False

    # Optional signal-layer metadata (for telemetry)
    pre_filter_matched: str | None = None
    gates_met: list[str] = field(default_factory=lambda: list[str]())
    gates_unmet: list[str] = field(default_factory=lambda: list[str]())
    qwen_calls: int = 0

    # True when a semantic phase-gate (or the transition-trigger intent) hit an
    # embed failure this turn — the gate fell open to UNKNOWN and the transition
    # may have silently not fired. Surfaced so telemetry can distinguish an
    # infra-degraded gate from a legitimately-unmet one. See PredicateContext.
    phase_gate_embed_failed: bool = False

    # Human-facing gate advisories (e.g. "intent fired but the exit artifact is
    # missing"). Surfaced to the agent alongside composed skills.
    advisories: list[str] = field(default_factory=lambda: list[str]())

    # Per-request attribution for telemetry: the resolved repo (str(cwd)) and the
    # session this request belongs to (key + how it was derived). The compose path
    # stamps these onto the trace so coverage/savings are queryable per-repo and
    # per-session. See ``agentalloy.api.proxy_session``.
    repo: str | None = None
    session_key: str | None = None
    session_source: str | None = None

    # Per-turn phase banner: a compact ONE-LINE recency anchor injected into the
    # trailing user message on EVERY carrier turn (independent of should_compose /
    # announce / cursor — it fires even when no workflow block is composed). Keeps the
    # active phase + its required artifact + section progress in the freshest position.
    # Set only on a carrier turn with a known phase under the active lifecycle mode;
    # None otherwise (and on any soft failure while building it). See `build_banner`.
    banner: str | None = None

    # Deferred cadence markers. The signal layer DECIDES what to record but no
    # longer writes `.agentalloy/{announced,composed}` itself — committing at
    # decision time burned a session whenever the later compose/inject produced
    # nothing (embed down, empty block, soft-fail to the original body): the phase
    # was recorded as oriented while the agent got no orientation, and Tier 1 never
    # re-fired. The injection path commits these only after the matching block is
    # actually emitted — see :func:`commit_markers`. ``pending_announce`` is
    # ``(phase, session_keys)`` for the announced file; ``pending_composed`` is the
    # cursor id for the composed file.
    pending_announce: tuple[str, list[str]] | None = None
    pending_composed: str | None = None


def _extract_task_from_messages(request: ProxyRequest) -> str | None:
    """Extract the first user message text as the task prompt.

    ``ProxyMessage.content`` may be a plain string or a list of
    Anthropic-style content blocks. Flatten the block form to text so the
    return type stays ``str | None`` as annotated.
    """
    for msg in request.messages:
        if msg.role != "user" or not msg.content:
            continue
        if isinstance(msg.content, str):
            return msg.content
        parts = [block.get("text", "") for block in msg.content if block.get("type") == "text"]
        joined = "".join(parts)
        if joined:
            return joined
    return None


def _resolve_current_contract(cwd: Path, phase: str) -> tuple[str | None, Path | None]:
    """Resolve the current work-item contract for Tier 2 domain composition.

    Returns ``(contract_id, abs_path)`` where ``contract_id`` is the cursor's
    canonical value used for cadence (the contracts-relative posix path, e.g.
    ``build/01-cache.md``) and ``abs_path`` is the file to compose.

    Work-items for a phase live in ``.agentalloy/contracts/<phase>/`` and are
    authored by the *prior* phase (the cascade hand-off). Resolution:

    1. An explicit ``.agentalloy/cursor`` (set by ``agentalloy task next``) wins,
       when it resolves to a file under ``.agentalloy/contracts/``.
    2. Exactly one contract in ``contracts/<phase>/`` → that single work-item
       (the common single-item phase: spec/design/qa/ship).
    3. Two or more, no cursor → a fan-out phase (build): don't guess which task is
       current — stay silent until ``task next`` sets the cursor.
    4. None → ``(None, None)``; Tier 2 stays silent.
    """
    from agentalloy.contracts import list_contracts_for_phase

    contracts_root = (cwd / ".agentalloy" / "contracts").resolve()
    cursor = _read_cursor(cwd)
    if cursor:
        candidate = (contracts_root / cursor).resolve()
        # Containment guard: a stale/hostile cursor must not read outside the tree.
        if candidate.is_file() and candidate.is_relative_to(contracts_root):
            return candidate.relative_to(contracts_root).as_posix(), candidate
        logger.warning("cursor %r does not resolve to a contract file; using phase default", cursor)

    in_phase = list_contracts_for_phase(cwd, phase)
    if len(in_phase) != 1:
        # 0 → nothing to compose; ≥2 → fan-out, wait for the cursor.
        return None, None
    only = in_phase[0].resolve()
    return only.relative_to(contracts_root).as_posix(), only


# Per-phase banner directive — the imperative core of the per-turn recency banner,
# keyed by SDD phase. Hand-tuned here because the pack corpus loads through a DuckDB
# schema that carries no banner column; the gate-path derivation below is the fallback
# for an unrecognized phase. Mirrors the MUST/MUST-NOT framing of each phase's orientation.
_PHASE_BANNER_DIRECTIVE: dict[str, str] = {
    "intake": "MUST capture the request as a contract (.agentalloy/contracts/) before any spec, design, or code",
    "spec": "MUST write docs/spec/<slug>.md (Acceptance Criteria + Out of Scope) before designing or coding",
    "design": "MUST write docs/design/<slug>/{approach,tasks,test-plan}.md before any src/ code",
    "build": "MUST work the design's tasks with tests — no new architecture or acceptance decisions here",
    "qa": "MUST record docs/qa/<slug>.md (Checks + Review) before shipping",
    "ship": "MUST write docs/ship/<slug>.md (Summary + Rollback); ship only what QA approved",
    "sdd-fast": "MUST write docs/fast/<slug>.md (Acceptance + Approach + Test Cases) and pass tests before ship",
}


def build_banner(
    phase: str,
    exit_gates: dict[str, Any],
    project_root: Path,
) -> str:
    """Build the compact one-line phase banner for *phase*.

    Format: ``[agentalloy · {phase}] {directive}{progress}``.

    - **directive**: the hand-tuned :data:`_PHASE_BANNER_DIRECTIVE` entry for the phase;
      for an unrecognized phase, the fallback ``MUST produce {artifact} before advancing``
      where ``{artifact}`` is the first ``path`` glob from the exit gate (via
      :func:`_extract_gate_paths`), or ``MUST satisfy the {phase} exit gate before
      advancing`` when no path is derivable.
    - **progress**: appended only when the gate declares required sections AND the
      artifact file exists — `` · {present}/{total} sections`` plus
      `` (missing: {first_missing})`` when any are missing. Computed via
      :func:`section_completeness`, whose file I/O is fully wrapped, so a missing or
      unreadable artifact contributes no progress suffix.

    Cheap and soft: all derivation is wrapped so a malformed gate or unreadable
    artifact yields a best-effort banner rather than raising.
    """
    directive = _PHASE_BANNER_DIRECTIVE.get(phase)
    if directive is None:
        try:
            paths = _extract_gate_paths(exit_gates)
        except Exception:
            paths = []
        directive = (
            f"MUST produce {paths[0]} before advancing"
            if paths
            else f"MUST satisfy the {phase} exit gate before advancing"
        )

    # progress: present/total sections + first-missing, only when the artifact exists.
    progress = ""
    try:
        sections = _extract_gate_sections(exit_gates)
        paths = _extract_gate_paths(exit_gates)
        if sections and paths:
            present, total, missing = section_completeness(paths[0], sections, project_root)
            # Only show progress once the artifact exists — section_completeness reports
            # (0, total, all) for a missing/unreadable file, which we suppress so the
            # banner doesn't claim "0/N sections" before the artifact is even created.
            artifact_exists = bool(_glob_first_exists(paths[0], project_root))
            if artifact_exists and total:
                progress = f" · {present}/{total} sections"
                if missing:
                    progress += f" (missing: {missing[0]})"
    except Exception:
        progress = ""

    return f"[agentalloy · {phase}] {directive}{progress}"


def _banner_for_turn(
    is_carrier: bool,
    phase: str,
    exit_gates: dict[str, Any],
    project_root: Path,
) -> str | None:
    """The per-turn banner string, or None.

    Returns the built banner only on a carrier turn (``is_carrier``) with a known
    *phase*; None otherwise. Independent of the announce/cursor cadence. Soft: any
    failure building the banner yields None rather than propagating — the banner is a
    recency-anchor nicety and must never break ``evaluate_signal``. The caller has
    already established the active (``full``) lifecycle mode and a valid phase.
    """
    if not is_carrier:
        return None
    try:
        return build_banner(phase, exit_gates, project_root)
    except Exception:
        logger.debug("banner build failed for phase=%s", phase, exc_info=True)
        return None


def _glob_first_exists(path_glob: str, project_root: Path) -> bool:
    """True if at least one file matches ``path_glob`` under ``project_root``.

    Soft: any IO failure yields False so the banner's progress suffix is suppressed
    rather than raising.
    """
    try:
        from agentalloy.signals.predicates import _glob_files  # type: ignore[reportPrivateUsage]

        return bool(_glob_files(project_root, path_glob))
    except Exception:
        return False


async def evaluate_signal(
    request: ProxyRequest,
    cwd: Path,
    embed_client: EmbedClient | None = None,
    session_id: str | None = None,
) -> SignalResult:
    """Evaluate the signal layer for an incoming proxy request.

    Flow:
    1. Read phase file (``.agentalloy/phase``)
    2. If no phase: return ``should_compose=False``
    3. Load workflow skill for the current phase
    4. Build PredicateContext from request data
    5. Run pre-filter (cheap keyword match)
    6. If pre-filter matched: run gate evaluation (may include semantic)
    7. If gates met: write phase transition atomically
    8. Return SignalResult indicating whether to compose

    Args:
        request: the incoming proxy request
        cwd: resolved working directory (project root)
        embed_client: optional client for semantic gate predicates

    Returns:
        SignalResult with composition decision and metadata
    """
    # 0. Per-repo lifecycle mode. Only `full` runs the phase lifecycle on the
    # proxy. `assist`/`off` defer entirely: the proxy has no phase-independent
    # injection path (all domain + system skills flow through this one compose),
    # so deferring the lifecycle means full passthrough here. The hook (Claude
    # Code) path offers the finer-grained `assist` that keeps system/domain
    # injection because those hooks fire independently of the phase. Guarding
    # before reading the phase means an assist/off repo that still has a stale
    # `.agentalloy/phase` (e.g. re-wired from full) is not composed for.
    mode = _read_lifecycle_mode(cwd)
    if mode != "full":
        logger.debug("composition deferred for %s: lifecycle_mode=%s", cwd, mode)
        return SignalResult(should_compose=False)

    # 1. Read phase file (sync, instant)
    phase = _read_phase(cwd)
    if not phase:
        # A missing `.agentalloy/` here (lifecycle is active — we passed the
        # mode!="full" guard above) is the signature of a project root that
        # isn't visible to the proxy: in container mode the decoded host path
        # must be bind-mounted at this exact path. Warn once per repo so this
        # never fails silently as a plain passthrough.
        agentalloy_dir = cwd / ".agentalloy"
        if not agentalloy_dir.exists():
            key = str(cwd)
            if key not in _warned_missing_root:
                _warned_missing_root.add(key)
                logger.warning(
                    "lifecycle active but %s is not visible to the proxy — if AgentAlloy "
                    "runs in a container, the project root must be bind-mounted at this exact "
                    "path (see AGENTALLOY_PROJECTS_ROOT). Composition skipped for this repo.",
                    agentalloy_dir,
                )
        return SignalResult(should_compose=False)

    task = _extract_task_from_messages(request)

    # Per-request attribution (repo + session), resolved once and carried on the
    # result so the compose path can stamp it onto telemetry. The session key also
    # drives the announce cadence below.
    repo = str(cwd)
    session_key, session_source = resolve_session_key(request, session_id)

    # Carrier-request gate. A harness reuses one session id across BOTH its main
    # agent loop AND background micro-requests — Claude Code sends
    # `x-claude-code-session-id` on its quota ping and its title / topic-detection
    # haiku calls too. Those auxiliary requests ship no tool array. Because the
    # one-shot orientation (Tier 1) and work-item cursor (Tier 2) markers are keyed
    # on the session, whichever request reaches the proxy first burns them — and when
    # a tool-less ping wins the race, the real conversation is recorded as oriented
    # while the agent got nothing (the exact recurring "no orientation block" bug).
    # Only a genuine agent turn — one that carries its tool definitions — may
    # announce or advance the cursor; background requests fall through to passthrough.
    is_carrier = bool(request.tools)

    # 2. Load workflow skill for the phase (sync DB query — run in thread)
    skill = await asyncio.to_thread(_load_workflow_skill_for_phase, phase, cwd)
    if skill is None:
        # No DuckDB/packs workflow skill for the phase. We can't compose, but a
        # carrier turn still gets a best-effort banner from the packaged exit gate
        # (corpus-free) so the recency anchor survives a missing profile skill.
        fallback_gates = exit_gates_for_phase(phase) or {}
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            banner=_banner_for_turn(is_carrier, phase, fallback_gates, cwd),
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )

    signal_keywords: list[str] = skill.get("signal_keywords") or []
    exit_gates: dict[str, Any] = skill.get("exit_gates") or {}

    # Per-turn banner (recency anchor). Built once here for every carrier turn under
    # the active lifecycle mode + a valid phase; independent of should_compose /
    # announce / cursor, so it threads onto every return below — quiet passthrough,
    # compose, or no-skill. Soft: never raises.
    banner = _banner_for_turn(is_carrier, phase, exit_gates, cwd)

    # 3. Build predicate context
    ctx = _build_predicate_context(
        project_root=cwd,
        phase=phase,
        prompt_text=task,
        # Proxy has no file/tool events — only prompt text
    )

    # 4. Announce cadence: a phase's orientation block is emitted once per
    #    (phase, session). `.agentalloy/announced` records the last phase AND the
    #    session key we announced for; we announce when either changed — a fresh
    #    wire / a transition (phase differs) OR a new session on the same phase
    #    (session key differs). Keying on the session, not just the phase, fixes a
    #    new session joining an already-announced phase getting no orientation
    #    (the marker is per-repo, not per-session). Mid-session same-phase turns
    #    match on both and stay quiet — the every-turn flood this replaces.
    last_phase, last_sessions = _read_announced_state(cwd)
    phase_changed = last_phase != phase
    # With a session key: announce on a new phase OR a session not yet oriented for
    # this phase. Without one (no user text): phase-only cadence (announce on entry).
    # Gated on `is_carrier` so a background micro-request never burns the marker — the
    # orientation waits for the next real agent turn instead of being lost to a ping.
    announce = is_carrier and (
        (phase_changed or session_key not in last_sessions) if session_key else phase_changed
    )

    # 5. Transition trigger (reranker-primary intent, deterministic floor). Runs
    #    for every phase, including intake — there is no unconditional bypass. On
    #    a turn carrying no completion/approval signal the trigger does not fire,
    #    so an in-progress phase stays silent unless it is also an entry turn.
    match: PreFilterMatch | None = check_transition_trigger(
        signal_keywords, exit_gates, ctx, embed_client
    )

    # 6. Eval (only when the trigger fired): evaluate exit gates, transition the
    #    phase if met, and collect gate advisories. Runs in a thread so the
    #    file/embed work in decide_transition never blocks the event loop.
    advisories: list[str] = []
    gates_met: list[str] = []
    gates_unmet: list[str] = []
    qwen_calls = 0

    if match is not None:

        def _run_gates() -> None:
            nonlocal advisories, gates_met, gates_unmet, qwen_calls
            # Leaving intake branches on the contract route: fast → sdd-fast, else
            # the linear intake → spec.
            route_hint = _intake_route_hint(cwd) if phase == INTAKE_PHASE else None
            decision = decide_transition(
                current_phase=phase,
                gate_spec=exit_gates,
                ctx=ctx,
                lm_client=embed_client,
                next_phase_hint=route_hint,
            )
            if decision.should_transition and decision.to_phase:
                try:
                    _write_phase_atomic(cwd, decision.to_phase)
                    logger.info("Phase transition: %s -> %s", phase, decision.to_phase)
                except OSError as e:
                    logger.warning("Failed to write phase file: %s", e)

            advisories = list(decision.advisories)
            gates_met = [g.gate_name for g in decision.gates_met]
            gates_unmet = [g.gate_name for g in decision.gates_unmet]
            qwen_calls = decision.qwen_calls

        await asyncio.to_thread(_run_gates)

    # Did any semantic gate / transition-trigger intent hit an embed failure this
    # turn? Read off the shared ctx (the trigger ran on this thread, the gates in
    # the worker thread — both mutate the same diagnostics sink, and to_thread
    # has already joined). Carried into telemetry so a silently-degraded gate is
    # queryable instead of only a WARNING line.
    phase_gate_embed_failed = ctx.embed_failed

    # 7. Tier 2 cadence: resolve the current work-item contract and decide whether
    #    its domain block fires. Tier 2 fires when the cursor changed since we last
    #    composed it — on phase entry (the incoming contract becomes current) or an
    #    `agentalloy task next`. Domain retrieval is keyed to the contract's task,
    #    NEVER the workflow's static process tags (which only ever emptied results).
    contract_id, contract_path = _resolve_current_contract(cwd, phase)
    # Same carrier gate as Tier 1: a tool-less background request must not burn the
    # work-item cursor marker (which would silently drop the domain block from the
    # real turn that follows).
    announce_cursor = is_carrier and contract_id is not None and _read_composed(cwd) != contract_id

    # 8. Decide. Inject when this is a phase-entry turn (Tier 1), a new work-item
    #    turn (Tier 2), OR the eval produced advisories. None → quiet passthrough.
    if not (announce or announce_cursor or advisories):
        # A quiet turn. When a clean transition fired this turn (phase written, no
        # advisory), carry the gate metadata so telemetry still records the eval
        # even though nothing is injected — the new phase announces next turn.
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            banner=banner,
            gates_met=gates_met,
            gates_unmet=gates_unmet,
            qwen_calls=qwen_calls,
            phase_gate_embed_failed=phase_gate_embed_failed,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )

    # Compute cadence state but DO NOT commit it here. Writing the markers at
    # decision time burned a session whenever the later compose/inject produced
    # nothing (embed down, empty block, soft-fail to the original body): the phase
    # was recorded as oriented while the agent received no orientation, and Tier 1
    # never re-fired. The injection path commits these only after the matching block
    # is actually emitted (see `commit_markers`). A phase entry resets the
    # oriented-session set; a new session on the same phase is appended (capped,
    # oldest dropped) so the same session stays quiet while a new one re-announces,
    # and a couple of concurrent sessions don't thrash.
    pending_announce: tuple[str, list[str]] | None = None
    if announce:
        if phase_changed:
            new_sessions = [session_key] if session_key else []
        elif session_key:
            new_sessions = [*last_sessions, session_key][-_MAX_ANNOUNCED_SESSIONS:]
        else:
            new_sessions = last_sessions
        pending_announce = (phase, new_sessions)
    pending_composed = contract_id if (announce_cursor and contract_id is not None) else None

    return SignalResult(
        should_compose=True,
        announce=announce,
        workflow_prose=skill.get("raw_prose") if announce else None,
        current_contract=str(contract_path) if announce_cursor and contract_path else None,
        announce_cursor=announce_cursor,
        phase=phase,
        task=task,
        banner=banner,
        pre_filter_matched=match.detail if match is not None else None,
        gates_met=gates_met,
        gates_unmet=gates_unmet,
        qwen_calls=qwen_calls,
        advisories=advisories,
        phase_gate_embed_failed=phase_gate_embed_failed,
        repo=repo,
        session_key=session_key,
        session_source=session_source,
        pending_announce=pending_announce,
        pending_composed=pending_composed,
    )


def commit_markers(
    project_root: Path,
    signal: SignalResult,
    *,
    announce_emitted: bool,
    cursor_emitted: bool,
) -> None:
    """Commit the deferred Tier 1 / Tier 2 cadence markers after injection.

    The injection path calls this once it knows what was actually emitted, so a
    degraded compose (embed down) or a soft-fail to the original body never records
    a phase/work-item as delivered when the agent got nothing.

    - ``announce_emitted``: the Tier 1 orientation block carried real text and was
      injected → commit ``pending_announce`` to ``.agentalloy/announced``.
    - ``cursor_emitted``: the Tier 2 domain leg reached a *terminal* state — it
      delivered skills or composed to a clean empty result, NOT a transient compose
      error → commit ``pending_composed`` to ``.agentalloy/composed`` so a work-item
      with genuinely no domain skills does not re-fire every turn.

    No-op when the corresponding ``pending_*`` is unset (the signal did not decide
    to announce / advance the cursor this turn).
    """
    if announce_emitted and signal.pending_announce is not None:
        phase, sessions = signal.pending_announce
        try:
            _write_announced_atomic(project_root, phase, sessions)
        except OSError as e:
            logger.warning("Failed to write announced file: %s", e)
    if cursor_emitted and signal.pending_composed is not None:
        try:
            _write_composed_atomic(project_root, signal.pending_composed)
        except OSError as e:
            logger.warning("Failed to write composed file: %s", e)
