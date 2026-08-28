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

# Structural cross-module reuse of private signal-layer helpers (imported into
# this public API module on purpose) — suppress private-usage reporting for the
# whole module rather than per call site.
# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.api.proxy_models import ProxyRequest
from agentalloy.api.proxy_session import resolve_session_key
from agentalloy.api.state_leg import build_state_leg
from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.classifier import check_transition_trigger
from agentalloy.signals.gates import INTAKE_PHASE
from agentalloy.signals.predicates import section_completeness, store_section_completeness
from agentalloy.signals.prefilter import (
    PreFilterMatch,
    _extract_artifact_contains_specs,  # type: ignore[reportPrivateUsage]
    _extract_artifact_contains_store_specs,  # type: ignore[reportPrivateUsage]
    _extract_artifact_exists_store_specs,  # type: ignore[reportPrivateUsage]
    _extract_exists_only_paths,  # type: ignore[reportPrivateUsage]
    _extract_gate_paths,  # type: ignore[reportPrivateUsage]
)
from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
    _MAX_ANNOUNCED_SESSIONS,
    _build_predicate_context,
    _intake_route_hint,
    _load_workflow_skill_for_phase,
    _phase_state,
    _read_announced_state,
    _read_banner_turn,
    _read_composed,
    _read_cursor,
    _read_lifecycle_mode,
    _read_orientation_announced,
    _write_announced_atomic,
    _write_banner_turn_atomic,
    _write_composed_atomic,
    _write_orientation_announced_atomic,
    _write_phase_atomic,
    exit_gates_for_phase,
)
from agentalloy.storage.protocols import TelemetryStore
from agentalloy.telemetry.phase_writer import PhaseTelemetryWriter

logger = logging.getLogger(__name__)

# Project roots we've already warned about being invisible to the proxy, so the
# WARNING fires at most once per repo per process instead of on every request.
_warned_missing_root: set[str] = set()


# Label for phase-boundary confirmation directives — distinct from the
# gate-advisory ``[agentalloy-eval]`` block so telemetry can tell them apart
# (phase-boundary-confirmation T1). Reuses the advisory injection seam.
CONFIRM_LABEL = "agentalloy-confirm"


@dataclass
class SignalResult:
    """Outcome of evaluating the signal layer for a proxy request."""

    should_compose: bool
    phase: str | None = None
    task: str | None = None
    trace_id: str | None = None
    domain_tags: list[str] | None = field(default_factory=lambda: list[str]())

    # Tier 1 (phase-entry announce). True when this request is the first one in
    # *phase* (the phase changed since we last announced), so the workflow + system
    # prose for the phase is emitted exactly once. `workflow_prose` is that phase's
    # operating instructions (the workflow skill's raw_prose). See `_read_announced`.
    announce: bool = False
    workflow_prose: str | None = None
    # The skill id behind `workflow_prose` (the phase's workflow skill), so the
    # Tier 1 header's source skill is identifiable in telemetry. None when no
    # workflow skill was loaded for the phase.
    workflow_skill_id: str | None = None

    # The phase's operating instructions for the *system-prompt* leg. Same prose as
    # `workflow_prose`, but with banner-like cadence: populated on EVERY carrier turn,
    # not just the announce turn. The harness rebuilds each request from its own local
    # history and never sees our mutation, so a once-per-(phase, session) system block
    # survives exactly one request and the agent goes blind from turn 2 on. Re-sending
    # it every turn is what keeps it in front of the model.
    #
    # Deliberately NOT marker-tracked, and it does NOT participate in `commit_outcome`
    # — any cadence marker on this leg would reintroduce the deliver-once bug in a new
    # place. The banner works for exactly this reason.
    #
    # Phase-pure by construction: `_load_workflow_skill_for_phase` is pure in
    # (phase, cwd) — shipped pack prose plus an on-disk profile override, no task text
    # and no retrieval — so the bytes are identical for every turn of a phase. That is
    # a *precondition* for prompt caching, not proof of it: whether this block is
    # actually cached depends on where the harness places its `cache_control`
    # breakpoint relative to where we append. See the COST note at the injection site
    # in `proxy_passthrough_router` — currently unmeasured.
    workflow_system_prose: str | None = None

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

    # Phase-boundary confirmation directives (phase-boundary-confirmation): a
    # deterministic MUST-ask prompt at a lifecycle boundary — e.g. at ship
    # completion, "ask the user whether to reset to intake". Injected via the
    # advisory seam under the distinct CONFIRM_LABEL; never writes the phase file.
    confirm_directives: list[str] = field(default_factory=lambda: list[str]())

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

    # Per-turn state leg: structured JSON context briefing injected into the
    # trailing user message on EVERY carrier turn (same cadence as banner).
    # Gives the LLM a machine-readable snapshot of phase, contract, artifacts,
    # and gate status — replaces the need for CLI queries. Designed for the
    # stateless-phase model: a fresh agent can read this and operate immediately.
    # None when the state is too thin to be useful or building failed.
    # See `agentalloy.api.state_leg.build_state_leg`.
    state_leg: str | None = None

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

    # Orientation marker cadence. ``announce_orientation`` is True when this is the
    # first request in this session for the current phase (orientation should fire).
    # ``pending_orientation`` is ``(phase, session_keys)`` for the orientation
    # cadence file; committed by ``commit_markers`` after the orientation block is
    # actually emitted. Follows the same once-per-(phase, session) pattern as
    # ``pending_announce`` but uses the orientation marker family (fires BEFORE the
    # workflow block).
    announce_orientation: bool = False
    pending_orientation: tuple[str, list[str]] | None = None

    # Workflow pause mode (``mode: paused`` in the store's phase row): ALL workflow
    # steering is paused (orientation, banner, exit gates, transitions, intake)
    # but domain-skill composition keyed on the request's task text is kept.
    # When True the compose path takes the compose-only branch
    # (``_compose_pause_block``) instead of the 3-tier workflow block.
    paused_mode: bool = False


def _extract_task_from_messages(request: ProxyRequest) -> str | None:
    """Extract the latest user message text as the task prompt.

    Chat-completions requests resend the full conversation history on every
    call, so the *first* user message is only ever the session's opening line
    (e.g. "hi") — scanning forward and returning on the first match pins the
    signal-keyword trigger (``recent_prompt_text`` in
    ``signals/prefilter.check_prefilter``) to that opening line for the rest of
    the session, silently disabling keyword-driven phase transitions. Scan in
    reverse instead, mirroring ``proxy_injection._last_user_index``, so this
    always reflects the current turn.

    ``ProxyMessage.content`` may be a plain string or a list of
    Anthropic-style content blocks. Flatten the block form to text so the
    return type stays ``str | None`` as annotated.
    """
    for msg in reversed(request.messages):
        if msg.role != "user" or not msg.content:
            continue
        if isinstance(msg.content, str):
            return msg.content
        parts = [block.get("text", "") for block in msg.content if block.get("type") == "text"]
        joined = "".join(parts)
        if joined:
            return joined
    return None


