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
    "design": "build",
    "build": "qa",
    "qa": "ship",
    "sdd-fast": "qa",  # fast lane: compressed spec+design+build, then merge into
    #                    the standard qa → ship verification + delivery
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
        )

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
