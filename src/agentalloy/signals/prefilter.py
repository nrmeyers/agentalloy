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