def _resolve_current_contract(
    cwd: Path,
    phase: str,
    session_key: str | None = None,
) -> tuple[str | None, Path | None]:
    """Resolve the current work-item contract for Tier 2 domain composition.

    Returns ``(contract_id, None)`` where ``contract_id`` is the store key
    (e.g. ``01-cache``). The path component is deprecated and always ``None``;
    consumers should load the contract from the store using the id.

    Resolution reads the cursor value (scoped then shared) and treats it as
    the contract's store key.  The cursor is seeded to the phase's first
    work-item on entry and advanced by ``agentalloy task next``.  Seeded
    cursors carry the marker form ``active/{phase}/{contract_id}.md``
    (``_write_phase_atomic`` / ``_auto_create_next_contract``); that wrapper
    is unwrapped here so the returned id is the bare store key that
    ``get_contract`` resolves — otherwise callers like
    ``_auto_create_next_contract`` look up a key that doesn't exist.
    """
    # Try scoped cursor first, then shared cursor
    cursor_val = _read_cursor(cwd, session_key)
    if not cursor_val:
        cursor_val = _read_cursor(cwd, None)

    if cursor_val:
        # The cursor value is the contract_id (store key), possibly wrapped in
        # the seeded marker form.
        # Containment guard: reject path-traversal values like "../../../etc/passwd".
        # A valid contract_id must not escape the project root.
        if ".." in cursor_val or cursor_val.startswith("/"):
            # Treat as invalid — fall through to the first-active-fallback below.
            pass
        else:
            contract_id = cursor_val
            if contract_id.startswith("active/"):
                # Unwrap active/{phase}/{contract_id}.md — split once past the
                # phase segment so multi-segment ids (``spec/slug``) survive.
                _parts = contract_id.split("/", 2)
                if len(_parts) == 3:
                    contract_id = _parts[2]
            if contract_id.endswith(".md"):
                contract_id = contract_id[: -len(".md")]
            return contract_id, None

    # No cursor — fall back to the first active contract for the phase.
    # Strict fail-safe: ≥2 contracts with no cursor → stay silent (don't guess).
    try:
        from agentalloy.signals.skill_loader import (  # noqa: PLC0415
            _phase_view,
        )

        view = _phase_view(cwd)
        if view is not None:
            rows = view.list_contracts(phase=phase, status="active")
            if rows:
                rows_sorted = sorted(rows, key=lambda r: str(r.get("contract_id", "")))
                if len(rows_sorted) == 1:
                    return rows_sorted[0]["contract_id"], None
                # ≥2 contracts, no cursor → strict resolver returns silent
                return None, None
    except Exception:  # noqa: BLE001 — fail-soft
        pass

    return None, None


# Per-phase banner status line — the declarative core of the per-turn recency banner,
# keyed by SDD phase. Hand-tuned here because the pack corpus loads through a DuckDB
# schema that carries no banner column; the gate-path derivation below is the fallback
# for an unrecognized phase.
#
# Deliberately phrased as a fact about state ("phase instructions: system prompt"),
# not a command ("Review..."/"MUST..."). This banner rides in the last USER message,
# unattributed except for the marker comment — an imperative voice there reads as an
# unattributed third party issuing orders inside the user's own turn, which is exactly
# the shape Claude's injected-content defenses are tuned to refuse. State the fact and
# let the model act on it; don't tell the model what to do from inside content that
# isn't the user or the system.
_PHASE_BANNER_DIRECTIVE: dict[str, str] = {
    "intake": "phase instructions: system prompt",
    "spec": "phase instructions: system prompt",
    "design": "phase instructions: system prompt",
    "plan": "phase instructions: system prompt",
    "build": "phase instructions: system prompt",
    "qa": "phase instructions: system prompt",
    "ship": "phase instructions: system prompt",
    "sdd-fast": "phase instructions: system prompt",
}

# Trailing clause on every directive: the banner is a recency anchor, not the
# instructions themselves. Pointing at the system prompt keeps it short and keeps
# the authority where it belongs.
_BANNER_SUFFIX = "phase instructions: system prompt"


# Per-phase base cadence, in carrier turns (#587 §2). Replaces the flat 5: build is
# heads-down coding where a status line is pure distraction, while the short
# front-of-lifecycle phases are where an agent actually drifts off-contract.
_PHASE_BANNER_CADENCE: dict[str, int] = {
    "intake": 2,
    "spec": 3,
    "design": 4,
    "plan": 4,
    "build": 10,
    "qa": 6,
    "ship": 4,
    "sdd-fast": 8,
}

_DEFAULT_BANNER_CADENCE = 5

# The only filesystem locations the banner may name: real code deliverables.
# Everything else in the lifecycle is store-backed.
_CODE_PATH_PREFIXES = ("src/", "tests/")


def _banner_turn_cadence() -> int:
    """The flat fallback cadence: ``AGENTALLOY_BANNER_TURN_CADENCE`` or 5.

    Retained as the hard override and the unknown-phase default;
    :func:`_adaptive_banner_cadence` is what the live path calls. A
    non-positive/invalid value falls back to 1 (emit every turn), so the banner is
    never silently suppressed forever.
    """
    raw = os.environ.get("AGENTALLOY_BANNER_TURN_CADENCE")
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    return _DEFAULT_BANNER_CADENCE


def _adaptive_banner_cadence(phase: str, turns_in_phase: int) -> int:
    """Carrier turns between banner emissions for *phase* (#587 §2).

    Phase base interval, stretched the longer the agent has been in the phase — by
    turn 50 the phase's shape is internalized and the anchor has done its job.
    Floors at 1 so the banner can never be suppressed entirely.

    ``AGENTALLOY_BANNER_TURN_CADENCE`` is a hard override and replaces the whole
    calculation, unchanged from before.

    Note what is deliberately NOT modeled: the issue proposes a "progress changed →
    emit immediately" rule and a "×1.5 while tool-using" damper. The first is
    redundant with the content-hash dedup (§3), which already emits precisely when
    the rendered banner differs and suppresses it when it doesn't — a strictly
    better signal than a separate progress snapshot, and one less state row. The
    second needs per-turn tool-use visibility the proxy does not have here.
    """
    if os.environ.get("AGENTALLOY_BANNER_TURN_CADENCE"):
        return _banner_turn_cadence()
    base = _PHASE_BANNER_CADENCE.get(phase, _DEFAULT_BANNER_CADENCE)
    if turns_in_phase > 50:
        base *= 2
    elif turns_in_phase > 20:
        base = int(base * 1.5)
    return max(1, base)


def _checkpoint_label(path_glob: str) -> str:
    """A short human label for an exists-only checkpoint glob.

    A contract-related glob (e.g. ``contracts/build/*``) maps to ``"build contracts"``;
    a non-contract glob falls back to its last non-wildcard segment, or the glob itself.
    """
    segments = [seg for seg in path_glob.split("/") if seg and "*" not in seg]
    parent = segments[-1] if segments else ""
    if "contract" in path_glob.lower() and parent:
        return f"{parent} contracts"
    return parent or path_glob


def _store_artifact_status(
    exit_gates: dict[str, Any],
    store: Any,
    slug: str | None,
) -> tuple[list[str], int, int, list[str], bool]:
    """Store-backed half of the banner: what's unrecorded, and section progress.

    Returns ``(unrecorded_names, present, total, missing, any_recorded)``.

    ``unrecorded_names`` are the artifact names the phase's gates require that the
    store holds no row for — the concrete thing the directive names. Names are the
    gate's ``name`` value with a bare ``*.md`` rendered as "its artifact", since
    "``*.md`` not yet recorded" reads as a filename to write.

    Never raises; an unreachable store degrades to "nothing known", which the caller
    renders as the plain phase directive.
    """
    unrecorded: list[str] = []
    present_total = 0
    section_total = 0
    missing: list[str] = []
    any_recorded = False

    for gate_phase, name in _extract_artifact_exists_store_specs(exit_gates):
        try:
            rows = (  # type: ignore[assignment]
                store.list_artifacts(gate_phase, slug=slug, name_glob=name) if store else []
            )
        except Exception:
            rows = []
        if rows:
            any_recorded = True
        else:
            label = "its artifact" if any(c in name for c in "*?[]") else name
            if label not in unrecorded:
                unrecorded.append(label)

    for gate_phase, name, sections in _extract_artifact_contains_store_specs(exit_gates):
        present, total, gate_missing = store_section_completeness(
            store,
            gate_phase,
            name,
            sections,
            slug=slug,
        )
        present_total += present
        section_total += total
        missing.extend(gate_missing)

    return unrecorded, present_total, section_total, missing, any_recorded


