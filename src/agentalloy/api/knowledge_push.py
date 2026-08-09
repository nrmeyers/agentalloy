"""Knowledge slice 2 — the just-in-time decision push (AC 6).

Given a design/build work-item's contract, the repo's code-index graph, and the
tier-2 text already composed this turn, select the decisions governing code in the
contract's ``scope.touches`` and render them as a distinct "why" block to fold into
the composed context (never the prompt-cached system field).

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
    """The rendered decision block plus its provenance counts (for telemetry —
    the push runs outside the compose telemetry merge, so it reports its own).
    """

    text: str
    count: int
    truncated: bool
    related_count: int = 0


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


def _strip_duplicate_heading(snippet: str, heading: str) -> str:
    """Drop the snippet's leading heading line when it duplicates ``heading``.

    Markdown chunks carry their own ``## Heading`` line in the body, and
    :func:`_render` emits the heading itself — without this the decision heading
    appears twice in the injected block (UAT finding).
    """
    body = snippet.strip()
    first, _, rest = body.partition("\n")
    if first.startswith("#") and first.lstrip("#").strip().casefold() == heading.strip().casefold():
        return rest.strip()
    return body


def _render(decisions: list[DecisionRow]) -> str:
    lines = ["# Decisions governing this work", ""]
    for d in decisions:
        source = d.qualified_name.split("::", 1)[0]
        heading = d.heading or d.qualified_name
        lines.append(f"## {heading}")
        lines.append(f"_governing decision — {source}_")
        if d.snippet:
            body = _strip_duplicate_heading(d.snippet, heading)
            if body:
                lines.append("")
                lines.append(body)
        lines.append("")
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
    return DecisionPush(
        text=_render(all_decisions),
        count=len(all_decisions),
        truncated=truncated,
        related_count=related_count,
    )
