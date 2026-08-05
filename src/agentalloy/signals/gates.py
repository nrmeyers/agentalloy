"""Gate aggregation and phase-transition decisions.

SDD phase graph (linear): intake → spec → design → build → qa → ship
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from agentalloy.embed_provider import EmbedClient
from agentalloy.signals.predicates import (
    PREDICATES,
    PredicateContext,
    PredicateResult,
    _glob_files,  # pyright: ignore[reportPrivateUsage]
    _read_file,  # pyright: ignore[reportPrivateUsage]
    evaluate_predicate,
)

# The entry phase. A freshly-wired repo starts here so the intake (intent
# interview) workflow composes on the first prompt; it bypasses the
# signal-keyword pre-filter (see api/proxy_signal) and hands off to "spec"
# via _PHASE_GRAPH.
INTAKE_PHASE = "intake"

# Linear SDD phase graph: phase → next phase
_PHASE_GRAPH: dict[str, str] = {
    "intake": "spec",  # entry phase: default (full) route. The fast route
    #                    overrides this with a next_phase_hint of "sdd-fast".
    "spec": "design",
    "design": "plan",  # design settles approach/decisions/rationale only; plan
    #                    decomposes it into tasks, test cases, and one build
    #                    contract per task (the density floor lives on plan→build)
    "plan": "build",
    "build": "qa",
    "qa": "ship",
    "sdd-fast": "qa",  # fast lane: compressed spec+design+build, then merge into
    #                    the standard qa → ship verification + delivery
    "add-skill": "intake",  # custom-skill authoring lane: deliverable is a locally
    #                         installed corpus skill, not a shippable change — return
    #                         to intake for whatever comes next
    "ship": "ship",  # terminal
}


@dataclass(frozen=True)
class GateEvaluation:
    gate_name: str
    result: PredicateResult
    detail: str = ""
    advisory: str | None = None


@dataclass(frozen=True)
class PhaseTransitionDecision:
    should_transition: bool
    from_phase: str
    to_phase: str | None
    gates_met: list[GateEvaluation]
    gates_unmet: list[GateEvaluation]
    qwen_calls: int
    advisories: list[str] = field(default_factory=lambda: list[str]())


def _build_completeness_advisory(args: dict[str, Any], ctx: PredicateContext) -> str | None:
    """Build an advisory string for artifact_completeness (soft advisory, never hard gate)."""
    path_pattern: str = args.get("path", "")
    criteria_text: str = args.get("criteria", "")
    if not path_pattern or not criteria_text:
        return None
    try:
        files = _glob_files(ctx.project_root, path_pattern)
        if not files:
            return None
        content = _read_file(files[0]) or ""
        return (
            f"[agentalloy-eval] Soft completeness check — does this artifact meet the bar?\n"
            f"Criteria: {criteria_text}\n\n"
            f"{content[:3000]}"
        )
    except Exception:
        return None


def _build_approval_advisory(ctx: PredicateContext) -> str:
    """Present-and-STOP nudge for a complete-but-unapproved phase.

    ``approval_recorded`` carries no ``path`` glob, so the missing-path advisory in
    :func:`decide_transition` stays silent for it. Attach this on the leaf eval so a
    phase that is done but awaiting human sign-off doesn't block without explanation.
    """
    phase = ctx.current_phase or "this phase"
    return (
        f"'{phase}' is complete and awaiting human approval. PRESENT the work in full and STOP; "
        f"run `agentalloy approve {phase}` only after the user explicitly approves (re-run it if the "
        f"exit artifact changed after the last approval)."
    )


def _build_contract_coverage_advisory(args: dict[str, Any], ctx: PredicateContext) -> str | None:
    """Advisory for ``build_contracts_cover_tasks`` NOT_MET (the §6 density floor).

    Cursor-scoped (#378) to match the predicate: counts against the active design
    work-item's tasks.md and its own build contracts, so the numbers reported are
    the same ones the gate judged (never the repo aggregate)."""
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        _count_task_items,
        _item_build_contracts,
        _resolve_workitem_slug,
    )

    slug = _resolve_workitem_slug(ctx, str(args.get("phase") or "design"))
    if slug is None:
        return None
    tasks_glob: str = args.get("tasks", "docs/design/{slug}/tasks.md").replace("{slug}", slug)

    # Legacy glob tolerance: pass through if present (traces deprecation)
    contracts_glob: str | None = args.get("contracts")

    try:
        tasks = 0
        for f in _glob_files(ctx.project_root, tasks_glob):
            tasks += _count_task_items(_read_file(f) or "")
        tasks = max(1, tasks)
        contracts = len(_item_build_contracts(ctx, slug, contracts_glob=contracts_glob))
    except Exception:
        return None
    return (
        f"Design emitted {contracts} build contract(s) for {tasks} task(s) in tasks.md. "
        f"Emit ONE build contract per task before advancing to build — "
        f"`agentalloy contract init --phase build --slug <NN-task-slug>`, each centered on a "
        f"single tech surface."
    )


def _build_tag_focus_advisory(args: dict[str, Any], ctx: PredicateContext) -> str | None:
    """Advisory for ``build_contract_tag_focus`` NOT_MET — name the over-tagged contracts.

    Cursor-scoped (#378): names only the active work-item's offenders, matching the
    predicate, so a sibling item's wide-tag contract is neither judged nor named."""
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        _item_build_contracts,
        _resolve_workitem_slug,
    )

    # Legacy glob tolerance: pass through if present (traces deprecation)
    contracts_glob: str | None = args.get("contracts")
    max_tags: int = args.get("max_tags", 2)
    slug = _resolve_workitem_slug(ctx, str(args.get("phase") or "design"))
    if slug is None:
        return None
    try:
        offenders: list[str] = []
        for c in _item_build_contracts(ctx, slug, contracts_glob=contracts_glob):
            tags = c.get("domain_tags") or []
            if len(tags) > max_tags:
                name = c.get("slug", c.get("contract_id", "unknown"))
                offenders.append(f"{name} ({len(tags)} tags)")
    except Exception:
        return None
    if not offenders:
        return None
    listed = ", ".join(sorted(offenders))
    return (
        f"Over-tagged build contract(s): {listed}. Each build contract MUST carry <={max_tags} "
        f"domain_tags centered on ONE dominant tech surface — at the build retrieval cap, more "
        f"surfaces truncate the fragments that matter. Split into per-surface tasks (e.g. a 7-tag "
        f"calendar contract -> date-layer [calendar], scaffold [vite, react], components "
        f"[react, css-grid], tests [vitest])."
    )


def _is_composite(spec: dict[str, Any]) -> bool:
    return any(k in spec for k in ("all_of", "any_of", "not"))


def _evaluate_single(
    predicate_name: str,
    args: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient | None,
    qwen_calls: list[int],
) -> PredicateResult:
    if predicate_name in PREDICATES:
        return evaluate_predicate(predicate_name, args, ctx)
    from agentalloy.signals.classifier import SEMANTIC_PREDICATES

    if predicate_name in SEMANTIC_PREDICATES:
        if lm_client is None:
            return PredicateResult.UNKNOWN
        from agentalloy.config import get_settings

        model = get_settings().runtime_embedding_model
        result = SEMANTIC_PREDICATES[predicate_name](args, ctx, lm_client, model)
        # Only count actual embed calls; artifact_completeness returns UNKNOWN without calling embed.
        if predicate_name != "artifact_completeness":
            qwen_calls[0] += 1
        return result
    raise ValueError(
        f"Unknown predicate '{predicate_name}'. "
        f"Available: {sorted(list(PREDICATES) + list(SEMANTIC_PREDICATES))}"
    )


def evaluate_node(
    spec: Any,
    ctx: PredicateContext,
    lm_client: EmbedClient | None,
    qwen_calls: list[int],
    depth: int = 0,
) -> tuple[PredicateResult, list[GateEvaluation]]:
    """Recursively evaluate a gate node. Returns (result, list of GateEvaluation)."""
    if not isinstance(spec, dict):
        return PredicateResult.UNKNOWN, []

    spec_d: dict[str, Any] = cast(dict[str, Any], spec)

    # Composite operators
    if "all_of" in spec_d:
        children: list[Any] = cast(list[Any], spec_d["all_of"])
        results: list[PredicateResult] = []
        evals: list[GateEvaluation] = []
        for child in children:
            r, sub_evals = evaluate_node(child, ctx, lm_client, qwen_calls, depth + 1)
            evals.extend(sub_evals)
            results.append(r)
            if r == PredicateResult.NOT_MET:
                # Short-circuit
                return PredicateResult.NOT_MET, evals
        # Any UNKNOWN (with no NOT_MET) → UNKNOWN
        if any(r == PredicateResult.UNKNOWN for r in results):
            return PredicateResult.UNKNOWN, evals
        return PredicateResult.MET, evals

    if "any_of" in spec_d:
        children = cast(list[Any], spec_d["any_of"])
        results = []
        evals = []
        for child in children:
            r, sub_evals = evaluate_node(child, ctx, lm_client, qwen_calls, depth + 1)
            evals.extend(sub_evals)
            results.append(r)
            if r == PredicateResult.MET:
                return PredicateResult.MET, evals
        if any(r == PredicateResult.UNKNOWN for r in results):
            return PredicateResult.UNKNOWN, evals
        return PredicateResult.NOT_MET, evals

    if "not" in spec_d:
        child: Any = spec_d["not"]
        r, evals = evaluate_node(child, ctx, lm_client, qwen_calls, depth + 1)
        if r == PredicateResult.MET:
            return PredicateResult.NOT_MET, evals
        if r == PredicateResult.NOT_MET:
            return PredicateResult.MET, evals
        return PredicateResult.UNKNOWN, evals

    # Leaf predicate: {predicate_name: args_dict}
    keys: list[str] = [k for k in spec_d if k not in ("all_of", "any_of", "not")]
    if not keys:
        return PredicateResult.UNKNOWN, []

    predicate_name: str = keys[0]
    raw_args = spec_d[predicate_name]
    args: dict[str, Any] = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}

    advisory: str | None = None
    if predicate_name == "artifact_completeness":
        advisory = _build_completeness_advisory(args, ctx)

    try:
        result = _evaluate_single(predicate_name, args, ctx, lm_client, qwen_calls)
    except ValueError:
        result = PredicateResult.UNKNOWN

    if predicate_name == "approval_recorded" and result == PredicateResult.NOT_MET:
        advisory = _build_approval_advisory(ctx)
    elif predicate_name == "build_contracts_cover_tasks" and result == PredicateResult.NOT_MET:
        advisory = _build_contract_coverage_advisory(args, ctx)
    elif predicate_name == "build_contract_tag_focus" and result == PredicateResult.NOT_MET:
        advisory = _build_tag_focus_advisory(args, ctx)

    eval_record = GateEvaluation(
        gate_name=predicate_name,
        result=result,
        detail=str(args),
        advisory=advisory,
    )
    return result, [eval_record]