def build_banner(
    phase: str,
    exit_gates: dict[str, Any],
    project_root: Path,
    slug: str | None = None,
    store: Any = None,
) -> str:
    """Build the compact phase banner for *phase*.

    Format: ``[agentalloy · {phase}] {directive}{progress}{checkpoint}``.

    - **directive** (#587 §1): names the specific store artifact the phase still owes,
      e.g. ``approach.md not yet recorded · phase instructions: system prompt``. Falls
      back to the bare :data:`_PHASE_BANNER_DIRECTIVE` entry when everything required is
      already recorded or the store is out of reach, and to ``{phase} exit gate not yet
      satisfied`` for an unrecognized phase. When *slug* is known, the literal ``<slug>``
      placeholder is resolved.

      Two properties are load-bearing and deliberately diverge from #587's draft table:

      * **Store names, never filesystem paths.** The draft specified
        ``docs/design/<slug>/tasks.md``; lifecycle artifacts are store-backed, so a
        path there is both unsatisfiable and an instruction to write a file the exit
        gate cannot see. That is precisely the failure that taught agents to create
        ``docs/fast/``. The draft's stated derivation (walk gates for ``path``) also
        cannot produce that table — ``_extract_gate_paths`` returns nothing for a
        store-backed gate.
      * **Declarative voice, never imperative.** "not yet recorded", not "produce X".
        See the comment on :data:`_PHASE_BANNER_DIRECTIVE`: this rides in the last
        USER message, where an order from an unattributed third party is the exact
        shape Claude's injected-content defenses refuse.

    - **progress**: `` · {present}/{total} sections (missing: a, b, c)``, shown once at
      least one target artifact exists. Scored PER GATE against ITS OWN artifact — store
      rows via :func:`store_section_completeness`, files via
      :func:`section_completeness`. ``missing`` is capped at three.
    - **checkpoint**: a second line per unmet exists-only *filesystem* gate (``src/**``,
      ``tests/**`` — the disk deliverables that are genuinely disk deliverables).

    Cheap and soft: all derivation is wrapped so a malformed gate, unreadable artifact,
    or unreachable store yields a best-effort banner rather than raising.
    """
    # Store-backed artifacts first: they carry the concrete directive noun.
    unrecorded: list[str] = []
    s_present = s_total = 0
    s_missing: list[str] = []
    any_recorded = False
    try:
        unrecorded, s_present, s_total, s_missing, any_recorded = _store_artifact_status(
            exit_gates,
            store,
            slug,
        )
    except Exception:
        logger.debug("banner store status failed for phase=%s", phase, exc_info=True)

    base = _PHASE_BANNER_DIRECTIVE.get(phase)
    if unrecorded:
        directive = f"{', '.join(unrecorded[:2])} not yet recorded · {_BANNER_SUFFIX}"
    elif base is not None:
        directive = base
    else:
        directive = f"{phase} exit gate not yet satisfied · {_BANNER_SUFFIX}"
    if slug:
        directive = directive.replace("<slug>", slug)

    # progress: aggregate section completeness across EACH artifact_contains gate, scoring
    # every required heading against ITS OWN artifact (not all sections against the first).
    progress = ""
    try:
        present_total = s_present
        section_total = s_total
        missing: list[str] = list(s_missing)
        any_artifact = any_recorded
        # `_extract_artifact_contains_specs` SYNTHESIZES a `docs/<phase>/<name>` glob for
        # store-backed gates so its filesystem-shaped signature has something to return.
        # Nothing is ever written at those paths, so scoring them always reported zero and
        # silently suppressed the progress suffix for every store-backed phase. Only score
        # globs that came from a real `path:` key; the store gates are scored above.
        real_paths = set(_extract_gate_paths(exit_gates))
        for path, gate_sections in _extract_artifact_contains_specs(exit_gates):
            if path not in real_paths:
                continue
            if _glob_first_exists(path, project_root):
                any_artifact = True
            present, total, gate_missing = section_completeness(path, gate_sections, project_root)
            present_total += present
            section_total += total
            missing.extend(gate_missing)
        # Only show progress once at least one artifact exists — the scorers report
        # (0, n, all) for a missing artifact, which we suppress so the banner doesn't
        # claim "0/N sections" before anything is produced.
        if any_artifact and section_total:
            progress = f" · {present_total}/{section_total} sections"
            if missing:
                progress += f" (missing: {', '.join(missing[:3])})"
    except Exception:
        progress = ""

    # checkpoint: surface unmet pure-existence gates (no sections) on a second line.
    # Restricted to CODE paths (`src/`, `tests/`) — the only disk deliverables an agent
    # legitimately writes. Any other exists-only glob is a lifecycle artifact whose
    # status lives in the data store, not on disk.
    checkpoint = ""
    try:
        for path in _extract_exists_only_paths(exit_gates):
            if not path.startswith(_CODE_PATH_PREFIXES):
                continue
            if path not in directive and not _glob_first_exists(path, project_root):
                checkpoint += f"\n · 0 {_checkpoint_label(path)} (need ≥1)"
    except Exception:
        checkpoint = ""

    return f"[agentalloy · {phase}] {directive}{progress}{checkpoint}"


def _banner_store(project_root: Path) -> Any:
    """Artifact-store handle for the banner, or ``None`` when out of reach.

    Soft by construction: the banner is a recency anchor, so a store outage must
    degrade it to the plain per-phase directive, never raise.
    """
    try:
        from agentalloy.signals.skill_loader import _state_view  # noqa: PLC0415

        return _state_view(project_root)
    except Exception:
        logger.debug("banner store handle unavailable", exc_info=True)
        return None


def _banner_for_turn(
    should_emit: bool,
    phase: str,
    exit_gates: dict[str, Any],
    project_root: Path,
    slug: str | None = None,
    store: Any = None,
    *,
    is_phase_entry: bool = False,
    mutate: bool = True,
) -> str | None:
    """The per-turn banner string, or None.

    Returns the built banner only when *should_emit* (a carrier turn that landed on the
    banner cadence tick) with a known *phase*; None otherwise. Independent of the
    announce/cursor cadence. Soft: any failure building the banner yields None rather than
    propagating — the banner is a recency-anchor nicety and must never break
    ``evaluate_signal``. The caller has already established the active (``full``) lifecycle
    mode and a valid phase, and passes the resolved contract *slug* (when known) so the
    directive's ``<slug>`` is concrete.

    Redundant-emission suppression (#587 §3): on a cadence tick whose rendered text is
    byte-identical to the last emitted banner, returns ``None`` — re-sending a string the
    agent already has costs tokens and teaches it to skim the anchor. The cadence counter
    still advances in the caller, so throttle timing is unchanged.

    *is_phase_entry* forces the emit past the dedup. This is load-bearing, not a nicety:
    under the plain per-phase directives every phase renders nearly the same text, so a
    naive hash compare would swallow the phase-entry banner — the one emission that most
    needs to land.
    """
    if not should_emit:
        return None
    try:
        text = build_banner(phase, exit_gates, project_root, slug=slug, store=store)
    except Exception:
        logger.debug("banner build failed for phase=%s", phase, exc_info=True)
        return None

    try:
        from agentalloy.signals.skill_loader import (  # noqa: PLC0415
            _read_banner_hash,
            _write_banner_hash_atomic,
        )

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not is_phase_entry and _read_banner_hash(project_root) == digest:
            return None  # identical to what the agent already has — suppress
        if mutate:
            _write_banner_hash_atomic(project_root, digest)
    except Exception:
        logger.debug("banner dedup failed for phase=%s", phase, exc_info=True)

    return text


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


