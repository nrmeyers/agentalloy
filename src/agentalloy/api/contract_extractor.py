"""Contract marker extraction from LLM responses.

Parses ``<!-- agentalloy:contract phase=… slug=… [route=…] [...] -->...<!--
/agentalloy:contract -->`` markers from LLM response text and writes the
extracted contract(s) to the DuckDB state store, scoped to the session's real
project root (not the process cwd). Returns the cleaned text (markers stripped).

This is the agent-facing complement to :mod:`agentalloy.api.artifact_extractor`.
Artifacts record *output of a phase the agent is already in*; a contract marker
bootstraps the **first** contract during intake, when no current contract exists
to propagate. The intake exit gate (``contract_exists``) then sees it and the
intake→next-phase transition can proceed. See anomaly D-1.

Two integration paths (mirrors artifact_extractor):

- **Non-streaming responses**: extract + strip + forward the cleaned body.
- **Streaming responses** (tee pattern): accumulate text while yielding chunks
  to the client, then extract after the stream ends. The harness sees the
  markers in real-time (harmless HTML comments), but the contract is captured
  in the store for the next turn.

Extraction is soft-fail: any error (regex, store, scope) is logged and
suppressed so it never breaks the response path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Marker pattern: <!-- agentalloy:contract attrs -->...<!-- /agentalloy:contract -->
# The attributes (phase/slug/route/tags/touches) sit in the open marker; the
# body is everything between the markers. re.DOTALL so . matches newlines.
_CONTRACT_MARKER_RE = re.compile(
    r"<!--\s*agentalloy:contract\s+(.*?)\s*-->"
    r"(.*?)"
    r"<!--\s*/agentalloy:contract\s*-->",
    re.DOTALL,
)

# key=value pairs inside the open marker (values are whitespace-delimited).
_ATTR_RE = re.compile(r"([a-z_]+)=([^\s>]+)")


@dataclass(frozen=True)
class ExtractedContract:
    """A single contract extracted from a response."""

    phase: str
    slug: str
    route: str | None = None
    tags: list[str] | None = None
    touches: list[str] | None = None
    body: str = ""


@dataclass
class ContractExtractionResult:
    """Result of extracting contracts from response text."""

    cleaned_text: str
    contracts: list[ExtractedContract]

    @property
    def extracted(self) -> bool:
        return len(self.contracts) > 0


def _parse_attrs(attr_str: str) -> dict[str, str]:
    """Parse ``key=value`` pairs (whitespace-delimited) from the open marker."""
    return {k: v for k, v in _ATTR_RE.findall(attr_str)}


def _split_list(value: str | None) -> list[str] | None:
    """Split a comma-separated attribute into a stripped list, else ``None``."""
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def extract_contracts(text: str) -> ContractExtractionResult:
    """Parse contract markers from *text*; return cleaned text + contracts.

    Pure function — no store interaction. Soft: never raises — a regex failure
    yields the original text unchanged with no contracts.
    """
    if not text or "<!--" not in text:
        return ContractExtractionResult(cleaned_text=text, contracts=[])

    contracts: list[ExtractedContract] = []
    try:
        for match in _CONTRACT_MARKER_RE.finditer(text):
            attrs = _parse_attrs(match.group(1))
            phase = attrs.get("phase", "").strip()
            slug = attrs.get("slug", "").strip()
            body = match.group(2).strip()
            if not phase or not slug:
                continue  # phase+slug are required; drop malformed markers silently
            contracts.append(
                ExtractedContract(
                    phase=phase,
                    slug=slug,
                    route=attrs.get("route") or None,
                    tags=_split_list(attrs.get("tags")),
                    touches=_split_list(attrs.get("touches")),
                    body=body,
                )
            )
    except Exception:
        logger.debug("contract marker regex failed", exc_info=True)
        return ContractExtractionResult(cleaned_text=text, contracts=[])

    cleaned = _CONTRACT_MARKER_RE.sub("", text).strip()
    # Collapse multiple blank lines left by marker removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return ContractExtractionResult(cleaned_text=cleaned, contracts=contracts)


def _scoped_store(project_root: Path) -> Any | None:
    """The state store scoped to *project_root*'s (repo, stream), or ``None``.

    Uses the process-wide SDD store (opened once by the app lifespan) re-scoped
    exactly like ``get_repo_store`` in the HTTP router — ``repo_slug`` collapses
    worktrees, ``stream_id`` re-splits them — so a marker written by an agent in
    one worktree lands in *that* repo/stream, never the process cwd's bucket.
    Views share the connection and must not be closed.
    """
    try:
        from agentalloy.api.state_router import scoped_state_store
        from agentalloy.storage.state_store import process_store
    except Exception:
        return None
    base = process_store()
    if base is None:
        return None
    try:
        return scoped_state_store(base, Path(project_root).resolve())
    except Exception:
        logger.debug("contract scoped-store resolution failed", exc_info=True)
        return None


def write_contracts(
    contracts: list[ExtractedContract],
    *,
    project_root: Path,
) -> int:
    """Write extracted contracts to the store, scoped to *project_root*.

    Returns the count written. Soft-fail: each contract is independent — a
    failure on one doesn't prevent the others; logs warnings on failure.
    """
    if not contracts:
        return 0

    store = _scoped_store(project_root)
    if store is None:
        logger.debug("contract write skipped: no scoped store for project_root=%s", project_root)
        return 0

    written = 0
    for c in contracts:
        try:
            # contract_id = slug, matching _auto_create_next_contract's scheme.
            store.put_contract(
                c.slug,
                phase=c.phase,
                slug=c.slug,
                route=c.route,
                domain_tags=c.tags if c.tags else None,
                scope_touches=c.touches if c.touches else None,
                body=c.body,
            )
            written += 1
            logger.info(
                "contract marker: phase=%s slug=%s (%d chars), scope=%s",
                c.phase,
                c.slug,
                len(c.body),
                project_root,
            )
        except Exception:
            logger.warning(
                "contract write failed: phase=%s slug=%s",
                c.phase,
                c.slug,
                exc_info=True,
            )
    return written


def extract_and_store(
    text: str,
    *,
    project_root: Path,
) -> ContractExtractionResult:
    """Extract contracts from *text* and write them to the scoped store.

    Convenience combining :func:`extract_contracts` and :func:`write_contracts`.
    Returns the full extraction result so the caller can inspect what was found
    even if store writes fail.
    """
    result = extract_contracts(text)
    if result.extracted:
        write_contracts(result.contracts, project_root=project_root)
    return result
