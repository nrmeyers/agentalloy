"""Cheap pre-filter logic — decides whether to run gate evaluation at all.

Pre-filters are deterministic and fast (<5ms). A miss skips Qwen entirely.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any, cast

from agentalloy.signals.predicates import PredicateContext


@dataclass(frozen=True)
class PreFilterMatch:
    name: str  # "prompt_keyword" | "artifact_event" | "tool_use_event" | "manual"
    detail: str


def _extract_gate_paths(gate_spec: Any) -> list[str]:
    """Walk gate_spec recursively and collect all `path` glob values."""
    paths: list[str] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        if "path" in gate_d:
            paths.append(str(gate_d["path"]))
        for v in gate_d.values():
            paths.extend(_extract_gate_paths(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            paths.extend(_extract_gate_paths(item))
    return paths


# Public alias: the gate-path walker is reused by the invariants module
# (signals.invariants.derive_invariants) to derive load-bearing prose tokens.
extract_gate_paths = _extract_gate_paths


def _extract_gate_store_specs(gate_spec: Any) -> list[tuple[str, str]]:
    """Walk gate_spec recursively and collect ``(phase, name)`` pairs.

    The store-backed sibling of :func:`_extract_gate_paths`: post-migration
    ``artifact_exists``/``artifact_contains`` nodes for spec/design carry
    ``phase``/``name`` instead of a filesystem ``path`` glob, so the missing-
    artifact advisory in :func:`agentalloy.signals.gates.decide_transition`
    needs its own walker to still name what's missing.
    """
    specs: list[tuple[str, str]] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        if "phase" in gate_d and "name" in gate_d:
            specs.append((str(gate_d["phase"]), str(gate_d["name"])))
        for v in gate_d.values():
            specs.extend(_extract_gate_store_specs(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            specs.extend(_extract_gate_store_specs(item))
    return specs


def _extract_gate_sections(gate_spec: Any) -> list[str]:
    """Walk gate_spec recursively and collect all `artifact_contains.sections` values.

    Sibling of :func:`_extract_gate_paths`: where that pulls every ``path`` glob, this
    pulls the required markdown-heading sections an ``artifact_contains`` gate declares.
    Returns the section names in declaration order (first-seen wins for the dedup), so
    the banner's progress suffix can report ``present/total`` against the same sections
    the exit gate checks. An ``artifact_contains`` with no ``sections`` contributes
    nothing; a missing/garbled spec yields ``[]``.
    """
    sections: list[str] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        contains = gate_d.get("artifact_contains")
        if isinstance(contains, dict):
            raw_sections = cast(dict[str, Any], contains).get("sections")
            if isinstance(raw_sections, list):
                for s in cast(list[Any], raw_sections):
                    if isinstance(s, str) and s not in sections:
                        sections.append(s)
        for k, v in gate_d.items():
            if k != "artifact_contains":
                for s in _extract_gate_sections(v):
                    if s not in sections:
                        sections.append(s)
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            for s in _extract_gate_sections(item):
                if s not in sections:
                    sections.append(s)
    return sections


def _extract_artifact_contains_specs(gate_spec: Any) -> list[tuple[str, list[str]]]:
    """Pair EACH ``artifact_contains`` gate's ``path`` with ITS OWN ``sections``.

    Unlike :func:`_extract_gate_sections` (which flattens every section name across all
    gates into one list), this keeps each gate's ``path`` glob bound to the sections that
    gate actually declares — so the banner can score each artifact against its own
    required headings instead of checking every section against the first path only (the
    bug this fixes). Order follows declaration; an ``artifact_contains`` lacking a string
    ``path`` or a non-empty list of string ``sections`` is skipped.

    Handles both filesystem ``path`` globs and store-backed ``phase``+``name``
    queries. For store-backed gates the ``phase``+``name`` pair is synthesized
    into a ``docs/<phase>/*.md`` glob so the scaffolding / banner machinery can
    still operate.
    """
    specs: list[tuple[str, list[str]]] = []
    # Only synthesize a ``path`` from ``phase``+``name`` for phases that
    # originally used disk-glob scaffolding (qa / ship / fast).  Spec and
    # design are fully store-backed — no scaffolding.
    _SYNTHESIZE_PHASES = frozenset({"qa", "ship", "sdd-fast"})
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        contains = gate_d.get("artifact_contains")
        if isinstance(contains, dict):
            c = cast(dict[str, Any], contains)
            path = c.get("path")
            raw_sections = c.get("sections")
            if isinstance(raw_sections, list):
                sections = [s for s in cast(list[Any], raw_sections) if isinstance(s, str)]
                if sections:
                    if isinstance(path, str):
                        specs.append((path, sections))
                    elif "phase" in c and "name" in c:
                        phase = str(c["phase"])
                        if phase in _SYNTHESIZE_PHASES:
                            # Store-backed form: synthesize a path glob for
                            # scaffolding / banner machinery.
                            name = str(c["name"])
                            specs.append((f"docs/{phase}/{name}", sections))
        for k, v in gate_d.items():
            if k != "artifact_contains":
                specs.extend(_extract_artifact_contains_specs(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            specs.extend(_extract_artifact_contains_specs(item))
    return specs


def _extract_artifact_contains_store_specs(gate_spec: Any) -> list[tuple[str, str, list[str]]]:
    """Pair EACH store-backed ``artifact_contains`` gate with its own sections.

    Returns ``(phase, name, sections)`` in declaration order — the store-backed
    sibling of :func:`_extract_artifact_contains_specs`, which can only express a
    filesystem glob.

    Why this exists: that function synthesizes a fake ``docs/<phase>/<name>`` glob
    for store-backed gates so the banner's progress machinery has *something* to
    hold. Nothing is ever written at that path (lifecycle artifacts live in the
    store), so scoring against it always reported "no sections present" and the
    banner suppressed its progress suffix for every store-backed phase. Scoring has
    to query the store, which needs the ``(phase, name)`` pair unsynthesized.

    Unlike the synthesizing walker there is no phase allow-list here: spec, design
    and plan are store-backed too and their progress is exactly as useful.
    """
    specs: list[tuple[str, str, list[str]]] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        contains = gate_d.get("artifact_contains")
        if isinstance(contains, dict):
            c = cast(dict[str, Any], contains)
            raw_sections = c.get("sections")
            if isinstance(raw_sections, list) and "phase" in c and "name" in c:
                sections = [s for s in cast(list[Any], raw_sections) if isinstance(s, str)]
                if sections:
                    specs.append((str(c["phase"]), str(c["name"]), sections))
        for k, v in gate_d.items():
            if k != "artifact_contains":
                specs.extend(_extract_artifact_contains_store_specs(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            specs.extend(_extract_artifact_contains_store_specs(item))
    return specs


def _extract_artifact_exists_store_specs(gate_spec: Any) -> list[tuple[str, str]]:
    """``(phase, name)`` for every store-backed ``artifact_exists`` gate.

    The store-backed counterpart of :func:`_extract_artifact_exists_paths`, used by
    the banner to name which artifact is not yet recorded.
    """
    specs: list[tuple[str, str]] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        exists = gate_d.get("artifact_exists")
        if isinstance(exists, dict):
            e = cast(dict[str, Any], exists)
            if "phase" in e and "name" in e:
                specs.append((str(e["phase"]), str(e["name"])))
        for k, v in gate_d.items():
            if k != "artifact_exists":
                specs.extend(_extract_artifact_exists_store_specs(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            specs.extend(_extract_artifact_exists_store_specs(item))
    return specs


def _extract_artifact_exists_paths(gate_spec: Any) -> list[str]:
    """Walk gate_spec recursively and collect the ``path`` of every ``artifact_exists`` gate."""
    paths: list[str] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        exists = gate_d.get("artifact_exists")
        if isinstance(exists, dict):
            p = cast(dict[str, Any], exists).get("path")
            if isinstance(p, str):
                paths.append(p)
        for k, v in gate_d.items():
            if k != "artifact_exists":
                paths.extend(_extract_artifact_exists_paths(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            paths.extend(_extract_artifact_exists_paths(item))
    return paths


def _extract_exists_only_paths(gate_spec: Any) -> list[str]:
    """Paths guarded by an ``artifact_exists`` gate with NO sibling ``artifact_contains``.

    In the SDD gates each doc file carries both an ``artifact_exists`` and an
    ``artifact_contains`` for the same glob; a pure checkpoint like the design phase's
    ``.agentalloy/contracts/build/*.md`` build-contract requirement has only
    ``artifact_exists``. Returning the exists-only paths lets the banner surface that
    requirement (otherwise invisible until the gate fails) without duplicating the
    section-bearing doc gates. Order follows declaration, deduped.
    """
    contains_paths = {p for p, _ in _extract_artifact_contains_specs(gate_spec)}
    out: list[str] = []
    for p in _extract_artifact_exists_paths(gate_spec):
        if p not in contains_paths and p not in out:
            out.append(p)
    return out


def _extract_gate_tools(gate_spec: Any) -> list[str]:
    """Walk gate_spec recursively and collect all `tools` list values."""
    tools: list[str] = []
    if isinstance(gate_spec, dict):
        gate_d: dict[str, Any] = cast(dict[str, Any], gate_spec)
        if "tools" in gate_d and isinstance(gate_d["tools"], list):
            tools.extend(cast(list[str], gate_d["tools"]))
        for k, v in gate_d.items():
            if k != "tools":
                tools.extend(_extract_gate_tools(v))
    elif isinstance(gate_spec, list):
        gate_l: list[Any] = cast(list[Any], gate_spec)
        for item in gate_l:
            tools.extend(_extract_gate_tools(item))
    return tools


def check_prefilter(
    signal_keywords: list[str],
    gate_spec: Any,
    ctx: PredicateContext,
) -> PreFilterMatch | None:
    """Return the first matching pre-filter or None.

    Args:
        signal_keywords: from workflow_skill.signal_keywords
        gate_spec: the exit_gates dict
        ctx: current predicate context
    """
    # Manual override via env var
    if os.environ.get("AGENTALLOY_FORCE_CHECK") == "1":
        return PreFilterMatch(name="manual", detail="AGENTALLOY_FORCE_CHECK=1")

    # Prompt keyword match (case-insensitive substring)
    if ctx.recent_prompt_text and signal_keywords:
        lower_prompt = ctx.recent_prompt_text.lower()
        for kw in signal_keywords:
            if kw.lower() in lower_prompt:
                return PreFilterMatch(name="prompt_keyword", detail=f"keyword='{kw}'")

    # Artifact event: any gate path glob intersects file_events_since
    if ctx.file_events_since and gate_spec:
        gate_paths = _extract_gate_paths(gate_spec)
        # Patterns depend only on gp, not on the event path — build them once.
        patterns = [(gp, str(ctx.project_root / gp), gp.split("/")[-1]) for gp in gate_paths]
        for event_path in ctx.file_events_since:
            for gp, full_pat, name_pat in patterns:
                try:
                    if fnmatch.fnmatch(str(event_path), full_pat):
                        return PreFilterMatch(
                            name="artifact_event",
                            detail=f"path={event_path} matched gate pattern={gp}",
                        )
                    # Also match just the filename part
                    if fnmatch.fnmatch(event_path.name, name_pat):
                        return PreFilterMatch(
                            name="artifact_event",
                            detail=f"path={event_path} matched gate pattern={gp}",
                        )
                except Exception:
                    continue

    # Tool use event: recent_tool_use matches any tool in gate_spec
    if ctx.recent_tool_use and gate_spec:
        gate_tools = _extract_gate_tools(gate_spec)
        tool_name = ctx.recent_tool_use.get("tool", "")
        if tool_name and any(t in tool_name for t in gate_tools):
            return PreFilterMatch(
                name="tool_use_event",
                detail=f"tool='{tool_name}' matched gate tools",
            )

    return None