# Sentinel recorded in `.agentalloy/announced` while workflow pause is active. It can
# never equal a real phase name, so (a) the pause-mode domain compose gets its own
# once-per-session cadence on the existing announced machinery, and (b) on resume
# the recorded "phase" mismatches the real one, guaranteeing a fresh orientation
# (intake included) as if it were the first request.
_PAUSE_ANNOUNCED = "__paused__"


def _evaluate_pause_mode(
    request: ProxyRequest,
    cwd: Path,
    phase: str,
    paused_since: str | None,
    session_id: str | None,
    *,
    mutate: bool,
) -> SignalResult:
    """Evaluate a proxy request for a repo in workflow pause mode (compose-only).

    Pause mode is fully silent: no Tier 1 orientation, no banner, no daily
    reminder, no exit-gate evaluation, no phase transition, no intake compose,
    no advisories. What remains:

    - **Domain compose**, once per (carrier) session, keyed on the request's task
      text rather than a work-item contract. Cadence rides the existing announced
      machinery under the :data:`_PAUSE_ANNOUNCED` sentinel; the marker is
      committed by the injection path only after delivery (``pending_announce``),
      exactly like the workflow-mode Tier 1.

    Carrier-gated like workflow mode: only identifiable sessions (``session_key`` present)
    trigger compose/cadence; anonymous requests are forwarded silently.
    """
    task = _extract_task_from_messages(request)
    repo = str(cwd)
    session_key, session_source = resolve_session_key(request, session_id)

    # Same carrier rule as workflow mode (see the gate in evaluate_signal):
    # tool-bearing turns always carry; tool-less turns carry iff the session is
    # fingerprint-keyed (background requests can't share a fingerprint key, so
    # the marker-burn race is header-source-only).
    if not session_key:
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            paused_mode=True,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )

    # Once-per-session domain compose cadence, on the announced machinery under
    # the pause sentinel. Without a session key, fall back to once-per-entry.
    last_phase, last_sessions = _read_announced_state(cwd)
    announce = (
        (last_phase != _PAUSE_ANNOUNCED or session_key not in last_sessions)
        if session_key
        else last_phase != _PAUSE_ANNOUNCED
    )
    announce = announce and bool(task)

    if not announce:
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            paused_mode=True,
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )

    pending_announce: tuple[str, list[str]] | None = None
    if announce:
        if last_phase != _PAUSE_ANNOUNCED:
            new_sessions = [session_key] if session_key else []
        elif session_key:
            new_sessions = [*last_sessions, session_key][-_MAX_ANNOUNCED_SESSIONS:]
        else:
            new_sessions = last_sessions
        pending_announce = (_PAUSE_ANNOUNCED, new_sessions)

    return SignalResult(
        should_compose=True,
        announce=announce,
        phase=phase,
        task=task,
        paused_mode=True,
        repo=repo,
        session_key=session_key,
        session_source=session_source,
        pending_announce=pending_announce,
    )


# Phases where auto-creating a next-phase contract on transition is appropriate.
# spec→design: same slug, carry scope forward. design→plan: same slug.
# build→qa: same slug, verification-focused.
_AUTO_CREATE_PHASES = {"design", "plan", "qa"}


def _auto_create_next_contract(
    project_root: Path,
    to_phase: str,
    current_contract_id: str | None,
    store: Any,
) -> None:
    """Auto-create a next-phase contract after a phase transition.

    Copies the slug and scope from the current contract into a fresh contract
    for the target phase. This means the next phase's agent starts with a
    contract already in place — no CLI ``contract init`` needed.

    The new contract_id is phase-scoped (``{phase}/{slug}``, the same scheme
    as CLI ``contract init``): a work-item's slug is continuous across phases,
    and ``put_contract`` upserts on contract_id alone, so a bare-slug id would
    overwrite the prior phase's contract instead of adding a sibling.

    Soft: never raises. A failure leaves the next phase without an auto-created
    contract (the agent can still create one manually).
    """
    if to_phase not in _AUTO_CREATE_PHASES:
        return
    if not current_contract_id or store is None:
        return

    try:
        row = store.get_contract(current_contract_id)
        if row is None:
            return

        slug = row.get("slug", "")
        if not slug:
            return

        # Check if a contract already exists for this phase+slug
        existing = store.list_contracts(phase=to_phase, slug=slug, status="active")
        if existing:
            return  # already exists, don't duplicate

        # Phase-scoped id (see docstring): a bare slug would collide with the
        # same work-item's contract in other phases and upsert over it.
        new_contract_id = f"{to_phase}/{slug}"

        # Carry forward scope from the current contract
        import json as _json

        scope_touches_raw = row.get("scope_touches", "[]")
        scope_avoids_raw = row.get("scope_avoids", "[]")
        domain_tags_raw = row.get("domain_tags", "[]")
        try:
            scope_touches = (
                _json.loads(scope_touches_raw)
                if isinstance(scope_touches_raw, str)
                else scope_touches_raw
            )
        except Exception:
            scope_touches = []
        try:
            scope_avoids = (
                _json.loads(scope_avoids_raw)
                if isinstance(scope_avoids_raw, str)
                else scope_avoids_raw
            )
        except Exception:
            scope_avoids = []
        try:
            domain_tags = (
                _json.loads(domain_tags_raw)
                if isinstance(domain_tags_raw, str)
                else domain_tags_raw
            )
        except Exception:
            domain_tags = []

        store.put_contract(
            new_contract_id,
            phase=to_phase,
            slug=slug,
            domain_tags=domain_tags if isinstance(domain_tags, list) else [],
            scope_touches=scope_touches if isinstance(scope_touches, list) else [],
            scope_avoids=scope_avoids if isinstance(scope_avoids, list) else [],
            body="",  # empty body — the agent fills it in
        )
        logger.info(
            "Auto-created contract for phase=%s slug=%s (from %s)",
            to_phase,
            slug,
            current_contract_id,
        )

        # Re-seed the cursor to the new contract so the next turn picks it up
        from agentalloy.signals.skill_loader import _write_cursor_atomic

        cursor = f"active/{to_phase}/{new_contract_id}.md"
        _write_cursor_atomic(project_root, cursor)

    except Exception:
        logger.debug(
            "auto-create contract failed for phase=%s",
            to_phase,
            exc_info=True,
        )