def aggregate(operator: str, children: list[PredicateResult]) -> PredicateResult:
    """Aggregate a list of PredicateResult values with the given operator."""
    if operator == "all_of":
        if any(r == PredicateResult.NOT_MET for r in children):
            return PredicateResult.NOT_MET
        if any(r == PredicateResult.UNKNOWN for r in children):
            return PredicateResult.UNKNOWN
        return PredicateResult.MET
    if operator == "any_of":
        if any(r == PredicateResult.MET for r in children):
            return PredicateResult.MET
        if any(r == PredicateResult.UNKNOWN for r in children):
            return PredicateResult.UNKNOWN
        return PredicateResult.NOT_MET
    if operator == "not":
        if not children:
            return PredicateResult.UNKNOWN
        r = children[0]
        if r == PredicateResult.MET:
            return PredicateResult.NOT_MET
        if r == PredicateResult.NOT_MET:
            return PredicateResult.MET
        return PredicateResult.UNKNOWN
    return PredicateResult.UNKNOWN


def _near_miss_candidates(root: Path, strict_glob: str) -> list[str]:
    """Files that look like the gate's deliverable but landed at the wrong path.

    For a *file-style* glob (final component is ``*.<ext>``), search the whole
    tree for files carrying the glob's most-specific literal directory token and
    matching extension — e.g. ``docs/spec/*.md`` searches ``**/*spec*.md`` and
    finds a misplaced ``linkvault-spec.md`` at the repo root. Anything the strict
    glob already matches is excluded. Returns project-root-relative paths, sorted.

    Empty for directory-style globs (``src/**``, ``tests/**``) where "wrong path"
    isn't meaningful, and when no literal directory token can be derived.

    Excludes ``archive/`` and ``_superseded/`` directories — those are
    historical contracts, not "wrote it somewhere wrong" candidates.
    """
    parts = [p for p in strict_glob.split("/") if p]
    if not parts:
        return []
    leaf = parts[-1]
    if "." not in leaf:  # bare ** or * — directory-style, skip
        return []
    ext = leaf.rsplit(".", 1)[-1]
    if not ext or any(c in ext for c in "*?[]"):
        return []
    # Most-specific literal directory token (last dir component without a glob char).
    token = ""
    for comp in parts[:-1]:
        if not any(c in comp for c in "*?[]"):
            token = comp
    if not token:
        return []
    strict_matches = {p.resolve() for p in _glob_files(root, strict_glob)}
    candidates: list[str] = []
    for p in _glob_files(root, f"**/*{token}*.{ext}"):
        if p.resolve() in strict_matches:
            continue
        # Exclude archive/_superseded directories — historical, not "wrong path"
        rel = p.relative_to(root)
        parts = rel.parts
        if any(part == "archive" or part.startswith("_") for part in parts):
            continue
        # Exclude directories that are clearly not plausible "wrong location"
        # for the expected deliverable. A file in eval/tests/fixtures is never
        # a misplaced spec/design/build contract.
        excluded_dirs = {"eval", "tests", "fixtures", "docs/fast", "docs/eval", "docs/design"}
        if any(part in excluded_dirs for part in parts):
            continue
        try:
            candidates.append(str(p.relative_to(root)))
        except ValueError:
            candidates.append(str(p))
    return sorted(candidates)


