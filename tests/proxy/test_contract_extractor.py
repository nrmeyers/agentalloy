"""Contract marker extraction tests.

Covers the intake-first-contract bootstrap (anomaly D-1): parsing
``<!-- agentalloy:contract -->`` markers from LLM response text and writing the
extracted contract to the DuckDB store scoped to the session's project root
(rather than the process cwd / the base store's default repo).
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.api.contract_extractor import (
    _scoped_store,
    extract_and_store,
    extract_contracts,
    write_contracts,
)
from agentalloy.storage.state_store import bind_process_store, open_state_store

# ---------------------------------------------------------------------------
# extract_contracts() — pure extraction
# ---------------------------------------------------------------------------


class TestExtractContracts:
    def test_no_markers_returns_empty(self) -> None:
        result = extract_contracts("Just regular text, no markers here.")
        assert not result.extracted
        assert result.contracts == []
        assert result.cleaned_text == "Just regular text, no markers here."

    def test_empty_text_returns_empty(self) -> None:
        result = extract_contracts("")
        assert not result.extracted

    def test_single_marker_extraction(self) -> None:
        text = """Here is the contract:

<!-- agentalloy:contract phase=spec slug=upstream-config route=full -->
## What the user wants
Add per-repo upstream config via the Web UI.
## Proposed route
full
<!-- /agentalloy:contract -->

Done."""

        result = extract_contracts(text)
        assert result.extracted
        assert len(result.contracts) == 1
        c = result.contracts[0]
        assert c.phase == "spec"
        assert c.slug == "upstream-config"
        assert c.route == "full"
        assert "per-repo upstream config" in c.body
        # Markers stripped from cleaned text; surrounding prose preserved.
        assert "agentalloy:contract" not in result.cleaned_text
        assert "Here is the contract:" in result.cleaned_text
        assert "Done." in result.cleaned_text

    def test_optional_attributes(self) -> None:
        text = (
            "<!-- agentalloy:contract phase=design slug=auth tags=fastapi,di "
            "touches=src/auth/**,src/deps/** -->\nbody\n<!-- /agentalloy:contract -->"
        )
        result = extract_contracts(text)
        assert result.extracted
        c = result.contracts[0]
        assert c.tags == ["fastapi", "di"]
        assert c.touches == ["src/auth/**", "src/deps/**"]
        assert c.body == "body"

    def test_missing_phase_or_slug_is_dropped(self) -> None:
        text = (
            "<!-- agentalloy:contract slug=only-slug -->x<!-- /agentalloy:contract --> "
            "<!-- agentalloy:contract phase=spec -->y<!-- /agentalloy:contract --> "
            "keep me"
        )
        result = extract_contracts(text)
        assert not result.extracted
        assert result.contracts == []
        # Malformed markers are still stripped from the cleaned text.
        assert "agentalloy:contract" not in result.cleaned_text

    def test_multiple_markers(self) -> None:
        text = (
            "<!-- agentalloy:contract phase=spec slug=a -->A<!-- /agentalloy:contract -->\n"
            "middle\n"
            "<!-- agentalloy:contract phase=sdd-fast slug=b -->B<!-- /agentalloy:contract -->"
        )
        result = extract_contracts(text)
        assert len(result.contracts) == 2
        assert [c.slug for c in result.contracts] == ["a", "b"]


# ---------------------------------------------------------------------------
# write_contracts() — scoped store write
# ---------------------------------------------------------------------------


class TestWriteContracts:
    def test_writes_into_project_root_scope_not_base_default(self, tmp_path: Path) -> None:
        """The marker lands in the session repo's scope, never the base default."""
        db = tmp_path / "state.duck"
        # Bind a base store whose *default* repo is deliberately wrong.
        base = open_state_store(db, repo="process-cwd-default")
        try:
            bind_process_store(base)

            extracted = extract_contracts(
                "<!-- agentalloy:contract phase=spec slug=upstream-config "
                "route=full -->Body text<!-- /agentalloy:contract -->"
            )
            written = write_contracts(
                extracted.contracts, project_root=tmp_path / "the-actual-repo"
            )
            assert written == 1

            # The base (default-repo) handle must NOT see it — it went to the
            # project_root's scope, not the wrong bucket (anomaly D-1 part 3).
            assert base.list_contracts(phase="spec", status="active") == []

            # The scoped handle for project_root *does* see it.
            scoped = _scoped_store(tmp_path / "the-actual-repo")
            assert scoped is not None
            rows = scoped.list_contracts(phase="spec", status="active")
            assert any(r.get("slug") == "upstream-config" for r in rows)
        finally:
            bind_process_store(None)
            base.close()

    def test_extract_and_store_end_to_end(self, tmp_path: Path) -> None:
        db = tmp_path / "state.duck"
        base = open_state_store(db, repo="base")
        try:
            bind_process_store(base)

            text = (
                "Intro.\n"
                "<!-- agentalloy:contract phase=spec slug=my-task route=full -->"
                "The work.\n"
                "<!-- /agentalloy:contract -->\n"
                "Outro."
            )
            result = extract_and_store(text, project_root=tmp_path / "repo")
            assert result.extracted
            # Markers stripped for forwarding.
            assert "agentalloy:contract" not in result.cleaned_text
            scoped = _scoped_store(tmp_path / "repo")
            assert scoped is not None
            rows = scoped.list_contracts(phase="spec", status="active")
            assert any(r.get("slug") == "my-task" for r in rows)
        finally:
            bind_process_store(None)
            base.close()

    def test_no_store_soft_fails(self, tmp_path: Path) -> None:
        # No process store bound -> write is a soft no-op (never raises).
        extracted = extract_contracts(
            "<!-- agentalloy:contract phase=spec slug=s -->b<!-- /agentalloy:contract -->"
        )
        write_contracts(extracted.contracts, project_root=tmp_path)
