"""Unit tests for `agentalloy contract init` gate-derived doc scaffolding.

Covers the two pure helpers added for feedback items F/G: `_concretize_glob` (glob ->
concrete repo-relative path) and `_scaffold_phase_docs` (seed each artifact_contains gate's
file with its required `## Section` headings, never overwriting).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentalloy.install.subcommands import contract as contract_cmd
from agentalloy.install.subcommands.contract import (
    _active_design_slug,
    _concretize_glob,
    _inject_work_item,
    _scaffold_phase_docs,
)


class TestInitTemplateSubstitution:
    """Regression guard: `{{task_slug_title}}` must fully resolve.

    sdd-fast.yaml and sdd-add-skill.yaml's contract_template use the
    double-brace `{{task_slug_title}}` token, matching every other token's
    double-brace convention in these templates ({{phase}}, {{task_slug}},
    ...). `_init`'s replace chain only had a single-brace `{task_slug_title}`
    substitution, so the double braces never fully resolved — every
    sdd-fast/add-skill contract's heading rendered as a literal
    "# {Knowledge Dogfooding}" (stray braces), not "# Knowledge Dogfooding".
    """

    def _init_and_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> str:
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.chdir(tmp_path)
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        contract_cmd.add_parser(sub)
        args = parser.parse_args(
            ["contract", "init", "--phase", phase, "--slug", "my-cool-task", "--route", "fast"]
        )
        args.func(args)
        return (tmp_path / ".agentalloy" / "contracts" / phase / "my-cool-task.md").read_text()

    def test_sdd_fast_heading_has_no_stray_braces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = self._init_and_read(tmp_path, monkeypatch, "sdd-fast")
        assert "# My Cool Task" in content
        assert "{" not in content.split("---", 2)[2]  # body, past the frontmatter
        assert "}" not in content.split("---", 2)[2]

    def test_add_skill_heading_has_no_stray_braces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = self._init_and_read(tmp_path, monkeypatch, "add-skill")
        assert "# My Cool Task" in content
        assert "{" not in content.split("---", 2)[2]
        assert "}" not in content.split("---", 2)[2]


class TestConcretizeGlob:
    def test_doublestar_segment_replaced_with_slug(self) -> None:
        assert (
            _concretize_glob("docs/design/**/approach.md", "feat") == "docs/design/feat/approach.md"
        )

    def test_slug_placeholder_replaced(self) -> None:
        assert _concretize_glob("docs/spec/<slug>.md", "feat") == "docs/spec/feat.md"

    def test_terminal_basename_wildcard_uses_slug(self) -> None:
        # A terminal basename wildcard names the per-feature artifact after the slug
        # (the qa/spec gate convention: docs/qa/*.md -> docs/qa/<slug>.md).
        assert _concretize_glob("docs/qa/*.md", "feat") == "docs/qa/feat.md"
        assert _concretize_glob("docs/spec/*.md", "feat") == "docs/spec/feat.md"

    def test_non_terminal_wildcard_returns_none(self) -> None:
        # A wildcard in a non-final segment is genuinely ambiguous (multi-dir match)
        # and must NOT be scaffolded to a single file.
        assert _concretize_glob("docs/design/*/approach.md", "feat") is None
        assert _concretize_glob("docs/*/*.md", "feat") is None


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

    def test_qa_scaffolds_slug_named_doc_with_headings(self, tmp_path: Path) -> None:
        # Regression for B4: the qa gate glob `docs/qa/*.md` (bare `*`) previously
        # concretized to None and scaffolded nothing. It must seed docs/qa/<slug>.md.
        created = _scaffold_phase_docs("qa", "big-calendar-ui", tmp_path)
        doc = tmp_path / "docs" / "qa" / "big-calendar-ui.md"
        assert doc.exists()
        text = doc.read_text()
        assert "## Checks" in text
        assert "## Review" in text
        assert created == ["docs/qa/big-calendar-ui.md"]

    def test_spec_scaffolds_slug_named_doc(self, tmp_path: Path) -> None:
        created = _scaffold_phase_docs("spec", "big-calendar-ui", tmp_path)
        doc = tmp_path / "docs" / "spec" / "big-calendar-ui.md"
        assert doc.exists()
        assert "## Acceptance Criteria" in doc.read_text()
        assert created == ["docs/spec/big-calendar-ui.md"]

    def test_never_overwrites_existing_file(self, tmp_path: Path) -> None:
        base = tmp_path / "docs" / "design" / "feat"
        base.mkdir(parents=True)
        (base / "approach.md").write_text("KEEP ME\n")
        created = _scaffold_phase_docs("design", "feat", tmp_path)
        assert (base / "approach.md").read_text() == "KEEP ME\n"
        assert "docs/design/feat/approach.md" not in created


class TestWorkItemStamp:
    """The #378 build-contract → design-item link stamped by `contract init`."""

    def _seed_design(self, tmp_path: Path, slug: str) -> None:
        d = tmp_path / ".agentalloy" / "contracts" / "design"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(f"---\nphase: design\ntask_slug: {slug}\n---\n\n# {slug}\n")

    def test_active_design_slug_from_sole_contract(self, tmp_path: Path) -> None:
        self._seed_design(tmp_path, "knowledge-module")
        assert _active_design_slug(tmp_path) == "knowledge-module"

    def test_active_design_slug_none_when_ambiguous(self, tmp_path: Path) -> None:
        # Two design items, no cursor → can't attribute → None (caller omits stamp).
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        assert _active_design_slug(tmp_path) is None

    def test_active_design_slug_honors_cursor(self, tmp_path: Path) -> None:
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        (tmp_path / ".agentalloy" / "cursor").write_text("design/b.md")
        assert _active_design_slug(tmp_path) == "b"

    def test_active_design_slug_rejects_cross_phase_cursor(self, tmp_path: Path) -> None:
        # A cursor drifted to another phase must not mislabel the build contract.
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        ship = tmp_path / ".agentalloy" / "contracts" / "ship"
        ship.mkdir(parents=True)
        (ship / "other.md").write_text("---\nphase: ship\n---\n\n# other\n")
        (tmp_path / ".agentalloy" / "cursor").write_text("ship/other.md")
        assert _active_design_slug(tmp_path) is None  # not under contracts/design/

    def test_inject_adds_work_item_after_task_slug(self) -> None:
        content = "---\nphase: build\ntask_slug: 01-store\nroute: full\n---\n\n# x\n"
        out = _inject_work_item(content, "knowledge-module")
        assert "task_slug: 01-store\nwork_item: knowledge-module\nroute: full" in out

    def test_inject_noop_when_slug_none_or_already_present(self) -> None:
        content = "---\nphase: build\ntask_slug: 01-store\n---\n\n# x\n"
        assert _inject_work_item(content, None) == content
        stamped = _inject_work_item(content, "km")
        assert _inject_work_item(stamped, "other") == stamped  # idempotent, no second line
