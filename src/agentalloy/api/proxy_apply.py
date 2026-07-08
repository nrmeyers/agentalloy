"""Shared inject+commit seam for both proxy surfaces.

Both live proxy surfaces — the native Anthropic passthrough
(``/proj/<token>/v1/messages``) and the OpenAI-compatible chat-completions
endpoint — run an identical ``compose → inject → commit_markers`` cycle. The
*decision* logic (``evaluate_signal`` in :mod:`agentalloy.api.proxy_signal`) is
already shared; this module unifies the *inject + commit* wiring so both
surfaces share one cadence-marker implementation.

:func:`apply_signal` composes the 3-tier block once, hands the text to a
surface-specific ``inject`` callable, and returns an :class:`InjectOutcome`
carrying the injected payload plus the per-tier emit flags — but it no longer
commits the cadence markers itself. Committing is deferred to
:func:`commit_outcome`, which the surface calls *after the upstream forward* and
only on a 2xx response: composing text the request then drops (no user message,
malformed content) must NOT burn the marker, and neither must a turn the model
never processed because upstream was overloaded (529) or errored. The compose
helper (:func:`_compose_block`) and its result (:class:`_ComposedBlock`) live
here too, imported back by the passthrough router.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentalloy.api import code_index_gate
from agentalloy.api.compose_models import ComposedResult, ComposeRequest, EmptyResult, Phase
from agentalloy.api.proxy_signal import SignalResult, commit_markers

if TYPE_CHECKING:
    from agentalloy.orchestration.compose import ComposeOrchestrator

logger = logging.getLogger(__name__)

_VALID_PHASES = ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast", "add-skill")


def _tier2_k() -> int | None:
    """Explicit per-work-item k for the Tier-2 domain leg.

    ``AGENTALLOY_TIER2_K`` overrides (clamped to ``[1, 50]``); ``None`` defers to
    the phase default (post-E1, build=4). Lets the Tier-2 per-work-item leg be
    tuned independently of direct ``/compose`` calls. A malformed or empty value
    falls back to ``None`` (phase default).
    """
    raw = os.environ.get("AGENTALLOY_TIER2_K")
    if raw:
        try:
            return max(1, min(50, int(raw)))
        except ValueError:
            return None
    return None


@dataclass
class ProxyComposeTelemetry:
    """Skill/fragment provenance for one proxy request, merged across both compose
    tiers, so the surface can write a single consolidated trace row instead of
    losing it to the orchestrator's (now-suppressed) per-leg writes.

    - ``workflow_skill_ids`` / ``header_fragment_ids``: the Tier 1 orientation
      "header" — the phase's workflow skill plus its system-prose fragments.
    - ``returned_skill_ids``: the Tier 2 domain skills actually injected.
    - ``selected_fragment_ids``: every fragment injected (Tier 1 system + Tier 2 domain).
    - ``tokens_returned`` / ``tokens_flat_equivalent``: summed across both tiers.
    - ``lm_assist_*``: Stage B detail from the Tier 2 domain leg (Stage B never runs
      on the system leg).
    - ``contract_path`` / ``contract_tags``: Tier-2 contract provenance, carried
      from the ``compose_request_from_contract`` request so production rows are
      auditable for contract-scoped vs free-text injection. Null/empty on every
      free-text path by construction (those never build a contract request).
    """

    workflow_skill_ids: list[str]
    header_fragment_ids: list[str]
    returned_skill_ids: list[str]
    selected_fragment_ids: list[str]
    tokens_returned: int
    tokens_flat_equivalent: int
    reranked: bool
    dense_leg_degraded: bool
    lm_assist_outcome: str
    lm_assist_model: str | None
    lm_assist_kept_ids: list[str]
    lm_assist_dropped_ids: list[str]
    lm_assist_scores: dict[str, float]
    # Compose latency (ms), summed across both tiers, from the orchestrator's per-leg
    # LatencyBreakdown. ``None`` when neither leg composed (passthrough). assembly is
    # omitted — it's structurally 0 since generative assembly was removed.
    retrieval_latency_ms: int | None = None
    total_latency_ms: int | None = None
    contract_path: str | None = None
    contract_tags: list[str] = field(default_factory=list)


@dataclass
class _ComposedBlock:
    """Result of :func:`_compose_block`: the text plus per-tier commit signals.

    These report what was *composed*. The caller pairs them with whether the block
    was actually injected (delivery) before committing a marker — composing text the
    request then drops (no user message, malformed content) must NOT burn the marker.

    - ``tier1_text``: the Tier 1 orientation block carried real text — its marker may
      be committed once that text is delivered.
    - ``cursor_terminal``: the Tier 2 domain leg reached a *terminal* state (delivered
      skills OR composed to a clean empty result, NOT a transient compose error). A
      cleanly-empty Tier 2 has nothing to deliver, so its cursor commits even without
      an injection — that is what stops a contract with genuinely no domain skills
      from re-firing every turn.
    - ``cursor_text``: the Tier 2 leg produced non-empty domain text — when True the
      cursor marker additionally requires delivery, so an undelivered domain block
      re-fires next turn instead of being silently lost.
    """

    text: str
    tier1_text: bool
    cursor_terminal: bool
    cursor_text: bool
    telemetry: ProxyComposeTelemetry


async def _compose_block(signal: SignalResult, orchestrator: ComposeOrchestrator) -> _ComposedBlock:
    """Compose the prose block to inject.

    Three independent parts, each gated separately:

    - **Eval advisory** — emitted whenever the gate eval produced advisories
      (a transition trigger fired). Light; may recur across turns; carries no marker.
    - **Tier 1 (phase-entry announce)** — the workflow skill's operating prose for
      the phase + its phase-scoped system prose. Emitted once per phase entry
      (``signal.announce``). How to operate here; never carries domain skills.
    - **Tier 2 (per work-item)** — the domain skills for the current work-item
      contract (``signal.current_contract``), keyed off its task, not the phase.
      Emitted once per work-item (``signal.announce_cursor``): phase entry, or an
      ``agentalloy task next``.

    Returns a :class:`_ComposedBlock` whose ``text`` is the parts joined (``""``
    when none has content) and whose flags tell the caller which cadence markers
    are safe to commit post-injection.

    Free-flow (``signal.free_mode``) takes the compose-only branch instead: no
    advisory / Tier 1 / Tier 2, just the task-keyed domain leg plus the daily
    reminder line (see :func:`_compose_free_block`).
    """
    if signal.free_mode:
        return await _compose_free_block(signal, orchestrator)

    phase = signal.phase
    compose_phase: Phase = phase if phase in _VALID_PHASES else "build"  # type: ignore[assignment]

    advisory_block = ""
    if signal.advisories:
        advisory_block = (
            "[agentalloy-eval]\n" + "\n".join(signal.advisories) + "\n[/agentalloy-eval]"
        )

    # Tier 1: workflow prose (operating instructions) + system-only compose.
    # ``record_trace=False`` suppresses the orchestrator's own per-leg write — this
    # surface folds both legs into one consolidated trace row below.
    tier1 = ""
    tier1_result: ComposedResult | EmptyResult | None = None
    if signal.announce:
        parts: list[str] = []
        if signal.workflow_prose:
            parts.append(signal.workflow_prose.strip())
        try:
            system_req = ComposeRequest(
                task=signal.task or f"Entering {compose_phase}.",
                phase=compose_phase,
                legs="system",
            )
            tier1_result = await orchestrator.compose(
                system_req,
                repo=signal.repo,
                session_key=signal.session_key,
                session_source=signal.session_source,
                record_trace=False,
                # Availability gate: sys-code-index is dropped unless this repo
                # actually has a completed code index (fail-closed on any doubt).
                exclude_system_skill_ids=code_index_gate.system_skill_exclusions(signal.repo),
            )
            if not isinstance(tier1_result, EmptyResult) and tier1_result.output:
                parts.append(tier1_result.output)
        except Exception:
            logger.warning("Tier 1 system compose failed -- workflow prose only", exc_info=True)
        tier1 = "\n\n".join(parts)

    # Tier 2: domain skills for the current work-item contract. `tier2_terminal`
    # distinguishes "composed to a clean result" (delivered text OR a legitimate
    # empty — the cursor is done) from "the compose leg threw" (transient — leave
    # the cursor unmarked so it re-fires next turn).
    tier2 = ""
    tier2_terminal = False
    tier2_result: ComposedResult | EmptyResult | None = None
    domain_req: ComposeRequest | None = None
    if signal.announce_cursor and signal.current_contract:
        try:
            from agentalloy.api.compose_models import compose_request_from_contract
            from agentalloy.contracts import parse_contract

            contract = parse_contract(Path(signal.current_contract))
            domain_req = compose_request_from_contract(contract, legs="domain", k=_tier2_k())
            tier2_result = await orchestrator.compose(
                domain_req,
                repo=signal.repo,
                session_key=signal.session_key,
                session_source=signal.session_source,
                record_trace=False,
            )
            tier2 = "" if isinstance(tier2_result, EmptyResult) else tier2_result.output
            tier2_terminal = True
        except Exception:
            logger.warning("Tier 2 domain compose failed -- passing through", exc_info=True)
            tier2 = ""
            tier2_terminal = False

    text = "\n\n".join(p for p in (advisory_block, tier1, tier2) if p)
    return _ComposedBlock(
        text=text,
        tier1_text=bool(tier1),
        cursor_terminal=tier2_terminal,
        cursor_text=bool(tier2),
        telemetry=_merge_compose_telemetry(signal, tier1_result, tier2_result, domain_req),
    )


async def _compose_free_block(
    signal: SignalResult, orchestrator: ComposeOrchestrator
) -> _ComposedBlock:
    """Compose the free-flow (compose-only) block.

    Two parts, both riding the standard injection block:

    - **Domain leg** — the domain skills retrieved for the request's task text
      (``signal.task``), gated on ``signal.announce`` (once per session, on the
      free sentinel cadence). No workflow prose, no system leg, no banner.
    - **Reminder** — the once-per-24h "workflow paused" line (already
      cadence-stamped by the signal layer).

    Marker semantics: ``tier1_text`` is True on a *terminal* domain compose
    (delivered skills OR a clean empty result — mirrors the workflow-mode cursor
    semantics), so the per-session free marker commits once the block is
    delivered and a transient compose error re-fires next turn. The Tier 2
    cursor channel is never used in free mode.
    """
    phase = signal.phase
    compose_phase: Phase = phase if phase in _VALID_PHASES else "build"  # type: ignore[assignment]

    domain = ""
    domain_terminal = False
    domain_result: ComposedResult | EmptyResult | None = None
    if signal.announce and signal.task:
        try:
            domain_req = ComposeRequest(
                task=signal.task,
                phase=compose_phase,
                legs="domain",
                k=_tier2_k(),
            )
            domain_result = await orchestrator.compose(
                domain_req,
                repo=signal.repo,
                session_key=signal.session_key,
                session_source=signal.session_source,
                record_trace=False,
            )
            domain = "" if isinstance(domain_result, EmptyResult) else domain_result.output
            domain_terminal = True
        except Exception:
            logger.warning("free-flow domain compose failed -- passing through", exc_info=True)

    text = "\n\n".join(p for p in (signal.reminder or "", domain) if p)
    return _ComposedBlock(
        text=text,
        tier1_text=domain_terminal,
        cursor_terminal=False,
        cursor_text=False,
        telemetry=_merge_compose_telemetry(signal, None, domain_result),
    )


def _merge_compose_telemetry(
    signal: SignalResult,
    tier1: ComposedResult | EmptyResult | None,
    tier2: ComposedResult | EmptyResult | None,
    tier2_request: ComposeRequest | None = None,
) -> ProxyComposeTelemetry:
    """Fold the Tier 1 (system/header) and Tier 2 (domain) compose results into one
    provenance record. Stage B fields come from Tier 2 only — it never runs on the
    system leg. Missing legs (passthrough) contribute nothing.

    ``tier2_request`` carries the contract provenance (``contract_path`` /
    ``contract_tags``) the request objects already hold but the results do not;
    it is recorded only when the Tier-2 leg actually composed (``tier2`` set),
    so a thrown compose never stamps contract fields on a row without skills."""
    t1 = tier1.telemetry if tier1 is not None else None
    t2 = tier2.telemetry if tier2 is not None else None
    workflow_ids = list(t1.workflow_skill_ids) if t1 else []
    if signal.workflow_skill_id and signal.workflow_skill_id not in workflow_ids:
        workflow_ids.append(signal.workflow_skill_id)
    header_fragment_ids = list(tier1.system_fragments) if tier1 is not None else []
    returned_skill_ids = list(tier2.source_skills) if tier2 is not None else []
    selected_fragment_ids = header_fragment_ids + (
        list(tier2.domain_fragments) if tier2 is not None else []
    )
    # Latency lives on the result (ComposedResult.latency_ms), not on .telemetry, and
    # only ComposedResult carries it — an EmptyResult / missing leg contributes nothing.
    lat1 = tier1.latency_ms if isinstance(tier1, ComposedResult) else None
    lat2 = tier2.latency_ms if isinstance(tier2, ComposedResult) else None
    if lat1 is None and lat2 is None:
        retrieval_latency_ms = total_latency_ms = None  # untimed (distinct from 0ms)
    else:
        retrieval_latency_ms = (lat1.retrieval_ms if lat1 else 0) + (
            lat2.retrieval_ms if lat2 else 0
        )
        total_latency_ms = (lat1.total_ms if lat1 else 0) + (lat2.total_ms if lat2 else 0)
    return ProxyComposeTelemetry(
        workflow_skill_ids=workflow_ids,
        header_fragment_ids=header_fragment_ids,
        returned_skill_ids=returned_skill_ids,
        selected_fragment_ids=selected_fragment_ids,
        tokens_returned=(t1.tokens_returned if t1 else 0) + (t2.tokens_returned if t2 else 0),
        tokens_flat_equivalent=(
            (t1.tokens_flat_equivalent if t1 else 0) + (t2.tokens_flat_equivalent if t2 else 0)
        ),
        reranked=bool(t2.reranked) if t2 else False,
        dense_leg_degraded=bool(t2 and t2.dense_leg_degraded) or bool(t1 and t1.dense_leg_degraded),
        lm_assist_outcome=t2.lm_assist_outcome if t2 else "disabled",
        lm_assist_model=t2.lm_assist_model if t2 else None,
        lm_assist_kept_ids=list(t2.lm_assist_kept_ids) if t2 else [],
        lm_assist_dropped_ids=list(t2.lm_assist_dropped_ids) if t2 else [],
        lm_assist_scores=dict(t2.lm_assist_scores) if t2 else {},
        retrieval_latency_ms=retrieval_latency_ms,
        total_latency_ms=total_latency_ms,
        contract_path=(
            tier2_request.contract_path if tier2_request is not None and tier2 is not None else None
        ),
        contract_tags=(
            list(tier2_request.contract_tags or [])
            if tier2_request is not None and tier2 is not None
            else []
        ),
    )


@dataclass
class InjectOutcome[T]:
    """Result of :func:`apply_signal`: the injected payload + deferred commit facts.

    The surface threads this across the upstream forward and hands it to
    :func:`commit_outcome` once the response status is known, so a marker is
    written only when the model actually received the block (a 2xx response). The
    emit flags already fold in *delivery* (the block reached the request body); the
    2xx gate is applied later, by :func:`commit_outcome`.

    - ``injected``: whatever ``inject`` returned — the new request/payload, or None
      on a no-op (nothing composed, or the block could not be injected).
    - ``announce_emitted`` / ``cursor_emitted``: candidate Tier 1 / Tier 2 commits,
      pending a 2xx forward.
    """

    injected: T | None
    signal: SignalResult
    announce_emitted: bool
    cursor_emitted: bool
    # Merged skill/fragment provenance for the consolidated proxy trace row.
    telemetry: ProxyComposeTelemetry


async def apply_signal[T](
    *,
    signal: SignalResult,
    orchestrator: ComposeOrchestrator,
    inject: Callable[[str], T | None],
    delivered: Callable[[T], bool],
) -> InjectOutcome[T]:
    """Shared inject seam for both proxy surfaces (commit is deferred).

    Composes the 3-tier block, injects it via the surface-specific ``inject``
    (which returns the new request/payload, or None on a no-op), and returns an
    :class:`InjectOutcome` with the per-tier emit flags folded against delivery.
    It does NOT write the cadence markers — the surface calls :func:`commit_outcome`
    after the upstream forward, gated on a 2xx response, so a turn the model never
    processed (overloaded/errored upstream) leaves the cadence intact and re-fires
    on the harness retry.
    """
    composed = await _compose_block(signal, orchestrator)
    if not composed.text:
        return InjectOutcome(
            injected=None,
            signal=signal,
            announce_emitted=False,
            cursor_emitted=False,
            telemetry=composed.telemetry,
        )
    injected = inject(composed.text)
    was_delivered = injected is not None and delivered(injected)
    return InjectOutcome(
        injected=injected,
        signal=signal,
        announce_emitted=composed.tier1_text and was_delivered,
        cursor_emitted=composed.cursor_terminal and (was_delivered or not composed.cursor_text),
        telemetry=composed.telemetry,
    )


def commit_outcome(project_root: Path, outcome: InjectOutcome[Any], *, upstream_ok: bool) -> None:
    """Commit the deferred cadence markers — only after a confirmed 2xx forward.

    ``upstream_ok`` is the surface's verdict that upstream returned 2xx (the model
    processed the injected block). A non-2xx (529 overloaded, 5xx, connection error)
    leaves ``.agentalloy/{announced,composed}`` untouched, so ``evaluate_signal``
    re-announces on the harness's retry instead of silently dropping orientation.
    No-op when nothing was injected (both emit flags False).
    """
    if not upstream_ok:
        return
    commit_markers(
        project_root,
        outcome.signal,
        announce_emitted=outcome.announce_emitted,
        cursor_emitted=outcome.cursor_emitted,
    )
