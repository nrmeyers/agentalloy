# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
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
    _glob_files,
    _read_file,
    evaluate_predicate,
)

# Lazy import alias — imported by evaluate_node / decide_transition /
# evaluate_phase_gate.  Kept here to avoid a circular import with graph.py
# (graph.py imports evaluate_phase_gate from this module at module level).
_NEXT: dict[str, str] | None = None
# Legacy alias — gates.py used to own _PHASE_GRAPH; tests and other modules
# still import it from here.  Resolved via __getattr__ (no module-level
# assignment — __getattr__ fires only for missing names).


def __getattr__(name: str):
    """Lazily resolve _PHASE_GRAPH when any module-level name is accessed."""
    if name in ("_PHASE_GRAPH", "_NEXT"):
        return _get_next()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_next() -> dict[str, str]:
    """Return the next-phase map, lazily loaded from graph on first call."""
    global _NEXT
    if _NEXT is None:
        # __getattr__ should have populated this, but guard for direct imports.
        from agentalloy.signals.graph import _NEXT as _next_map  # noqa: N811

        _NEXT = _next_map  # type: ignore[assignment]
    return _NEXT


# The entry phase. A freshly-wired repo starts here so the intake (intent
# interview) workflow composes on the first prompt; it bypasses the
# signal-keyword pre-filter (see api/proxy_signal) and hands off to "spec".
INTAKE_PHASE = "intake"


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
        f"the phase advances only after the user explicitly approves. If the exit artifact "
        f"changed after the last approval, present the updated work for re-approval."
    )


def _build_artifact_missing_advisory(args: dict[str, Any], ctx: PredicateContext) -> str:
    """Advisory for ``artifact_exists`` NOT_MET — name the store endpoint.

    The exit artifact is a store record, not a file; the advisory must point at
    the recording endpoint so the agent doesn't invent a filesystem path.
    """
    phase = str(args.get("phase") or ctx.current_phase or "this phase")
    name = str(args.get("name") or args.get("name_glob") or "<name>.artifact")
    return (
        f"Phase '{phase}' isn't complete yet: its exit artifact '{name}' is not "
        f"recorded. Record it by piping the body straight to the store: "
        f"agentalloy artifact put --phase {phase} --slug <task-slug> "
        f"--name {name} (content on stdin; PUT /state/artifact is the HTTP "
        f"fallback). A file on disk is not an artifact — the store is the "
        f"artifact's only home. Then present the work for approval."
    )


def _build_artifact_sections_advisory(args: dict[str, Any], ctx: PredicateContext) -> str:
    """Advisory for ``artifact_contains`` NOT_MET — name the missing headings.

    The fix for the bare-'not_met' block: the artifact exists but lacks required
    section headings, and without this advisory the agent cannot tell which. Names
    the artifact, the exact missing headings, and the re-record path — including
    the consequence (re-record voids the approval digest, so re-approval follows).
    """
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        _resolve_workitem_slug_for,
        _section_present,
    )

    phase = str(args.get("phase") or ctx.current_phase or "this phase")
    name = str(args.get("name") or args.get("name_glob") or "<name>.artifact")
    sections = list(args.get("sections") or [])
    if ctx.store is None:
        return _build_artifact_missing_advisory(args, ctx)
    slug = _resolve_workitem_slug_for(ctx.store, ctx.project_root, phase)
    rows = ctx.store.list_artifacts(phase, slug=slug, name_glob=name)
    if not rows:
        return _build_artifact_missing_advisory(args, ctx)
    body = rows[0].get("content", "")
    missing = [s for s in sections if not _section_present(body, s)] or sections
    names = ", ".join(f"'## {s}'" for s in missing)
    return (
        f"The '{name}' artifact is recorded but missing required section heading(s): "
        f"{names}. Update the body to include those headings and re-record it: "
        f"agentalloy artifact put --phase {phase} --slug <task-slug> "
        f"--name {name} (content on stdin; PUT /state/artifact is the HTTP "
        f"fallback). Re-recording voids any existing approval for '{phase}', so "
        f"present the updated artifact for re-approval."
    )