async def evaluate_signal(
    request: ProxyRequest,
    cwd: Path,
    embed_client: EmbedClient | None = None,
    session_id: str | None = None,
    *,
    mutate: bool = True,
    vector_store: TelemetryStore | None = None,
    phase_telemetry: PhaseTelemetryWriter | None = None,
) -> SignalResult:
    """Evaluate the signal layer for an incoming proxy request.

    ``mutate=False`` runs the full evaluation read-only — no phase-file write on
    a met transition and no banner-turn counter bump — for simulators (the web
    UI's signal playground) that must observe the decision without advancing
    repo state. Cadence markers are never written here either way; that's
    ``commit_markers``'s job, which a simulator simply never calls.

    Flow:
    1. Read phase from the store
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
        vector_store: telemetry store; used to build a fallback phase-event
            writer when ``phase_telemetry`` is not supplied.
        phase_telemetry: the app-state-scoped ``PhaseTelemetryWriter`` (task
            04). Reused instead of constructing a fresh writer per call so the
            schema DDL doesn't re-run on every write. Falls back to a
            per-call writer over ``vector_store`` when not supplied (e.g.
            callers without app.state, such as tests and the web playground).

    Returns:
        SignalResult with composition decision and metadata

    """
    # Trace ID — single source of truth for correlating all telemetry events
    # (phase_start, llm_sent, llm_received, composition_traces) for this request.
    # Generated at the very start so all early-return paths have it.
    trace_id = str(uuid.uuid4())

    # 0. Per-repo lifecycle mode. Only `full` runs the phase lifecycle on the
    # proxy. `assist`/`off` defer entirely: the proxy has no phase-independent
    # injection path (all domain + system skills flow through this one compose),
    # so deferring the lifecycle means full passthrough here. The hook (Claude
    # Code) path offers the finer-grained `assist` that keeps system/domain
    # injection because those hooks fire independently of the phase. Guarding
    # before reading the phase means an assist/off repo that still has a stale
    # phase row in the store (e.g. re-wired from full) is not composed for.
    mode = _read_lifecycle_mode(cwd)
    if mode != "full":
        logger.debug("composition deferred for %s: lifecycle_mode=%s", cwd, mode)
        return SignalResult(should_compose=False, trace_id=trace_id)

    # 1. Read phase from the store (sync, instant). `transitioned_by` is read in
    # the same breath — the session key (if any) that caused *this* phase value,
    # so a later comparison against this turn's own `session_key` can tell
    # whether a different concurrent session moved the phase (see
    # `_boundary_confirm_directives`'s "swept" case). Captured before this
    # turn's own potential transition further down, so it reflects "who set
    # the phase as of the start of this turn", never this turn's own write.
    # One store read, projected three ways (phase, actor, flow mode).
    # hand this request a mixed view of the same row.
    seeded_this_turn = False
    phase_state = _phase_state(cwd)
    phase = phase_state.phase if phase_state else None
    transitioned_by = phase_state.transitioned_by if phase_state else None
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
            return SignalResult(should_compose=False, trace_id=trace_id)

        # Wired, but phase-less: `wire` writes no state at all (AC-9), so the
        # entry phase is seeded lazily, here, on the first real request. Before
        # this, a wired repo with no phase row was inert — every prompt fell
        # through as a plain passthrough until someone ran `phase set` by hand.
        # A read-only evaluation seeds nothing and just evaluates as intake.
        phase = INTAKE_PHASE
        if mutate:
            _write_phase_atomic(cwd, phase)
            phase_state = _phase_state(cwd)
            transitioned_by = phase_state.transitioned_by if phase_state else None
            seeded_this_turn = True

    # 1b. Pause guard (single guard point). ``mode: paused`` in the phase row
    # flips the whole request into compose-only handling: no orientation, no
    # banner, no gate eval, no phase transition, no intake compose — but domain
    # skills for the task content still compose (via SignalResult.paused_mode →
    # `_compose_pause_block`). The phase value itself is untouched; resume
    # returns to it exactly. Because this branch never commits the announce /
    # composed / banner markers under their workflow keys, resuming re-orients
    # (and re-runs intake) as if this were the first request.
    # Legacy alias: ``mode: free`` (old name) reads as paused — kept indefinitely.
    paused_mode = phase_state is not None and (phase_state.mode or "").lower() in ("paused", "free")
    pause_since = phase_state.paused_since if (paused_mode and phase_state) else None
    if paused_mode:
        return _evaluate_pause_mode(request, cwd, phase, pause_since, session_id, mutate=mutate)

    task = _extract_task_from_messages(request)

    # Per-request attribution (repo + session), resolved once and carried on the
    # result so the compose path can stamp it onto telemetry. The session key also
    # drives the announce cadence below.
    repo = str(cwd)
    session_key, session_source = resolve_session_key(request, session_id)

    # Carrier-request gate: only identifiable sessions are carriers.
    #
    # Every supported harness (Claude Code, Qwen Code, aider, hermes …) is either
    # header-keyed or fingerprint-keyed, so ``session_key`` is the sole signal:
    # if we have a session id, we're an identifiable agent session and compose/inject
    # cadence apply. Anonymous requests (no session header, no user message for
    # fingerprinting) are forwarded silently — they don't carry enough identity to
    # justify burning the once-per-session orientation markers.
    #
    # The legacy ``request.tools`` carve-out and the ``SOURCE_FINGERPRINT``
    # special-case are removed: the per-session announcement/confirmation logic
    # already ensures orientation and cursors are burned exactly once per (phase,
    # session), so there's no race between main-loop and background requests.
    is_carrier = bool(session_key)

    # Resolve the active work-item contract ONCE here (reused for the banner's <slug>
    # resolution and the Tier 2 cursor cadence further down). `phase` is the in-memory
    # phase for this turn; a later transition writes the phase file but leaves it unchanged.
    contract_id, _contract_path = _resolve_current_contract(cwd, phase, session_key)
    # Derive slug from contract_id (store key) for the banner's <slug> placeholder.
    # contract_id is either a store key (e.g. "01-cache") or a contracts-relative
    # path from the filesystem fallback (e.g. "active/build/01-cache.md").
    if contract_id:
        _stem = contract_id.split("/")[-1]  # handle both forms
        contract_slug = _stem.rsplit(".", 1)[0] if "." in _stem else _stem
    else:
        contract_slug = None

    # Per-turn banner cadence. The recency-anchor banner is emitted once every
    # `_banner_turn_cadence()` carrier turns rather than on every turn — the every-turn
    # flood costs the agent acknowledgement tokens. Reset-and-emit on a phase or session
    # change, so the banner always fires on phase entry (aligned with the orientation
    # block) and once for a new session. The counter is written EAGERLY here: the banner
    # is best-effort and the deferred commit seam is a no-op on quiet/banner-only turns,
    # so a one-off miscount on an upstream error is harmless.
    #
    # The interval is now phase-aware and stretches with time-in-phase
    # (`_adaptive_banner_cadence`, #587 §2): build gets a wide 10 so a heads-down
    # coding stretch isn't interrupted, intake/spec get 2-3 where drift is likeliest.
    emit_banner = False
    banner_is_phase_entry = False
    if is_carrier:
        bt_phase, bt_session, bt_count = _read_banner_turn(cwd)
        if bt_phase != phase or bt_session != session_key:
            bt_count = 0  # phase/session changed -> fresh start, emit now
            banner_is_phase_entry = True
        emit_banner = bt_count % _adaptive_banner_cadence(phase, bt_count) == 0
        if mutate:
            try:
                _write_banner_turn_atomic(cwd, phase, session_key, bt_count + 1)
            except OSError:
                logger.debug("banner-turns write failed", exc_info=True)

    # Store handle for the gate AND the banner. Bound unconditionally on every
    # carrier turn: the approval gate and store-backed exit predicates must
    # evaluate on non-banner turns too — gating the bind on `emit_banner` made
    # `ctx.store` None between banner ticks, which skipped the approval branch
    # and let UNKNOWN fail the gate open (pipeline-collapse regression, 8f7f354).
    # Read-only here; a None handle degrades the banner to its plain per-phase
    # directive but still blocks approval-gated transitions.
    gate_store = _banner_store(cwd)

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
            trace_id=trace_id,
            banner=_banner_for_turn(
                emit_banner,
                phase,
                fallback_gates,
                cwd,
                slug=contract_slug,
                store=gate_store,
                is_phase_entry=banner_is_phase_entry,
                mutate=mutate,
            ),
            repo=repo,
            session_key=session_key,
            session_source=session_source,
        )

    signal_keywords: list[str] = skill.get("signal_keywords") or []
    exit_gates: dict[str, Any] = skill.get("exit_gates") or {}

    # Phase telemetry: record phase_start now that we know the skill loaded.
    # Prefer the app-state-scoped writer (task 04); fall back to a fresh
    # per-call writer over vector_store when the caller has none (tests, the
    # web signal playground).
    _phase_telemetry = (
        phase_telemetry
        if phase_telemetry is not None
        else (PhaseTelemetryWriter(vector_store) if vector_store is not None else None)
    )
    if _phase_telemetry is not None:
        try:
            _phase_telemetry.phase_start(
                trace_id,
                phase,
                model=skill.get("model"),
                workflow_skill_id=skill.get("skill_id"),
                success=True,
                repo=repo,
            )
        except Exception:  # noqa: BLE001 — soft-fail by design
            logger.debug("phase_start telemetry write failed", exc_info=True)

    # Per-turn banner (recency anchor). Built on a carrier turn that lands on the cadence
    # tick (`emit_banner`) under the active lifecycle mode + a valid phase; independent of
    # should_compose / announce / cursor, so it threads onto every return below — quiet
    # passthrough, compose, or no-skill. Soft: never raises.
    banner = _banner_for_turn(
        emit_banner,
        phase,
        exit_gates,
        cwd,
        slug=contract_slug,
        store=gate_store,
        is_phase_entry=banner_is_phase_entry,
        mutate=mutate,
    )

    # 3. Build predicate context
    # Pass the store handle so exit-gate predicates (contract_exists,
    # artifact_exists) and the approval gate evaluate against real store data
    # instead of failing open (UNKNOWN) or returning blind NOT_MET. gate_store
    # is bound unconditionally above — it must reach the gate on every carrier
    # turn, not just banner ticks.
    ctx = _build_predicate_context(
        project_root=cwd,
        phase=phase,
        prompt_text=task,
        session_key=session_key,
        store=gate_store,
        # Proxy has no file/tool events — only prompt text
    )

    # Register session in the registry if it's new. This makes session tracking
    # explicit and reliable, not dependent on fragile fingerprint detection.
    is_new_session = False
    if session_key and ctx.store is not None:
        try:
            existing = ctx.store.get_session(session_key)
            if existing is None:
                # New session - create it in the registry
                ctx.store.create_session(session_key, task_slug=None, phase=phase)
                logger.info("Registered new session: %s", session_key)
                is_new_session = True
            else:
                # Existing session - update activity
                ctx.store.update_session_activity(session_key, phase=phase)
        except Exception:
            logger.debug("Session registry update failed", exc_info=True)

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
    # Removed is_carrier gate: workflow instructions should inject on phase entry
    # regardless of session detection.
    announce = (phase_changed or session_key not in last_sessions) if session_key else phase_changed

    # 4b. Orientation cadence: the orientation marker fires once per (phase, session),
    #    BEFORE the workflow block. Uses its own cadence file (`_read_orientation_announced`
    #    / `_write_orientation_announced_atomic`) so the workflow announce and orientation
    #    cadence are independent — a degraded workflow announce never burns the orientation
    #    marker, and vice versa. Removed session_id requirement: orientation should fire
    #    for any identifiable session (header or fingerprint), enabling resume after
    #    interruption even when harnesses don't send session headers.
    last_orientation_phase, last_orientation_sessions = _read_orientation_announced(cwd)
    orientation_phase_changed = last_orientation_phase != phase
    # Fire orientation for any identifiable session (session_key from header or fingerprint).
    # This enables resume after interruption (power loss, etc.) even when harnesses
    # don't send explicit session_id headers.
    announce_orientation = bool(session_key) and (
        orientation_phase_changed or session_key not in last_orientation_sessions
    )

    # System-prompt leg of the workflow prose. Get workflow instructions from
    # the LangGraph. The graph starts at orientation and routes to the current
    # phase, embedding workflow instructions in its state.
    from agentalloy.signals.graph import (  # noqa: PLC0415
        initial_phase_graph_state,
        make_thread_key,
        phase_graph,
    )
    
    thread_key = make_thread_key(cwd)
    input_state = initial_phase_graph_state(phase=phase, lane="sdd-full")
    # Set should_transition=False to terminate after the current phase node.
    # We just want the workflow instructions for the current phase, not routing.
    input_state["should_transition"] = False
    input_state["to_phase"] = None
    
    graph = phase_graph()
    config = {"configurable": {"thread_id": thread_key.as_tuple()}}  # type: ignore[assignment]
    
    try:
        graph_result = graph.invoke(input_state, config=config)  # pyright: ignore[reportArgumentType, reportUnknownMemberType]
        graph_workflow_instructions = graph_result.get("workflow_instructions") if graph_result else None
    except Exception:
        logger.debug("Graph invocation failed, falling back to skill.raw_prose", exc_info=True)
        graph_workflow_instructions = None
    
    # Use graph's workflow_instructions, fall back to skill.raw_prose
    workflow_instructions = graph_workflow_instructions or (skill.get("raw_prose") or None)
    
    # Inject on phase entry, new session, or orientation. The agent needs workflow
    # instructions when entering a phase or starting a new session.
    should_inject_prose = phase_changed or is_new_session or announce_orientation or announce
    workflow_system_prose = workflow_instructions if should_inject_prose else None

    # 5. Transition trigger (reranker-primary intent, deterministic floor). Runs
    #    for every phase, including intake — there is no unconditional bypass. On
    #    a turn carrying no completion/approval signal the trigger does not fire,
    #    so an in-progress phase stays silent unless it is also an entry turn.
    #    Runs in a worker thread (like the gate eval below) so the reranker /
    #    embed network I/O never blocks the single uvicorn event loop.
    if seeded_this_turn:
        # The seeding turn orients — the repo just entered lifecycle
        # management and the agent needs its intake turn before any gate
        # runs. Evaluating here would let a trigger auto-advance the same
        # request that created the phase row.
        match = None
    else:
        match = await asyncio.to_thread(
            check_transition_trigger,
            signal_keywords,
            exit_gates,
            ctx,
            embed_client,
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
            lane = route_hint if route_hint else "sdd-full"
            
            # Intent-based contract creation: if we're in intake and the trigger fired,
            # auto-create the first contract before gate evaluation. This eliminates
            # the fragile dependency on the agent outputting HTML comment markers.
            if phase == INTAKE_PHASE and ctx.store is not None:
                # Generate a slug from the task (first 50 chars, sanitized)
                import re as _re
                task_text = task or "intake"
                slug = _re.sub(r'[^a-z0-9]+', '-', task_text.lower())[:50].strip('-')
                if not slug:
                    slug = "intake"
                
                # Check if a contract already exists for spec phase
                existing = ctx.store.list_contracts(phase="spec", status="active")
                if not existing:
                    # Auto-create the first contract for spec phase
                    contract_id = f"spec/{slug}"
                    try:
                        ctx.store.put_contract(
                            contract_id,
                            phase="spec",
                            slug=slug,
                            domain_tags=[],
                            scope_touches=[],
                            scope_avoids=[],
                            body=task or "",  # Use the task as the initial contract body
                        )
                        logger.info(
                            "Intent-based: auto-created first contract for phase=spec slug=%s",
                            slug,
                        )
                    except Exception:
                        logger.debug("intent-based contract creation failed", exc_info=True)
            # Evaluate gates and decide the transition. _route_step is the single
            # decision point for proxy/HTTP/CLI (slice 09).
            from agentalloy.signals.graph import (  # noqa: PLC0415
                _route_step,
            )

            # Pass project_root=cwd so evaluate_phase_gate resolves path-based
            # predicates and the approval gate against the real repo, not the
            # proxy's process cwd (the uv tool dir).
            out = _route_step(phase, lane, project_root=cwd, store=ctx.store)
            # Only write to_phase when the gate allows the transition.
            # When the gate blocks, stay put.
            to_phase = out.to_phase if out.should_transition else None
            if mutate and to_phase:
                # Design → plan migration: auto-copy design's tasks.md /
                # test-plan.md into plan so the plan gate is satisfied on first
                # entry.  Mirrors the CLI path's migration in phase.run_phase_set.
                if phase == "design" and to_phase == "plan":
                    try:
                        from agentalloy.install.subcommands.phase import (
                            _migrate_design_to_plan,
                        )

                        _migrate_design_to_plan(ctx.store)
                    except Exception:
                        logger.debug(
                            "design→plan migration failed — artifacts may be missing",
                            exc_info=True,
                        )
                try:
                    _write_phase_atomic(cwd, to_phase, session_key=session_key)
                    logger.info("Phase transition: %s -> %s", phase, to_phase)
                    # Auto-create next-phase contract if the transition warrants it.
                    # This runs AFTER _write_phase_atomic (which clears cursors), so
                    # we re-seed the cursor to the new contract if creation succeeds.
                    # Carry slug/scope forward from the current phase's work-item
                    # contract (cursor/sole-active fallback). Every transition must
                    # resolve it here: the intake intent branch only binds its local
                    # when it created the first spec contract, and that local must
                    # not shadow this path for spec→design and beyond.
                    source_contract_id, _ = _resolve_current_contract(
                        cwd, phase, session_key
                    )
                    _auto_create_next_contract(
                        cwd,
                        to_phase,
                        source_contract_id,
                        ctx.store,
                    )
                    # Rewrite enforcement posture for wired Tier A harnesses (D1–D9).
                    # mode="workflow" is not a guess: this whole branch is only
                    # reached past the pause guard above (1b), which returns
                    # early on `mode: paused` — an auto-advance never fires while
                    # the repo is in pause, so the mode here is always
                    # "workflow".
                    try:
                        from agentalloy.install.subcommands.wire_harness import (
                            rewrite_enforcement_posture,
                        )

                        rewrite_enforcement_posture(cwd, to_phase, mode="workflow")
                    except Exception:
                        logger.debug(
                            "posture rewrite failed after transition to %s",
                            to_phase,
                            exc_info=True,
                        )
                except OSError as e:
                    logger.warning("Failed to write phase file: %s", e)

            advisories = list(out.advisories)
            gates_met = list(out.gates_met)
            gates_unmet = list(out.gates_unmet)
            qwen_calls = out.qwen_calls

        await asyncio.to_thread(_run_gates)

    # 6b. AC feedback: evaluate the current work-item's success criteria against
    #     its phase artifacts and append a [agentalloy-gate-feedback] advisory
    #     when any criterion is unmet.
    ac_feedback = None
    if contract_id is not None and ctx.store is not None:
        try:
            from agentalloy.signals.predicates import _evaluate_ac_feedback

            contract_row = ctx.store.get_contract(contract_id)
            if contract_row is not None:
                ac_feedback = _evaluate_ac_feedback(ctx.store, contract_row)
        except Exception:
            logger.debug("AC feedback evaluation failed", exc_info=True)

    if ac_feedback is not None:
        advisories.append(ac_feedback)

    # Did any semantic gate / transition-trigger intent hit an embed failure this
    # turn? Read off the shared ctx (the trigger and the gates each ran in a
    # worker thread via to_thread — both mutate the same diagnostics sink, and
    # both have already joined). Carried into telemetry so a silently-degraded
    # gate is queryable instead of only a WARNING line.
    phase_gate_embed_failed = ctx.embed_failed

    # State leg: structured JSON context briefing for the LLM. Built on every
    # carrier turn (same cadence as banner). Soft: never raises — a failure
    # yields None and the leg is simply not injected.
    state_leg_text: str | None = None
    if is_carrier and phase:
        try:
            state_leg_text = build_state_leg(
                phase,
                paused_mode=paused_mode,
                store=ctx.store,
                contract_id=contract_id,
                gates_met=gates_met,
                gates_unmet=gates_unmet,
                repo_root=cwd,
            )
        except Exception:
            logger.debug("state_leg build failed for phase=%s", phase, exc_info=True)

    # 7. Tier 2 cadence: decide whether the current work-item contract's domain block
    #    fires. `contract_id`/`contract_path` were resolved once near the top of this
    #    function (shared with the banner's <slug>). Tier 2 fires when the cursor changed
    #    since we last composed it — on phase entry (the incoming contract becomes current)
    #    or an `agentalloy task next`. Domain retrieval is keyed to the contract's task,
    #    NEVER the workflow's static process tags (which only ever emptied results).
    # Same carrier gate as Tier 1: a tool-less background request must not burn the
    # work-item cursor marker (which would silently drop the domain block from the
    # real turn that follows).
    announce_cursor = is_carrier and contract_id is not None and _read_composed(cwd) != contract_id

    # 7b. Phase-boundary confirm directives (phase-boundary-confirmation). Purely
    #     deterministic (phase + on-disk delivery record + new-session detection);
    #     no phase write. The ship-ask persists every ship turn (ship never
    #     self-advances); the new-session confirm fires only while this session is
    #     unoriented for the phase — i.e. it rides the SAME (phase, session) marker
    #     as `announce` (announce is always True when `new_session` holds), so the
    #     announce commit records the session and the confirm goes quiet next turn.
    #     `not phase_changed` excludes the same-session-advanced-here case (that's a
    #     phase entry, oriented via Tier 1, not a stale-phase resume to confirm).
    new_session = bool(
        is_carrier and session_key and session_key not in last_sessions and not phase_changed,
    )
    confirm_directives = _boundary_confirm_directives(
        cwd,
        phase,
        artifact_slug=contract_slug,
        new_session=new_session,
        # Same carrier gate as `new_session`/`announce`: a background tool-less
        # request must not fire or burn any marker (orientation-carrier-request-race).
        phase_changed=phase_changed and is_carrier,
        transitioned_by=transitioned_by,
        session_key=session_key,
    )

    # 8. Decide. Inject when this is a phase-entry turn (Tier 1), a new work-item
    #    turn (Tier 2), the eval produced advisories, OR a boundary confirm is due.
    #    None → quiet passthrough.
    if not (announce or announce_cursor or advisories or confirm_directives):
        # A quiet turn. When a clean transition fired this turn (phase written, no
        # advisory), carry the gate metadata so telemetry still records the eval
        # even though nothing is injected — the new phase announces next turn.
        return SignalResult(
            should_compose=False,
            phase=phase,
            task=task,
            trace_id=trace_id,
            banner=banner,
            state_leg=state_leg_text,
            # Quiet for composition, NOT quiet for the system leg: this is the return
            # taken on every turn after the first of a phase, and it is precisely where
            # the workflow instructions used to vanish.
            workflow_system_prose=workflow_system_prose,
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

    # Orientation cadence: build the pending tuple so commit_markers can write it.
    pending_orientation: tuple[str, list[str]] | None = None
    if announce_orientation and session_key:
        if orientation_phase_changed:
            orient_sessions = [session_key]
        else:
            orient_sessions = [*last_orientation_sessions, session_key][-_MAX_ANNOUNCED_SESSIONS:]
        pending_orientation = (phase, orient_sessions)

    # 6c. Gate feedback artifact injection: read any gate_feedback artifact
    #     from the store and append it to advisories. Soft — failures yield
    #     None and don't break the proxy request.
    if ctx.store is not None and contract_slug is not None:
        try:
            gate_fb = ctx.store.get_artifact(
                phase,
                contract_slug,
                "gate_feedback",
                status="active",
            )
            if gate_fb and gate_fb.get("content"):
                advisories.append(f"[agentalloy-gate-feedback] {gate_fb['content']}")
        except Exception:
            logger.debug("Gate feedback artifact read failed", exc_info=True)

    return SignalResult(
        should_compose=True,
        announce=announce,
        workflow_prose=skill.get("raw_prose") if announce else None,
        workflow_system_prose=workflow_system_prose,
        workflow_skill_id=(skill.get("skill_id") or None) if announce else None,
        current_contract=contract_id if announce_cursor else None,
        announce_cursor=announce_cursor,
        trace_id=trace_id,
        phase=phase,
        task=task,
        banner=banner,
        state_leg=state_leg_text,
        pre_filter_matched=match.detail if match is not None else None,
        gates_met=gates_met,
        gates_unmet=gates_unmet,
        qwen_calls=qwen_calls,
        advisories=advisories,
        confirm_directives=confirm_directives,
        phase_gate_embed_failed=phase_gate_embed_failed,
        repo=repo,
        session_key=session_key,
        session_source=session_source,
        pending_announce=pending_announce,
        pending_composed=pending_composed,
        announce_orientation=announce_orientation,
        pending_orientation=pending_orientation,
    )


def _boundary_confirm_directives(
    cwd: Path,
    phase: str | None,
    *,
    artifact_slug: str | None = None,
    new_session: bool,
    phase_changed: bool = False,
    transitioned_by: str | None = None,
    session_key: str | None = None,
) -> list[str]:
    """Deterministic phase-boundary confirm prompts (phase-boundary-confirmation).

    Three boundaries, all pure reads (never write the phase file), returned as a
    single coherent directive list — at most one prompt, never two conflicting
    MUST blocks:

    - **T1 ship completion** — when phase==ship and a ``delivery`` artifact
      exists in the store for the current work-item slug, emit a confirm
      directive asking the user whether to reset to intake.  This fires
      mid-workflow, *before* merge — the agent confirms delivery is complete
      before the watcher is engaged.
    - **T2 new-session resume** — when a *new* session (its key not yet oriented
      for this phase) resumes on a non-intake phase, confirm that phase is correct
      before adopting it: the per-repo phase file is contended by concurrent
      sessions, so a stale mid-``build`` resume is exactly worth confirming.
    - **T3 swept by another session** — an *already-oriented* session observes the
      phase changed (``phase_changed``) since it last looked, AND the recorded
      ``transitioned_by`` names a *different* known session. Ordinary self-driven
      advancement (this session's own turn passed the exit gate) is excluded: at
      the moment `phase_changed` is computed, a same-turn transition hasn't
      "aged" into an observed jump yet (that shows up as `new_session`/quiet Tier
      1 orientation instead) — this only fires on the FIRST subsequent turn where
      a jump is visible and attributable to someone else. `transitioned_by` is
      ``None`` (ambiguous — e.g. a bare CLI ``phase set`` outside a tracked
      session) doesn't fire this; only a concrete, different session id does, to
      keep false positives near zero. Like `new_session`, this must not fire on
      a background tool-less request — the caller passes ``phase_changed=False``
      for a non-carrier turn (same gate as `new_session`'s own `is_carrier` check).

    `new_session` and the swept case are mutually exclusive by construction
    (`new_session` requires `not phase_changed`), so there's no ordering conflict
    between them. Precedence when either combines with ship-landed: a single
    combined prompt — confirm the phase, then ask about the reset — never two
    blocks.
    """
    # T4 intake confirmation — fires when intake is active and a NEW session
    # resumes. The agent must confirm it should stay on intake before proceeding.
    if phase is not None and phase == INTAKE_PHASE:
        if new_session:
            return [
                "You are resuming a NEW session on phase `intake` (the entry phase). "
                "Before doing any work, create a contract for the `spec` phase "
                "and PRESENT it in full and STOP — do not draft "
                "solutions. The user will approve and advance the phase.",
            ]
        return []

    # T1 ship completion — check store for a delivery artifact.
    # Ship writes the ``delivery`` artifact to the store (not disk).
    # If it exists, emit a confirm directive asking the user whether to reset
    # to intake. This fires every ship turn (ship is terminal — never self-advances).
    if phase == "ship" and artifact_slug is not None:
        store = _banner_store(cwd)  # repo+stream-scoped handle, soft (None on outage)
        if store is not None:
            try:
                delivery = store.get_artifact("ship", artifact_slug, "delivery", status="active")
                if delivery is not None and delivery.get("content"):
                    return [
                        "Delivery landed — ASK the user whether to reset to intake.",
                    ]
            except Exception:
                logger.debug("Delivery artifact check failed (ship completion)", exc_info=True)

    # Ship watcher emits reset markers via lifecycle.set_merged() →
    # .agentalloy/reset-to-intake-<slug>.pending. The proxy picks these up
    # via the intake-phase new_session path (the watcher sets phase=ship,
    # the reset marker tells the agent to ask the user, then LLM runs
    # `agentalloy phase set intake`).

    swept = bool(
        phase_changed and session_key and transitioned_by and transitioned_by != session_key,
    )

    if new_session:
        if phase == "ship":
            return [
                "You are resuming a NEW session and the phase is `ship`. First "
                "CONFIRM with the user that `ship` is the right phase to be on; "
                "if it is, check for a reset marker file in `.agentalloy/` — if one "
                "exists, ASK whether they are ready to reset to intake for the next "
                "work item. Do NOT change the phase "
                "on your own initiative — wait for their answer.",
            ]
        return [
            f"You are resuming a NEW session on phase `{phase}` (not intake). Before "
            f"doing this phase's work, CONFIRM with the user that `{phase}` is the "
            "correct phase to resume — the per-repo phase file can be left stale by a "
            "prior or concurrent session. Do NOT change the phase on your own initiative.",
        ]

    if swept:
        if phase == "ship":
            return [
                "The phase changed to `ship` since your last turn here — a different "
                "concurrent session on this repo advanced it, not this one. First "
                "CONFIRM with the user that `ship` is the right phase to be on; if it "
                "is, check for a reset marker file in `.agentalloy/` — if one exists, "
                "ASK whether they are ready to reset to intake for the next work "
                "item. Do NOT change the phase on your "
                "own initiative — wait for their answer.",
            ]
        return [
            f"The phase changed to `{phase}` since your last turn here — a different "
            "concurrent session on this repo advanced it, not this one. CONFIRM with "
            f"the user that `{phase}` is the correct phase to continue on before doing "
            "this phase's work. Do NOT change the phase on your own initiative.",
        ]

    return []


def commit_markers(
    project_root: Path,
    signal: SignalResult,
    *,
    announce_emitted: bool,
    cursor_emitted: bool,
    orientation_emitted: bool = False,
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
    - ``orientation_emitted``: the orientation marker block carried real text and was
      injected → commit ``pending_orientation`` to the orientation cadence file so the
      orientation marker does not re-fire this session.

    No-op when the corresponding ``pending_*`` is unset (the signal did not decide
    to announce / advance the cursor / orient this turn).
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
    if orientation_emitted and signal.pending_orientation is not None:
        phase, sessions = signal.pending_orientation
        try:
            _write_orientation_announced_atomic(project_root, phase, sessions)
        except OSError as e:
            logger.warning("Failed to write orientation announced file: %s", e)
