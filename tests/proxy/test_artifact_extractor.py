"""Artifact marker extraction tests.

Tests the regex-based extraction of ``<!-- agentalloy:artifact -->`` markers
from LLM response text, the store write path, and edge cases.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentalloy.api.artifact_extractor import (
    extract_and_store,
    extract_artifacts,
    write_artifacts,
)

# ---------------------------------------------------------------------------
# extract_artifacts() — pure extraction
# ---------------------------------------------------------------------------


class TestExtractArtifacts:
    def test_no_markers_returns_empty(self) -> None:
        result = extract_artifacts("Just some regular text without markers.")
        assert not result.extracted
        assert result.artifacts == []
        assert result.cleaned_text == "Just some regular text without markers."

    def test_empty_text_returns_empty(self) -> None:
        result = extract_artifacts("")
        assert not result.extracted

    def test_none_text_returns_empty(self) -> None:
        result = extract_artifacts(None)  # type: ignore[arg-type]
        assert not result.extracted

    def test_single_marker_extraction(self) -> None:
        text = """Here is the spec:

<!-- agentalloy:artifact name=spec.artifact -->
## Acceptance Criteria
- LCP < 2.5s
## Out of Scope
- Mobile UI
<!-- /agentalloy:artifact -->

Done."""
        result = extract_artifacts(text)
        assert result.extracted
        assert len(result.artifacts) == 1
        assert result.artifacts[0].name == "spec.artifact"
        assert "## Acceptance Criteria" in result.artifacts[0].body
        assert "## Out of Scope" in result.artifacts[0].body
        # Markers stripped from cleaned text
        assert "<!-- agentalloy:artifact" not in result.cleaned_text
        assert "<!-- /agentalloy:artifact -->" not in result.cleaned_text
        # Surrounding text preserved
        assert "Here is the spec:" in result.cleaned_text
        assert "Done." in result.cleaned_text

    def test_multiple_markers(self) -> None:
        text = """
<!-- agentalloy:artifact name=design.artifact -->
## Architecture
Event sourcing
<!-- /agentalloy:artifact -->

Some text between.

<!-- agentalloy:artifact name=tasks.artifact -->
## Tasks
1. Token validation
2. Refresh flow
<!-- /agentalloy:artifact -->
"""
        result = extract_artifacts(text)
        assert result.extracted
        assert len(result.artifacts) == 2
        names = [a.name for a in result.artifacts]
        assert "design.artifact" in names
        assert "tasks.artifact" in names

    def test_multiline_body_preserved(self) -> None:
        text = """<!-- agentalloy:artifact name=spec.artifact -->
Line 1
Line 2
Line 3
<!-- /agentalloy:artifact -->"""
        result = extract_artifacts(text)
        assert result.extracted
        assert "Line 1\nLine 2\nLine 3" in result.artifacts[0].body

    def test_name_with_dots_and_dashes(self) -> None:
        text = """<!-- agentalloy:artifact name=my-complex.artifact-name -->
Content
<!-- /agentalloy:artifact -->"""
        result = extract_artifacts(text)
        assert result.extracted
        assert result.artifacts[0].name == "my-complex.artifact-name"

    def test_malformed_marker_ignored(self) -> None:
        text = """<!-- agentalloy:artifact -->
No name attribute
<!-- /agentalloy:artifact -->"""
        result = extract_artifacts(text)
        assert not result.extracted

    def test_unclosed_marker_ignored(self) -> None:
        text = """<!-- agentalloy:artifact name=spec.artifact -->
Content without closing marker"""
        result = extract_artifacts(text)
        assert not result.extracted

    def test_empty_body_ignored(self) -> None:
        text = """<!-- agentalloy:artifact name=spec.artifact -->
<!-- /agentalloy:artifact -->"""
        result = extract_artifacts(text)
        # Empty body after strip → not extracted
        assert not result.extracted

    def test_text_without_html_comments_skips_regex(self) -> None:
        # Fast path: no "<!--" means no regex scan
        result = extract_artifacts("No markers here at all.")
        assert not result.extracted
        assert result.cleaned_text == "No markers here at all."


# ---------------------------------------------------------------------------
# write_artifacts() — store interaction
# ---------------------------------------------------------------------------


class TestWriteArtifacts:
    def test_writes_to_store(self) -> None:
        store = MagicMock()
        store.set_artifact.return_value = {"name": "spec.artifact"}

        from agentalloy.api.artifact_extractor import ExtractedArtifact

        artifacts = [ExtractedArtifact(name="spec.artifact", body="## Acceptance\n...")]
        written = write_artifacts(artifacts, phase="spec", slug="auth", store=store)

        assert written == 1
        store.set_artifact.assert_called_once_with(
            "spec", "auth", "spec.artifact", "## Acceptance\n..."
        )

    def test_empty_artifacts_no_write(self) -> None:
        store = MagicMock()
        written = write_artifacts([], phase="spec", slug="auth", store=store)
        assert written == 0
        store.set_artifact.assert_not_called()

    def test_none_store_no_write(self) -> None:
        from agentalloy.api.artifact_extractor import ExtractedArtifact

        artifacts = [ExtractedArtifact(name="spec.artifact", body="content")]
        written = write_artifacts(artifacts, phase="spec", slug="auth", store=None)
        assert written == 0

    def test_store_failure_is_soft(self) -> None:
        store = MagicMock()
        store.set_artifact.side_effect = RuntimeError("db error")

        from agentalloy.api.artifact_extractor import ExtractedArtifact

        artifacts = [ExtractedArtifact(name="spec.artifact", body="content")]
        # Should not raise — soft failure
        written = write_artifacts(artifacts, phase="spec", slug="auth", store=store)
        assert written == 0
        store.set_artifact.assert_called_once()  # failed, but didn't raise


# ---------------------------------------------------------------------------
# extract_and_store() — combined
# ---------------------------------------------------------------------------


class TestExtractAndStore:
    def test_extracts_and_writes(self) -> None:
        store = MagicMock()
        text = """
<!-- agentalloy:artifact name=spec.artifact -->
## Acceptance Criteria
- LCP < 2.5s
<!-- /agentalloy:artifact -->
"""
        result = extract_and_store(text, phase="spec", slug="auth", store=store)
        assert result.extracted
        store.set_artifact.assert_called_once()

    def test_no_markers_no_write(self) -> None:
        store = MagicMock()
        result = extract_and_store("No markers here.", phase="spec", slug="auth", store=store)
        assert not result.extracted
        store.set_artifact.assert_not_called()