def _build_artifact_size_advisory(args: dict[str, Any], ctx: PredicateContext) -> str:
    """Advisory for ``artifact_size_min`` NOT_MET — the artifact is too thin."""
    phase = str(args.get("phase") or ctx.current_phase or "this phase")
    glob = str(args.get("name_glob") or args.get("name") or "*.artifact")
    minimum = args.get("minimum_size", 0)
    return (
        f"Phase '{phase}' exit artifact matching '{glob}' is below the minimum "
        f"size ({minimum} chars). Flesh out the artifact body and re-record it: "
        f"agentalloy artifact put --phase {phase} --slug <task-slug> "
        f"--name {glob} (content on stdin; PUT /state/artifact is the HTTP "
        f"fallback)."
    )


def _build_contract_coverage_advisory(args: dict[str, Any], ctx: PredicateContext) -> str | None:
    """Advisory for ``build_contracts_cover_tasks`` NOT_MET (the §6 density floor).

    Cursor-scoped (#378) to match the predicate: counts against the active
    work-item's tasks (store artifact when ``tasks_from_store``, else the disk
    glob) and its own build contracts, so the numbers reported are the same
    ones the gate judged (never the repo aggregate).
    """
    from agentalloy.signals.predicates import (  # noqa: PLC0415
        _count_task_items,
        _item_build_contracts,
        _resolve_workitem_slug,
    )

    phase = str(args.get("phase") or "design")
    slug = _resolve_workitem_slug(ctx, phase)
    if slug is None:
        return None

    # Legacy glob tolerance: pass through if present (traces deprecation)
    contracts_glob: str | None = args.get("contracts")

    try:
        tasks = 0
        if args.get("tasks_from_store") and ctx.store is not None:
            tasks_name = str(args.get("tasks_artifact_name") or "tasks.artifact")
            artifact = ctx.store.get_artifact(phase, slug, tasks_name)
            if artifact is not None and artifact.get("content") is not None:
                tasks = _count_task_items(artifact["content"])
        else:
            tasks_glob: str = args.get("tasks", "docs/design/{slug}/tasks.md").replace(
                "{slug}", slug
            )
            for f in _glob_files(ctx.project_root, tasks_glob):
                tasks += _count_task_items(_read_file(f) or "")
        tasks = max(1, tasks)
        contracts_list = _item_build_contracts(ctx, slug, contracts_glob=contracts_glob)
        if contracts_list is None:
            return None  # store error -> fail-open (no advisory)
        contracts = len(contracts_list)
    except Exception:
        return None
    return (
        f"{contracts} build contract(s) recorded for {tasks} counted task(s) — "
        f"one build contract per task is required before advancing (POST "
        f"/contracts with work_item '{slug}'; each centered on ONE dominant "
        f"tech surface, <=2 domain_tags). Tasks are counted as top-level "
        f"bullets under the tasks artifact's '## Tasks' heading — '### T-N' "
        f"headings count as zero."
    )


def _build_contract_exists_advisory(args: dict[str, Any], ctx: PredicateContext) -> str:
    """Advisory for ``contract_exists`` NOT_MET during intake.

    When the intake phase's ``contract_exists`` gate fires but no contracts exist
    in the next phase, tell the agent the concrete action: use the advance
    endpoint (it writes the next-phase contract and advances in one call —
    the agent-facing way to create the first contract), then present it for
    approval. This gives intake's gate forward gear instead of echoing the
    posture, by pointing at a real tool the agent has (anomaly D-1).
    """
    target_phase = str(args.get("phase", "spec"))
    to_phase = _get_next().get(target_phase, target_phase)
    return (
        f"You are in intake, but no contracts exist for phase '{target_phase}'. "
        f"Create it with your state panel's advance action: POST /state/advance "
        f"with `slug`, `contract_body` (a concrete restatement of what the user "
        f"wants), and `to_phase` '{target_phase}'. "
        f"Fill the body with a concrete restatement of what the user wants, then "
        f"PRESENT it in full and STOP — do not draft solutions. "
        f"The phase advances to '{to_phase}' once the user approves."
    )