def decide_transition(
    current_phase: str,
    gate_spec: dict[str, Any],
    ctx: PredicateContext,
    lm_client: EmbedClient | None = None,
    next_phase_hint: str | None = None,
) -> PhaseTransitionDecision:
    """Evaluate gates and decide whether to transition to the next phase."""
    qwen_calls: list[int] = [0]
    result, all_evals = evaluate_node(gate_spec, ctx, lm_client, qwen_calls)

    gates_met = [e for e in all_evals if e.result == PredicateResult.MET]
    gates_unmet = [e for e in all_evals if e.result != PredicateResult.MET]
    advisories: list[str] = [e.advisory for e in all_evals if e.advisory is not None]

    should_transition = result == PredicateResult.MET
    to_phase = next_phase_hint or _PHASE_GRAPH.get(current_phase)

    # The trigger fired (decide_transition is only called after a transition
    # trigger matches), but the deterministic guard isn't satisfied. Tell the
    # agent WHICH required exit artifact is missing rather than silently staying
    # put. Only name paths that genuinely don't exist on disk — a block caused
    # by a soft/semantic check on a file that already exists shouldn't read as
    # "produce this file".
    if not should_transition and current_phase != to_phase:
        from agentalloy.signals.prefilter import (
            _extract_gate_paths,  # pyright: ignore[reportPrivateUsage]
            _extract_gate_store_specs,  # pyright: ignore[reportPrivateUsage]
        )

        required = dict.fromkeys(_extract_gate_paths(gate_spec))
        missing = [p for p in required if not _glob_files(ctx.project_root, p)]

        # Store-backed artifacts (spec/design, post-migration): no filesystem
        # glob to check, so report what's missing by querying the store instead.
        store_specs = dict.fromkeys(_extract_gate_store_specs(gate_spec))
        for phase_name, name_glob in store_specs:
            rows = ctx.store.list_artifacts(phase_name, name_glob=name_glob) if ctx.store else []
            if not rows:
                advisories.append(
                    f"Phase '{current_phase}' isn't complete yet, so staying in "
                    f"'{current_phase}'. To advance to '{to_phase}', record its exit "
                    f"artifact: `agentalloy contract artifact-set --phase {phase_name} "
                    f"--slug <slug> --name {name_glob}`."
                )
        # Split missing paths into "wrote it somewhere wrong" vs "doesn't exist at
        # all". A near-miss (the deliverable exists but at the wrong path — e.g.
        # `linkvault-spec.md` at the repo root vs the gate's `docs/spec/*.md`) gets
        # a sharper, actionable advisory naming where to move it.
        generic: list[str] = []
        for p in missing:
            near = _near_miss_candidates(ctx.project_root, p)
            if near:
                found = ", ".join(f"`{c}`" for c in near[:3])
                advisories.append(
                    f"Found {found}, but phase '{current_phase}' expects its exit "
                    f"artifact at `{p}`. Move or rename it there to advance to "
                    f"'{to_phase}'."
                )
            else:
                generic.append(p)
        if generic:
            paths = ", ".join(f"`{p}`" for p in generic)
            advisories.append(
                f"Phase '{current_phase}' isn't complete yet, so staying in "
                f"'{current_phase}'. To advance to '{to_phase}', produce its exit "
                f"artifact(s): {paths}."
            )

    return PhaseTransitionDecision(
        should_transition=should_transition,
        from_phase=current_phase,
        to_phase=to_phase if should_transition else None,
        gates_met=gates_met,
        gates_unmet=gates_unmet,
        qwen_calls=qwen_calls[0],
        advisories=advisories,
    )


