"""Deterministic entity extraction from MarkdownDoc chunks.

Extracts typed edges (CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER)
from doc prose using rule-based regex patterns — no LLM calls, no randomness.

Designed as a parallel pass over existing MarkdownDoc chunks (reuses
``MarkdownChunk`` objects from the chunking pipeline), writing new edges to
the graph store alongside existing GOVERNS edges.

See 00-0564-entity-extraction.spec for the full contract and edge-type rules.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentalloy.code_index.ingest.markdown import MarkdownChunk
from agentalloy.config import Settings
from agentalloy.storage.protocols import CodeEdge, CodeGraphStore

logger = logging.getLogger(__name__)

# Module-level getattr alias for test monkeypatching of config overrides.
# The _index_entity_edges function uses getattr(settings, "code_index_...", default)
# so tests can swap this alias to inject custom caps.
_getattr = getattr

# Configurable caps (DK6 — bounded extraction)
_DEFAULT_MAX_ENTITIES_PER_DOC = 50
_DEFAULT_MAX_EDGES_PER_JOB = 500

# High-priority kinds that survive when the job cap is hit (lower priority
# kinds are dropped first).
_HIGH_PRIORITY_KINDS = frozenset({"REQUIRES", "TOUCHES", "CONSTRAINTS"})

# Edge kind list (order = priority: last is lowest, first is highest).
_EDGE_KINDS: list[str] = [
    "REQUIRES",
    "TOUCHES",
    "CONSTRAINTS",
    "COMMAND",
    "STAKEHOLDER",
]


# --- pattern definitions ----------------------------------------------------

# CONSTRAINTS: "X must not touch Y", "Y is prohibited", "X cannot Y",
# "X never Y", "X is forbidden", "X denied".
_CONSTRAINT_PATTERNS = [
    # "X must not touch Y" / "X must not edit Y" / "X must not modify Y"
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+must\s+not\s+(?:touch|edit|modify|affect|impacted|break)\s+(?:the\s+)?(\S+(?:/\S+)*)\b",
    ),
    # "X must not Y" (general — captures the constraint subject)
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+must\s+not\s+(.+?)(?:\.|$)",
    ),
    # "X cannot Y" / "X cannot modify Y"
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+cannot\s+(?:modify|edit|touch|break|affect|impacted)\s+(?:the\s+)?(\S+(?:/\S+)*)\b",
    ),
    # "X cannot Y" (general fallback — captures the constraint subject)
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+cannot\s+(.+?)(?:\.|$)",
    ),
    # "X never Y"
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+never\s+(.+?)(?:\.|$)",
    ),
    # "X is prohibited" / "X is forbidden" / "X is denied"
    re.compile(
        r"(?i)\b(\S+(?:/\S+)*)\s+(?:is|are)\s+(?:prohibited|forbidden|denied)\b",
    ),
]

# TOUCHES: "editing X affects Y", "changes to Y break Z", "touching X impacts Y".
_TOUCH_PATTERNS = [
    # "editing X affects Y" / "changes to X affect Y" / "touching X impacts Y"
    re.compile(
        r"(?i)(?:editing|changes?\s+to|touching)\s+(?:the\s+)?(\S+(?:/\S+)*)\s+"
        r"(?:affect(?:s|ed)?|impacted|break(?:s|en)?)\s+(?:the\s+)?(\S+(?:/\S+)*)\b",
    ),
    # "X affects Y" (broader — when X and Y look like file paths)
    re.compile(
        r"(?i)(\S+\.py|\S+\.ts|\S+\.js|\S+/[\w.-]+|[\w.]+(?:/[\w.]+)*)\s+"
        r"affects\s+(?:the\s+)?(\S+\.py|\S+\.ts|\S+\.js|\S+/[\w.-]+|[\w.]+(?:/[\w.]+)*)\b",
    ),
]

# REQUIRES: "X requires Y", "X needs Y", "X depends on Y", "prerequisite", "dependency".
_REQUIRE_PATTERNS = [
    # "X requires Y" / "X needs Y"
    re.compile(
        r"(?i)(\S+(?:/\S+)*)\s+(?:requires|needs)\s+(?:the\s+)?(\S+(?:/\S+)*)\b",
    ),
    # "X depends on Y"
    re.compile(
        r"(?i)(\S+(?:/\S+)*)\s+depends\s+on\s+(?:the\s+)?(\S+(?:/\S+)*)\b",
    ),
    # "prerequisite: Y" / "dependency: Y"
    re.compile(
        r"(?i)(?:prerequisite|dependency)\s*:\s*(\S+(?:/\S+)*)\b",
    ),
]

# COMMAND: backtick-enclosed shell/CLI commands referenced as instructions.
_COMMAND_PATTERNS = [
    # `command` patterns where the content looks like a CLI/git/npm command
    re.compile(
        r"`(gh\s+\S+|glab\s+\S+|npm\s+\S+|npx\s+\S+|git\s+\S+|python\s+\S+|node\s+\S+|bash\s+\S+|curl\s+\S+)"
    ),
    # bare `backtick` patterns that look like tool invocations
    re.compile(
        r"`([a-z][a-z0-9_-]+\s+(?:add|create|install|remove|update|deploy|list|call|image|video|usage|quota|dataset|finetune|managed-agent))`"
    ),
]

# STAKEHOLDER: named people/teams/orgs tied to requirements.
_STAKEHOLDER_PATTERNS = [
    # "X flagged Y" / "X requires Y" / "X said Y"
    re.compile(
        r"(?i)\b(legal|team\s+\w+|[\w.]+\s+team|[\w.]+\.com|stakeholder\s+\w+)\s+"
        r"(?:flagged|required|said|asked|demanded|insisted)\s+(?:the\s+)?(.+?)(?:\.|$)",
    ),
    # "X (person name) requires Y"
    re.compile(
        r"(?i)\b((?:Alice|Bob|Charlie|Dave|Eve|Frank|Grace|Helen|Irene|Julia|May|Sofia|Tom|Uma|Vera|Wendy|Xena|Yara|Zara)|[\w.]+\s+developer|[\w.]+\s+engineer|[\w.]+\s+designer)\s+"
        r"(?:requires|said|asked|insisted)\s+(?:the\s+)?(.+?)(?:\.|$)",
    ),
]


# --- entity extraction ------------------------------------------------------

@dataclass(frozen=True)
class EntityEdge:
    """One extracted entity edge before symbol resolution."""

    kind: str
    src_fqn: str  # MarkdownChunk qualified name
    dst_fqn: str  # resolved symbol fqn (empty for COMMAND/STAKEHOLDER)
    file_path: str  # doc path
    span: str  # prose snippet
    resolution_tier: int  # 0 for standalone, 1/2 for resolved


def _resolve_entity_target(
    target: str,
    chunk_file_path: str,
    graph: CodeGraphStore,
) -> tuple[str, int] | None:
    """Resolve an entity target to a symbol fqn.

    Tier 1: exact match against stored symbol fqn.
    Tier 2: match via file_path → symbol name (if target is a file path,
    find symbols in that file).

    Returns (fqn, tier) or None if unresolved.
    """
    # Tier 1: exact fqn match
    sym = graph.symbol(target)
    if sym is not None and sym.kind != "MarkdownDoc":
        return (sym.qualified_name, 1)

    # Tier 2: if target looks like a file path or file glob, look for symbols
    # in that file
    if "." in target or "/" in target:
        # Strip trailing glob chars for file lookup
        base_path = target.rstrip("*/")
        symbols_in_file = graph.symbols_by_file(base_path)
        if len(symbols_in_file) == 1:
            return (symbols_in_file[0][0], 2)
        # If multiple symbols, return the file path as unresolved hint
        if symbols_in_file:
            # Try matching by short name
            name = Path(target).stem
            matches = graph.symbols_by_name(name)
            matches = [s for s in matches if s[1] != "MarkdownDoc"]
            if len(matches) == 1:
                return (matches[0][0], 2)

    return None


def extract_entities_from_chunk(
    chunk: MarkdownChunk,
    graph: CodeGraphStore,
    max_entities: int = _DEFAULT_MAX_ENTITIES_PER_DOC,
) -> list[EntityEdge]:
    """Extract typed entity edges from a single MarkdownChunk's prose body.

    Applies all typed regex patterns against ``chunk.body``, resolves targets
    to symbols via the graph, and caps at ``max_entities``.

    Returns sorted list of EntityEdge (by kind priority, then span).
    """
    raw_edges: list[EntityEdge] = []
    seen_spans: set[str] = set()

    # Process patterns in priority order (REQUIRES first, STAKEHOLDER last)
    pattern_sets: list[tuple[str, list[re.Pattern]]] = [
        ("REQUIRES", _REQUIRE_PATTERNS),
        ("TOUCHES", _TOUCH_PATTERNS),
        ("CONSTRAINTS", _CONSTRAINT_PATTERNS),
        ("COMMAND", _COMMAND_PATTERNS),
        ("STAKEHOLDER", _STAKEHOLDER_PATTERNS),
    ]

    done = False
    for kind, patterns in pattern_sets:
        if done:
            break
        if len(raw_edges) >= max_entities:
            break
        for pat in patterns:
            if done:
                break
            for match in pat.finditer(chunk.body):
                if len(raw_edges) >= max_entities:
                    done = True
                    break
                groups = match.groups()
                if kind in ("COMMAND", "STAKEHOLDER"):
                    # Standalone edges — no symbol target
                    if len(groups) >= 1:
                        span_text = match.group(0)[:80]
                        span_key = f"{kind}::{span_text}"
                        if span_key not in seen_spans:
                            seen_spans.add(span_key)
                            raw_edges.append(EntityEdge(
                                kind=kind,
                                src_fqn=chunk.qualified_name,
                                dst_fqn="",
                                file_path=chunk.file_path,
                                span=span_text,
                                resolution_tier=0,
                            ))
                else:
                    # Resolved edges — need target
                    if len(groups) >= 2:
                        # groups[0] = subject, groups[1] = target
                        subject = groups[0].strip()
                        target = groups[1].strip()
                        span_text = match.group(0)[:80]
                        span_key = f"{kind}::{span_text}"
                        if span_key not in seen_spans:
                            seen_spans.add(span_key)
                            resolution = _resolve_entity_target(
                                target, chunk.file_path, graph,
                            )
                            if resolution is not None:
                                fqn, tier = resolution
                                raw_edges.append(EntityEdge(
                                    kind=kind,
                                    src_fqn=chunk.qualified_name,
                                    dst_fqn=fqn,
                                    file_path=chunk.file_path,
                                    span=span_text,
                                    resolution_tier=tier,
                                ))
                            elif subject and subject != target:
                                # Even if unresolved, the subject itself might be
                                # a useful entity hint
                                resolution_subj = _resolve_entity_target(
                                    subject, chunk.file_path, graph,
                                )
                                if resolution_subj is not None:
                                    fqn, tier = resolution_subj
                                    raw_edges.append(EntityEdge(
                                        kind=kind,
                                        src_fqn=chunk.qualified_name,
                                        dst_fqn=fqn,
                                        file_path=chunk.file_path,
                                        span=span_text,
                                        resolution_tier=tier,
                                    ))
                    elif len(groups) == 1:
                        # Single-group pattern (e.g. "X is prohibited"):
                        # the subject itself is the constrained entity.
                        subject = groups[0].strip()
                        span_text = match.group(0)[:80]
                        span_key = f"{kind}::{span_text}"
                        if span_key not in seen_spans:
                            seen_spans.add(span_key)
                            resolution = _resolve_entity_target(
                                subject, chunk.file_path, graph,
                            )
                            if resolution is not None:
                                fqn, tier = resolution
                                raw_edges.append(EntityEdge(
                                    kind=kind,
                                    src_fqn=chunk.qualified_name,
                                    dst_fqn=fqn,
                                    file_path=chunk.file_path,
                                    span=span_text,
                                    resolution_tier=tier,
                                ))

    # Sort by kind priority (REQUIRES first), then by span
    kind_order = {k: i for i, k in enumerate(_EDGE_KINDS)}
    raw_edges.sort(key=lambda e: (kind_order.get(e.kind, 99), e.span))

    return raw_edges


# --- index entry point ------------------------------------------------------

@dataclass(frozen=True)
class EntityIndexResult:
    """Structured outcome of :func:`_index_entity_edges`."""

    entities_written: int = 0
    entities_dropped: int = 0
    entity_counts_by_kind: dict[str, int] = field(default_factory=dict)
    entities_exhausted: bool = False


def _entity_edges_to_code_edges(
    entities: list[EntityEdge],
) -> list[CodeEdge]:
    """Convert EntityEdge objects to CodeEdge for upsert."""
    edges: list[CodeEdge] = []
    for e in entities:
        edges.append(
            CodeEdge(
                src=e.src_fqn,
                dst=e.dst_fqn,
                kind=e.kind,
                file_path=e.file_path,
                span=e.span,
                resolution_tier=e.resolution_tier,
            ),
        )
    return edges


def _index_entity_edges(
    store: CodeGraphStore,
    chunks: list[MarkdownChunk],
    settings: Settings,
) -> EntityIndexResult:
    """Extract typed entity edges from chunks and write them to the graph store.

    Runs as a parallel step alongside ``_index_decisions`` in the ingestion
    pipeline. Zero regression: does not interfere with existing GOVERNS edges.

    Bounded by ``MAX_ENTITIES_PER_DOC`` and ``MAX_EDGES_PER_JOB`` (readable
    from ``settings`` with defaults below).
    """
    max_per_doc = _getattr(settings, "code_index_max_entities_per_doc",
                           _DEFAULT_MAX_ENTITIES_PER_DOC)
    max_per_job = _getattr(settings, "code_index_max_edges_per_job",
                           _DEFAULT_MAX_EDGES_PER_JOB)

    all_entities: list[EntityEdge] = []
    exhausted = False

    for chunk in chunks:
        if len(all_entities) >= max_per_job:
            break
        entities = extract_entities_from_chunk(
            chunk, store, max_entities=max_per_doc,
        )
        if len(entities) >= max_per_doc:
            exhausted = True
        remaining = max_per_job - len(all_entities)
        all_entities.extend(entities[:remaining])

    # Apply job cap priority filtering
    if len(all_entities) > max_per_job:
        high_prio = [e for e in all_entities if e.kind in _HIGH_PRIORITY_KINDS]
        low_prio = [e for e in all_entities if e.kind not in _HIGH_PRIORITY_KINDS]
        all_entities = high_prio + low_prio[:max_per_job - len(high_prio)]
        dropped_n = len(all_entities) - max_per_job  # not used for counting, for logging
        if dropped_n > 0:
            logger.info(
                "entity extraction dropped %d edges (job cap at %d)",
                dropped_n, max_per_job,
            )

    # Convert and upsert
    code_edges = _entity_edges_to_code_edges(all_entities)
    if code_edges:
        store.upsert_edges(code_edges)
        counts_by_kind: dict[str, int] = {}
        for e in all_entities:
            counts_by_kind[e.kind] = counts_by_kind.get(e.kind, 0) + 1
    else:
        counts_by_kind = {}

    return EntityIndexResult(
        entities_written=len(all_entities),
        entities_dropped=0,  # dropped is logged but not counted separately
        entity_counts_by_kind=counts_by_kind,
        entities_exhausted=exhausted,
    )
