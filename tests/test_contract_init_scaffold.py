"""Unit tests for `agentalloy contract init` gate-derived doc scaffolding.

Covers the two pure helpers added for feedback items F/G: `_concretize_glob` (glob ->
concrete repo-relative path) and `_scaffold_phase_docs` (seed each artifact_contains gate's
file with its required `## Section` headings, never overwriting).
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.install.subcommands.contract import _concretize_glob, _scaffold_phase_docs


class TestConcretizeGlob:
    def test_doublestar_segment_replaced_with_slug(self) -> None:
        assert (
            _concretize_glob("docs/design/**/approach.md", "feat") == "docs/design/feat/approach.md"
        )

    def test_slug_placeholder_replaced(self) -> None:
        assert _concretize_glob("docs/spec/<slug>.md", "feat") == "docs/spec/feat.md"

    def test_remaining_wildcard_returns_none(self) -> None:
        # An ambiguous multi-match glob must NOT be scaffolded to a single file.
        assert _concretize_glob(".agentalloy/contracts/build/*.md", "feat") is None


class TestScaffoldPhaseDocs:
    def test_design_scaffolds_three_docs_with_required_headings(self, tmp_path: Path) -> None:
        created = _scaffold_phase_docs("design", "calendar-web-ui", tmp_path)
        base = tmp_path / "docs" / "design" / "calendar-web-ui"
        assert "## Approach" in (base / "approach.md").read_text()
        assert "## Tasks" in (base / "tasks.md").read_text()
        assert "## Test Cases" in (base / "test-plan.md").read_text()
        assert sorted(created) == [
            "docs/design/calendar-web-ui/approach.md",
            "docs/design/calendar-web-ui/tasks.md",
            "docs/design/calendar-web-ui/test-plan.md",
        ]

    def test_never_overwrites_existing_file(self, tmp_path: Path) -> None:
        base = tmp_path / "docs" / "design" / "feat"
        base.mkdir(parents=True)
        (base / "approach.md").write_text("KEEP ME\n")
        created = _scaffold_phase_docs("design", "feat", tmp_path)
        assert (base / "approach.md").read_text() == "KEEP ME\n"
        assert "docs/design/feat/approach.md" not in created
