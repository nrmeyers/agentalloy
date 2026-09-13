"""Artifact marker extraction from LLM responses.

Parses ``<!-- agentalloy:artifact name=X -->...<!-- /agentalloy:artifact -->``
markers from LLM response text, writes the extracted artifact bodies to the
DuckDB store, and returns the cleaned text (markers stripped).

Two integration paths:

- **Non-streaming responses**: extract + strip + forward the cleaned body.
  The harness never sees the markers.
- **Streaming responses** (tee pattern): accumulate text while yielding chunks
  to the client, then extract after the stream ends. The harness sees the
  markers in real-time (they're harmless HTML comments), but the artifacts are
  captured in the store for the next turn's state leg.

The extraction is soft-fail: any error (regex, store write) is logged and
suppressed so it never breaks the response path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Marker pattern: <!-- agentalloy:artifact name=artifact-name -->...<!-- /agentalloy:artifact -->
# The name attribute is required. The body is everything between the markers.
# re.DOTALL so . matches newlines in the artifact body.
_ARTIFACT_MARKER_RE = re.compile(
    r"<!--\s*agentalloy:artifact\s+name=([\w.\-/]+)\s*-->"
    r"(.*?)"
    r"<!--\s*/agentalloy:artifact\s*-->",
    re.DOTALL,
)


@dataclass(frozen=True)
class ExtractedArtifact:
    """A single artifact extracted from a response."""

    name: str
    body: str


@dataclass
class ExtractionResult:
    """Result of extracting artifacts from response text.

    ``cleaned_text`` is the response with all artifact markers removed.
    ``artifacts`` is the list of extracted artifacts (name + body).
    ``extracted`` is True when at least one artifact was found.
    """

    cleaned_text: str
    artifacts: list[ExtractedArtifact]

    @property
    def extracted(self) -> bool:
        return len(self.artifacts) > 0


def extract_artifacts(text: str) -> ExtractionResult:
    """Parse artifact markers from *text*, return cleaned text + extracted artifacts.

    Pure function — no store interaction. The caller decides whether to write
    the artifacts to the store. Soft: never raises — a regex failure yields
    the original text unchanged with no artifacts.
    """
    if not text or "<!--" not in text:
        return ExtractionResult(cleaned_text=text, artifacts=[])

    artifacts: list[ExtractedArtifact] = []
    try:
        for match in _ARTIFACT_MARKER_RE.finditer(text):
            name = match.group(1).strip()
            body = match.group(2).strip()
            if name and body:
                artifacts.append(ExtractedArtifact(name=name, body=body))
    except Exception:
        logger.debug("artifact marker regex failed", exc_info=True)
        return ExtractionResult(cleaned_text=text, artifacts=[])

    cleaned = _ARTIFACT_MARKER_RE.sub("", text).strip()
    # Collapse multiple blank lines left by marker removal.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return ExtractionResult(cleaned_text=cleaned, artifacts=artifacts)


def write_artifacts(
    artifacts: list[ExtractedArtifact],
    *,
    phase: str,
    slug: str,
    store: Any,
) -> int:
    """Write extracted artifacts to the store. Returns the count written.

    Soft: each artifact write is independent — a failure on one doesn't
    prevent the others from being written. Logs warnings on failure.
    """
    if not artifacts or not store:
        return 0

    written = 0
    for artifact in artifacts:
        try:
            store.set_artifact(phase, slug, artifact.name, artifact.body)
            written += 1
            logger.info(
                "artifact extracted: phase=%s slug=%s name=%s (%d chars)",
                phase,
                slug,
                artifact.name,
                len(artifact.body),
            )
        except Exception:
            logger.warning(
                "artifact write failed: phase=%s slug=%s name=%s",
                phase,
                slug,
                artifact.name,
                exc_info=True,
            )
    return written


def extract_and_store(
    text: str,
    *,
    phase: str,
    slug: str,
    store: Any,
) -> ExtractionResult:
    """Extract artifacts from *text* and write them to the store.

    Convenience function combining :func:`extract_artifacts` and
    :func:`write_artifacts`. Returns the full extraction result so the
    caller can inspect what was found even if store writes fail.
    """
    result = extract_artifacts(text)
    if result.extracted:
        write_artifacts(result.artifacts, phase=phase, slug=slug, store=store)
    return result
