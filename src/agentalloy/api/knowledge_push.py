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

import fnmatch
from dataclasses import dataclass

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
    the push runs outside the compose telemetry merge, so it reports its own)."""

    text: str
    count: int
    truncated: bool


def _is_superseded(_decision: DecisionRow) -> bool:
    """Forward-compatible no-op (DK5): decisions carry no status today — the
    schema/``DecisionRow`` have no such field and ingestion never sets one. Placed
    at the seam so the exclusion activates unchanged when supersession authoring
    lands (a later, deferred slice)."""
    return False


def _solutions_slug(decision_qn: str) -> str | None:
    """The lesson slug for a ``docs/solutions/<slug>.md::anchor`` decision, else
    None (only solutions decisions can have a promoted skill — the #375 promote
    path only promotes ``docs/solutions/``)."""
    path = decision_qn.split("::", 1)[0]
    if path.startswith(_SOLUTIONS_PREFIX) and path.endswith(".md"):
        return path[len(_SOLUTIONS_PREFIX) : -len(".md")]
    return None


def _covered_by_instructions(decision: DecisionRow, composed_text: str) -> bool:
    """True iff a promoted skill for this decision's lesson **actually injected**
    into this turn's composed text (DK4). We dedup against what was really composed
    — not mere skill existence — so Knowledge yields only when Instructions truly
    covered the why here; a promoted-but-unranked/untagged skill leaves no fragment
    in the text and the decision is still pushed (no silent gap)."""
    slug = _solutions_slug(decision.qualified_name)
    if slug is None:
        return False
    skill_id = _sanitize_skill_id(slug)
    return f"## skill: {skill_id}" in composed_text


def _resolve_touched_files(graph: CodeGraphStore, globs: list[str]) -> list[str]:
    """Indexed files matching any ``scope.touches`` glob, scan-bounded and capped
    at ``_MAX_TOUCH_FILES`` (DK6). ``fnmatch`` ``*`` spans ``/`` — intentional, so a
    ``dir/**`` glob matches nested files."""
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
    appears twice in the injected block (UAT finding)."""
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
    contract: Contract, composed_text: str, graph: CodeGraphStore
) -> DecisionPush | None:
    """Select + render the governing-decision block for a design/build work-item,
    or None when nothing applies. Pure: the caller gates and supplies the graph."""
    globs = list(contract.scope.touches) if contract.scope else []
    if not globs:
        return None
    files = _resolve_touched_files(graph, globs)
    if not files:
        return None
    kept: list[DecisionRow] = []
    for d in graph.decisions_for_files(files):
        if _is_superseded(d):
            continue
        if _covered_by_instructions(d, composed_text):
            continue
        kept.append(d)
    if not kept:
        return None
    # deterministic order (source path, then anchor) so the selection is stable
    kept.sort(key=lambda d: (d.file_path or "", d.qualified_name))
    truncated = len(kept) > _MAX_DECISIONS
    kept = kept[:_MAX_DECISIONS]
    return DecisionPush(text=_render(kept), count=len(kept), truncated=truncated)