# ---------------------------------------------------------------------------
# Phase gate verdict helper (slice 09: state-phase-gate)
# ---------------------------------------------------------------------------

# Approval-since paths keyed by the phase they're associated with.
# These are globs for the phase's exit artifacts — the approval marker
# (.agentalloy/approved/<phase>) must be newer than any matching file.
#
# spec/design moved to the artifact store (specs/final_migration.md); their
# staleness check is a store-side name_glob (_APPROVAL_STORE_NAME_GLOB), not a
# filesystem glob. sdd-fast/add-skill are unmigrated and keep the disk glob.
_APPROVAL_SINCE: dict[str, str] = {
    "sdd-fast": "docs/fast/*.md",
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}
# MUST mirror each pack's `approval_recorded: since_name_glob`. This map and the
# pack are two sources of truth for the same set, and `run_approve` digests via
# this map while the gate re-digests via the pack's arg — so any disagreement
# records an approval digest the gate can never reproduce, and the phase stays
# blocked while the CLI reports success. test_approval_globs_match_packs pins it.
_APPROVAL_STORE_NAME_GLOB: dict[str, str] = {
    "spec": "*.md",
    # design produces exactly approach.md post-split; narrower than "*.md" on
    # purpose, so a leftover pre-split tasks.md under phase=design can't shift
    # the digest.
    "design": "approach.md",
    # plan produces tasks.md + test-plan.md, so "*.md" covers both.
    "plan": "*.md",
}