def _build_tag_focus_advisory(args: dict[str, Any], ctx: PredicateContext) -> str | None:
    """Advisory for ``build_contract_tag_focus`` NOT_MET — name the over-tagged contracts.

    Cursor-scoped (#378): names only the active work-item's offenders, matching the
    predicate, so a sibling item's wide-tag contract is neither judged nor named.
    """
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
        contracts_list = _item_build_contracts(ctx, slug, contracts_glob=contracts_glob)
        if contracts_list is None:
            return None  # store error -> fail-open (no advisory)
        for c in contracts_list:
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
        f"Available: {sorted(list(PREDICATES) + list(SEMANTIC_PREDICATES))}",
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

    if predicate_name == "contract_exists" and result == PredicateResult.NOT_MET:
        advisory = _build_contract_exists_advisory(args, ctx)
    elif predicate_name == "approval_recorded" and result == PredicateResult.NOT_MET:
        advisory = _build_approval_advisory(ctx)
    elif predicate_name == "artifact_exists" and result == PredicateResult.NOT_MET:
        advisory = _build_artifact_missing_advisory(args, ctx)
    elif predicate_name == "artifact_contains" and result == PredicateResult.NOT_MET:
        advisory = _build_artifact_sections_advisory(args, ctx)
    elif predicate_name == "artifact_size_min" and result == PredicateResult.NOT_MET:
        advisory = _build_artifact_size_advisory(args, ctx)
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
    target_phase: str | None = None,
) -> PhaseTransitionDecision:
    """Evaluate gates and decide whether to transition to the next phase.

    When ``target_phase`` is provided it is used as the next phase directly.
    When omitted, ``_NEXT`` (imported from ``graph.py``) is used for the
    standard linear route.
    """
    qwen_calls: list[int] = [0]
    result, all_evals = evaluate_node(gate_spec, ctx, lm_client, qwen_calls)

    gates_met = [e for e in all_evals if e.result == PredicateResult.MET]
    gates_unmet = [e for e in all_evals if e.result != PredicateResult.MET]
    advisories: list[str] = [e.advisory for e in all_evals if e.advisory is not None]

    should_transition = result == PredicateResult.MET
    if target_phase is not None:
        to_phase = target_phase
    elif next_phase_hint is not None:
        to_phase = next_phase_hint
    else:
        # Lazy import to avoid circular import with graph.py (which imports
        # evaluate_phase_gate from this module at module level).
        from agentalloy.signals.graph import (  # noqa: PLC0415
            _PHASE_GRAPH,
        )

        to_phase = _PHASE_GRAPH.get(current_phase)

    # A same-phase "transition" (terminal ship, or a no-op write) has nowhere
    # to advance to — suppress the leaf advisories so the agent isn't nagged
    # about producing an exit artifact for a phase that doesn't leave.
    if current_phase == to_phase:
        advisories = []

    # The trigger fired (decide_transition is only called after a transition
    # trigger matches), but the deterministic guard isn't satisfied. Tell the
    # agent WHICH required exit artifact is missing rather than silently staying
    # put. Only name paths that genuinely don't exist on disk — a block caused
    # by a soft/semantic check on a file that already exists shouldn't read as
    # "produce this file".
    if not should_transition and current_phase != to_phase:
        from agentalloy.signals.prefilter import (  # noqa: PLC0415
            _extract_gate_paths,
        )

        # Store-backed artifacts are covered by the leaf-level advisories
        # (_build_artifact_missing_advisory / _build_artifact_sections_advisory);
        # this branch handles the remaining disk-path gates.
        required = dict.fromkeys(_extract_gate_paths(gate_spec))
        missing = [p for p in required if not _glob_files(ctx.project_root, p)]
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
                    f"'{to_phase}'.",
                )
            else:
                generic.append(p)
        if generic:
            paths = ", ".join(f"`{p}`" for p in generic)
            advisories.append(
                f"Phase '{current_phase}' isn't complete yet, so staying in "
                f"'{current_phase}'. To advance to '{to_phase}', produce its exit "
                f"artifact(s): {paths}.",
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
# spec/design/plan/sdd-fast moved to the artifact store (specs/final_migration.md);
# their staleness check is a store-side name_glob (_APPROVAL_STORE_NAME_GLOB), not
# a filesystem glob.
#
# add-skill is the ONE remaining disk entry and stays deliberately: its deliverable
# is a custom-skill pack YAML authored by `agentalloy new-skill-pack`, which is
# tool-written configuration, not a lifecycle artifact an agent hand-writes.
_APPROVAL_SINCE: dict[str, str] = {
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}
# MUST mirror each pack's `approval_recorded: since_name_glob`. This map and the
# pack are two sources of truth for the same set, and `run_approve` digests via
# this map while the gate re-digests via the pack's arg — so any disagreement
# records an approval digest the gate can never reproduce, and the phase stays
# blocked while the CLI reports success. test_approval_globs_match_packs pins it.
_APPROVAL_STORE_NAME_GLOB: dict[str, str] = {
    "spec": "spec.artifact",
    # design produces exactly approach.artifact post-split; narrower than
    # "*.artifact" on purpose, so a leftover pre-split tasks.artifact under
    # phase=design can't shift the digest.
    "design": "approach.artifact",
    # plan produces tasks.artifact + test-plan.artifact, so "*.artifact" covers both.
    "plan": "*.artifact",
    # sdd-fast's one collapsed artifact; matches the pack's artifact_exists leaf.
    "sdd-fast": "fast.artifact",
}


def _is_forward_skip(current_phase: str, target_phase: str) -> bool:
    """True when the transition jumps *forward* over one or more main-chain
    phases (e.g. ``spec → build``, skipping ``design`` and ``plan``).

    The SDD flow is a linear pipeline — you advance one phase at a time — so a
    forward skip is never a valid transition and is blocked by
    :func:`evaluate_phase_gate`. Backward moves (``qa → build``), bail routes
    (``sdd-fast → spec``), and resets (``ship → intake``) are *not* skips; they
    fall through to the unguarded path.

    Lane phases (``sdd-fast`` / ``add-skill`` / ``sdd-flow``) sit off the main
    chain; their only forward edge is their declared linear next (``_NEXT``), so
    a move involving a lane endpoint is never treated as a main-chain skip here.
    """
    rank = {
        "intake": 0,
        "spec": 1,
        "design": 2,
        "plan": 3,
        "build": 4,
        "qa": 5,
        "ship": 6,
    }
    cur = rank.get(current_phase)
    tgt = rank.get(target_phase)
    if cur is None or tgt is None:
        return False  # a lane endpoint → not a main-chain skip
    # A skip is a forward move that is not the immediate linear next
    # (``tgt == cur + 1`` is the linear next; ``tgt > cur + 1`` skips ≥1 phase).
    return tgt > cur + 1


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
    - Forward-skip guard: a transition that jumps forward over one or more
      main-chain phases (e.g. spec → build) is refused — the flow advances one
      phase at a time. Not waivable by ``override`` (unlike the completeness
      gate). Backward / bail / reset moves are not skips and stay unguarded.
    - Approval gate: forward transitions out of approval-gated phases
      (spec/design/add-skill) require a recorded human approval marker
      (unforgeable by --force). For store-backed approval phases, an
      unreachable store (``store is None``) blocks fail-closed — an
      un-checkable approval checkpoint must not waive itself.
    - ``override=True`` skips the forward gate (artifact completeness)
      but NOT the approval gate (nor the forward-skip guard).
    - If ``project_root`` is ``None`` (no repo context), the gate is
      skipped and the write is allowed (fail open).
    - If ``current_phase`` is ``None`` (fresh repo), the gate is skipped.
    - If the phase hasn't changed (same-phase write), the gate is skipped.
    - The completeness gate treats ``UNKNOWN`` as allow — an embed-server
      outage must not wedge phase writes. That fail-open policy applies
      ONLY to the artifact completeness gate, never to the approval gate.
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

    # Forward-skip guard: the SDD flow is a linear pipeline — you advance one
    # phase at a time. A transition that jumps forward over one or more main-chain
    # phases (e.g. spec → build, skipping design + plan) is never a valid
    # transition and is blocked here. This closes the hole where a non-linear
    # forward target fell through the "backward / bail / non-linear → unguarded"
    # early-returns below and bypassed BOTH the approval gate and the exit-artifact
    # completeness gate — the bug that let a workflow advance spec → build with no
    # design/plan artifacts.
    #
    # Deliberately NOT waived by ``override`` (--force): --force exists to waive an
    # incomplete *artifact* on a legitimate linear advance, not to skip phases of the
    # workflow. Backward, bail, and reset moves are not forward skips and fall
    # through to the unguarded path below.
    if _is_forward_skip(current_phase, target_phase):
        return {
            "result": "forward_skip",
            "reason": "forward_skip",
            "advisories": [
                f"Refusing to advance '{current_phase}' → '{target_phase}': that skips "
                f"one or more workflow phases. The SDD flow advances one phase at a "
                f"time — run the intermediate phase(s) in order. (Not waivable by "
                f"--force.)"
            ],
        }

    # Approval gate: forward transitions out of approval-gated phases
    # require a recorded human approval marker. --force does NOT bypass
    # this checkpoint.
    if target_phase == _get_next().get(current_phase) and approval_required(current_phase):
        approval_blocked = False
        store_unreachable = False
        if current_phase in _APPROVAL_STORE_NAME_GLOB and project_root:
            name_glob = _APPROVAL_STORE_NAME_GLOB[current_phase]
            if store is not None:
                # Always run the approval predicate — an empty scoped row set
                # must BLOCK here (the Tier 2 approval checkpoint survives
                # --force), not defer to the forward gate that override skips
                # (#516). eval_approval_recorded itself returns NOT_MET when
                # nothing is produced/approvable.
                ctx = PredicateContext(
                    project_root=project_root,
                    current_phase=current_phase,
                    store=store,
                )
                result = eval_approval_recorded({"since_name_glob": name_glob}, ctx)
                approval_blocked = result == PredicateResult.NOT_MET
            else:
                # Fail closed: this phase's approval gate is store-backed and
                # cannot run at all without a store handle. Allowing the
                # advance would waive the human checkpoint — the hole behind
                # the pipeline-collapse regression (8f7f354), where a None
                # store on non-banner turns let every approval gate silently
                # pass. An un-checkable checkpoint must block, not waive
                # itself. Not waivable by override (checked below).
                approval_blocked = True
                store_unreachable = True
        else:
            since = _APPROVAL_SINCE.get(current_phase, "")
            # Exit artifact doesn't exist yet — skip approval gate, let the
            # forward (completeness) gate handle it.
            if since and project_root and any(p.is_file() for p in project_root.glob(since)):
                ctx = PredicateContext(
                    project_root=project_root,
                    current_phase=current_phase,
                    store=store,
                )
                result = eval_approval_recorded({"since": since}, ctx)
                approval_blocked = result == PredicateResult.NOT_MET
        if approval_blocked:
            advisory = (
                f"'{current_phase}' requires human approval before advancing "
                f"to '{target_phase}'. PRESENT the work in full and STOP; when "
                f"the user approves, advance via your state panel's advance "
                f"action (POST /state/advance) with `approved: true`."
            )
            if store_unreachable:
                advisory = (
                    f"'{current_phase}' requires human approval before advancing "
                    f"to '{target_phase}', and the state store is unreachable, so "
                    f"the approval check cannot run. Blocking fail-closed — an "
                    f"un-checkable approval gate must not waive itself. "
                    f"Retry once the state store is available."
                )
            return {
                "result": "approval",
                "reason": "approval",
                "advisories": [advisory],
            }

    # Override flag: skip the forward (completeness) gate
    if override:
        return None

    # Forward gate: evaluate the current phase's exit gates
    # Only for forward transitions — backward/bail/non-linear are unguarded
    from agentalloy.signals.skill_loader import (  # noqa: PLC0415
        exit_gates_for_phase,
    )

    if target_phase != _get_next().get(current_phase):
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
    decision = decide_transition(
        current_phase,
        gate_spec,
        ctx,
        lm_client=None,
        target_phase=target_phase,
    )
    return {
        "result": "not_met",
        "reason": decision.advisories[0] if decision.advisories else "Exit gate not met",
        "advisories": decision.advisories or [],
    }
