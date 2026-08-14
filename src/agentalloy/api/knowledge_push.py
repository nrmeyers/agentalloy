"""Knowledge slice 2 — the just-in-time decision manifest (AC 6).

Given a design/build work-item's contract, the repo's code-index graph, and the
tier-2 text already composed this turn, select the decisions governing code in the
contract's ``scope.touches`` and render them as a compact manifest — decision
headings and source paths only, no snippet bodies.  The model must pull full
rationale via ``agentalloy knowledge why <fqn>``.

Deterministic, no LLM, no network. This module holds the pure selection/render
logic (it takes an opened graph store); the compose seam (``proxy_apply``) owns the
fire-gate (phase ∈ {design,build} ∧ cursor-entry ∧ ``code_index_available``) and
the lazy-imported read-handle open/close, per the code-index import discipline.
"""

from __future__ import annotations

# ``_sanitize_skill_id`` is reused cross-module on purpose; suppress private-usage
# reporting for this module rather than at the call site.
# pyright: reportPrivateUsage=false
import asyncio
import fnmatch
from dataclasses import dataclass
from typing import Any

from agentalloy.contracts import Contract
from agentalloy.install.lesson_pack import _sanitize_skill_id
from agentalloy.storage.protocols import CodeGraphStore, DecisionRow

# Hot-path caps (DK6). Bound the file scan/match and the injected decision count.
_FILE_SCAN_LIMIT = 5000
_MAX_TOUCH_FILES = 200
_MAX_DECISIONS = 8

_SOLUTIONS_PREFIX = "docs/solutions/"


@dataclass(frozen=True)
class DecisionPush:
    """The rendered decision manifest plus its provenance counts (for telemetry —
    the push runs outside the compose telemetry merge, so it reports its own).

    ``decisions`` carries the selected rows so downstream consumers (tests,
    telemetry) can inspect what was chosen without re-parsing the manifest text.
    ``entity_edges`` carries typed non-decision edges pointing at the anchor fqn.
    """

    text: str
    count: int
    truncated: bool
    decisions: tuple[DecisionRow, ...] = ()
    related_count: int = 0
    entity_edges: tuple[Any, ...] = ()


def _is_superseded(_decision: DecisionRow) -> bool:
    """Forward-compatible no-op (DK5): decisions carry no status today — the
    schema/``DecisionRow`` have no such field and ingestion never sets one. Placed
    at the seam so the exclusion activates unchanged when supersession authoring
    lands (a later, deferred slice).
    """
    return False


def _solutions_slug(decision_qn: str) -> str | None:
    """The lesson slug for a ``docs/solutions/<slug>.md::anchor`` decision, else
    None (only solutions decisions can have a promoted skill — the #375 promote
    path only promotes ``docs/solutions/``).
    """
    path = decision_qn.split("::", 1)[0]
    if path.startswith(_SOLUTIONS_PREFIX) and path.endswith(".md"):
        return path[len(_SOLUTIONS_PREFIX) : -len(".md")]
    return None


def _covered_by_instructions(decision: DecisionRow, composed_text: str) -> bool:
    """True iff a promoted skill for this decision's lesson **actually injected**
    into this turn's composed text (DK4). We dedup against what was really composed
    — not mere skill existence — so Knowledge yields only when Instructions truly
    covered the why here; a promoted-but-unranked/untagged skill leaves no fragment
    in the text and the decision is still pushed (no silent gap).
    """
    slug = _solutions_slug(decision.qualified_name)
    if slug is None:
        return False
    skill_id = _sanitize_skill_id(slug)
    return f"## skill: {skill_id}" in composed_text


def _resolve_touched_files(graph: CodeGraphStore, globs: list[str]) -> list[str]:
    """Indexed files matching any ``scope.touches`` glob, scan-bounded and capped
    at ``_MAX_TOUCH_FILES`` (DK6). ``fnmatch`` ``*`` spans ``/`` — intentional, so a
    ``dir/**`` glob matches nested files.
    """
    matched: list[str] = []
    for f in graph.list_files(limit=_FILE_SCAN_LIMIT):
        if any(fnmatch.fnmatch(f, g) for g in globs):
            matched.append(f)
            if len(matched) >= _MAX_TOUCH_FILES:
                break
    return matched


def _render(decisions: list[DecisionRow]) -> str:
    """Render a compact manifest: headings + source paths, no snippet bodies.

    The model reads this list to learn *which* decisions exist, then pulls the
    full rationale on demand via the ``agentalloy_query`` tool.
    """
    lines = [
        "# Decisions governing this work",
        "",
        "The following design decisions govern code in this work-item's scope.",
        "Use `agentalloy_query` with action=`knowledge_why` and query=<symbol> "
        "to read a decision's full rationale.",
        "",
    ]
    for d in decisions:
        source = d.qualified_name.split("::", 1)[0]
        heading = d.heading or d.qualified_name
        lines.append(f"- **{heading}** — `{source}`")
    lines.append("")
    lines.append(
        'Pull full content: `agentalloy_query` action=`knowledge_why` query=<fqn>'
        ' · action=`knowledge_related` query="<topic>"'
    )
    return "\n".join(lines).rstrip()