def evaluate_phase_gate(
    current_phase: str | None,
    target_phase: str,
    project_root: Path | None,
    override: bool,
    store: Any = None,
) -> dict[str, Any] | None:
    """Evaluate the exit gate for a phase transition.

    Returns a verdict dict when the gate blocks, or ``None`` when the
    transition is allowed.  The verdict carries ``result``, ``reason``,
    and ``advisories`` (missing-artifact paths) so the CLI can render
    operator-facing guidance without re-evaluating the gate.

    Rules:
    - Approval gate: forward transitions out of approval-gated phases
      (spec/design/add-skill) require a recorded human approval marker
      (unforgeable by --force).
    - ``override=True`` skips the forward gate (artifact completeness)
      but NOT the approval gate.
    - If ``project_root`` is ``None`` (no repo context), the gate is
      skipped and the write is allowed (fail open).
    - If ``current_phase`` is ``None`` (fresh repo), the gate is skipped.
    - If the phase hasn't changed (same-phase write), the gate is skipped.
    - ``UNKNOWN`` fails open — an embed-server outage must not wedge
      phase writes.
    """
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        PredicateContext,
        PredicateResult,
        approval_required,
        eval_approval_recorded,
    )

    # Same-phase write — no gate needed
    if current_phase is None or current_phase == target_phase:
        return None

    # Approval gate: forward transitions out of approval-gated phases
    # require a recorded human approval marker. --force does NOT bypass
    # this checkpoint.
    if target_phase == _PHASE_GRAPH.get(current_phase) and approval_required(current_phase):
        approval_blocked = False
        if current_phase in _APPROVAL_STORE_NAME_GLOB and project_root:
            name_glob = _APPROVAL_STORE_NAME_GLOB[current_phase]
            if store is not None:
                # Always run the approval predicate — an empty scoped row set
                # must BLOCK here (the Tier 2 approval checkpoint survives
                # --force), not defer to the forward gate that override skips
                # (#516). eval_approval_recorded itself returns NOT_MET when
                # nothing is produced/approvable.
                ctx = PredicateContext(
                    project_root=project_root, current_phase=current_phase, store=store
                )
                result = eval_approval_recorded({"since_name_glob": name_glob}, ctx)
                approval_blocked = result == PredicateResult.NOT_MET
        else:
            since = _APPROVAL_SINCE.get(current_phase, "")
            # Exit artifact doesn't exist yet — skip approval gate, let the
            # forward (completeness) gate handle it.
            if since and project_root and any(p.is_file() for p in project_root.glob(since)):
                ctx = PredicateContext(
                    project_root=project_root, current_phase=current_phase, store=store
                )
                result = eval_approval_recorded({"since": since}, ctx)
                approval_blocked = result == PredicateResult.NOT_MET
        if approval_blocked:
            return {
                "result": "approval",
                "reason": "approval",
                "advisories": [
                    f"'{current_phase}' requires human approval before advancing "
                    f"to '{target_phase}'. Run `agentalloy approve {current_phase}` "
                    f"once the user has approved."
                ],
            }

    # Override flag: skip the forward (completeness) gate
    if override:
        return None

    # Forward gate: evaluate the current phase's exit gates
    # Only for forward transitions — backward/bail/non-linear are unguarded
    from agentalloy.signals.skill_loader import (  # noqa: PLC0415
        exit_gates_for_phase,
    )

    if target_phase != _PHASE_GRAPH.get(current_phase):
        return None  # backward / bail / non-linear → unguarded

    gate_spec = exit_gates_for_phase(current_phase)
    if not gate_spec:
        return None  # no packaged gate for this phase

    ctx = PredicateContext(
        project_root=project_root or Path.cwd(),
        current_phase=current_phase or "",
        store=store,
    )
    result, _ = evaluate_node(gate_spec, ctx, lm_client=None, qwen_calls=[0])

    # Only NOT_MET blocks — UNKNOWN fails open (embed outage must not wedge).
    if result != PredicateResult.NOT_MET:
        return None  # MET or UNKNOWN — allow

    # Use decide_transition for human-readable advisory text (near-miss paths,
    # missing-artifact guidance). It re-evaluates deterministically (lm_client=None).
    decision = decide_transition(current_phase, gate_spec, ctx, lm_client=None)
    return {
        "result": "not_met",
        "reason": decision.advisories[0] if decision.advisories else "Exit gate not met",
        "advisories": decision.advisories or [],
    }