def _render_entities(entity_edges: list[Any]) -> str:
    """Render typed entity edges alongside decisions.

    Surfaces constraints, file touches, dependencies, commands, and stakeholders
    that weren't captured in the SDD artifact pipeline.
    """
    if not entity_edges:
        return ""
    lines = [
        "# Entities governing this work",
        "",
        "The following typed entities were extracted from the repo's docs.",
        "",
    ]
    for e in entity_edges[:10]:
        dst = e.dst if hasattr(e, 'dst') and e.dst else "(standalone)"
        src = e.src if hasattr(e, 'src') else ""
        kind = e.kind if hasattr(e, 'kind') else ""
        span = e.span if hasattr(e, 'span') and e.span else ""
        span_preview = span[:80] if span else "(no span)"
        lines.append(f"- **{kind}**: `{src}` → `{dst}` (`{span_preview}`)")
    lines.append("")
    lines.append(
        'Pull full content: `agentalloy_query` action=`knowledge_why` query=<fqn>'
    )
    return "\n".join(lines).rstrip()


def build_decision_block(
    contract: Contract,
    composed_text: str,
    graph: CodeGraphStore,
    state: Any = None,
    slug: str | None = None,
    task_title: str | None = None,
) -> DecisionPush | None:
    """Select + render the governing-decision block for a design/build work-item,
    or None when nothing applies. Pure: the caller gates and supplies the graph.

    Phase 2 (DK6): when *state*, *slug*, and *task_title* are provided, also
    merge related (thematic) decisions via ``related_decisions(task_title)``.
    """
    globs = list(contract.scope.touches) if contract.scope else []
    if not globs:
        return None
    files = _resolve_touched_files(graph, globs)
    if not files:
        return None

    # Phase 1: GOVERNS path (existing)
    kept: list[DecisionRow] = []
    for d in graph.decisions_for_files(files):
        if _is_superseded(d):
            continue
        if _covered_by_instructions(d, composed_text):
            continue
        kept.append(d)

    # Phase 2: Thematic path (related_decisions), constrained to GOVERNS set.
    # F3: the knowledge leg must contain only decisions with a GOVERNS edge into
    # a symbol in a scope.touches file.  Thematic search can discover governed
    # decisions missed by the file-glob path, but must NOT add generic README/doc
    # heading chunks that lack a GOVERNS edge.
    all_decisions = list(kept)
    related_count = 0
    if state is not None and slug is not None and task_title is not None:
        try:
            from agentalloy.code_index.retrieval.hybrid import related_decisions

            related_results = asyncio.run(related_decisions(state, slug, task_title, k=8))
            kept_qns = {d.qualified_name for d in kept}
            # Governed QN set: every decision with a GOVERNS edge into a touched
            # file (before superseded/instructions exclusion).  Phase 2 results
            # are filtered to this set so generic prose never enters the leg.
            governed_qns = {d.qualified_name for d in graph.decisions_for_files(files)}
            for r in related_results:
                if r.qualified_name not in governed_qns:
                    continue  # F3: no GOVERNS edge → not a decision for this work
                if r.qualified_name in kept_qns:
                    continue  # already present from Phase 1
                # Convert SearchResult to DecisionRow
                from agentalloy.storage.protocols import DecisionRow

                all_decisions.append(
                    DecisionRow(
                        qualified_name=r.qualified_name,
                        heading=r.qualified_name.split("::")[-1]
                        if "::" in r.qualified_name
                        else r.qualified_name,
                        snippet=r.snippet,
                        file_path=r.qualified_name.split("::")[0]
                        if "::" in r.qualified_name
                        else None,
                        start_line=None,
                    ),
                )
                related_count += 1
        except Exception:
            # Graceful degradation: if related_decisions fails, fall back to
            # GOVERNS-only path. The feature is additive, not critical.
            pass

    if not all_decisions:
        return None
    # deterministic order (source path, then anchor) so the selection is stable
    all_decisions.sort(key=lambda d: (d.file_path or "", d.qualified_name))
    truncated = len(all_decisions) > _MAX_DECISIONS
    all_decisions = all_decisions[:_MAX_DECISIONS]

    # Phase 3: Entity path — surface typed entities alongside decisions
    entity_edges: list[Any] = []
    if all_decisions:
        # Use the first decision's qualified name as the anchor for entity lookup
        anchor_fqn = all_decisions[0].qualified_name.split("::", 1)[0]
        try:
            entity_edges = graph.typed_edges_for_fqn(anchor_fqn)
        except Exception:
            pass  # Graceful degradation: no entities if query fails

    return DecisionPush(
        text=_render(all_decisions),
        count=len(all_decisions),
        truncated=truncated,
        decisions=tuple(all_decisions),
        related_count=related_count,
        entity_edges=tuple(entity_edges),
    )
