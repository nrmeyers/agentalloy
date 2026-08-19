"""Unit tests for `agentalloy contract init` gate-derived doc scaffolding.

Covers the two pure helpers added for feedback items F/G: `_concretize_glob` (glob ->
concrete repo-relative path) and `_scaffold_phase_docs` (seed each artifact_contains gate's
file with its required `## Section` headings, never overwriting).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from agentalloy.api.state_client import StateClient
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

    def _substitute_template(self, phase: str, slug: str, route: str) -> str:
        """Load the template for *phase* and run the same substitution chain
        as ``_init`` uses, returning the resulting content string."""
        template = contract_cmd._load_contract_template(phase)
        if template is None:
            template = (
                "---\n"
                "phase: {phase}\n"
                "task_slug: {task_slug}\n"
                "route: {route}\n"
                "domain_tags: []\n"
                "scope:\n"
                "  touches: []\n"
                "  avoids: []\n"
                "success_criteria: []\n"
                "created_at: {created_at}\n"
                "---\n\n"
                "# {task_slug_title}\n\n"
                "## Task description\n\n"
                "<fill in what you intend to do and why>\n"
            )

        from datetime import UTC, datetime

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        title = slug.replace("-", " ").title()
        return (
            template.replace("{{phase}}", phase)
            .replace("{{task_slug}}", slug)
            .replace("{{created_at}}", now)
            .replace("{{route}}", route)
            .replace("{{task_slug_title}}", title)
            .replace("{phase}", phase)
            .replace("{task_slug}", slug)
            .replace("{created_at}", now)
            .replace("{route}", route)
            .replace("{task_slug_title}", title)
        )

    def test_sdd_fast_heading_has_no_stray_braces(self) -> None:
        content = self._substitute_template("sdd-fast", "my-cool-task", "sdd-fast")
        assert "# My Cool Task" in content
        assert "{" not in content.split("---", 2)[2]  # body, past the frontmatter
        assert "}" not in content.split("---", 2)[2]

    def test_add_skill_heading_has_no_stray_braces(self) -> None:
        content = self._substitute_template("add-skill", "my-cool-task", "add-skill")
        assert "# My Cool Task" in content
        assert "{" not in content.split("---", 2)[2]
        assert "}" not in content.split("---", 2)[2]

    def test_init_end_to_end_sdd_fast(self, tmp_path: Path) -> None:
        """Drive `_init` end to end against a stubbed service (sdd-fast route).

        The service is required for storage; stub it so the command can run
        without a live server. Assert the stored contract body has no stray
        braces — the original regression that hollowed out this test.
        """
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands.contract import _init

        captured: list[dict] = []

        def fake_create_contract(payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["contract_id"]}

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.create_contract = fake_create_contract

        args = argparse.Namespace(
            phase="sdd-fast",
            slug="my-cool-task",
            route="sdd-fast",
            json=False,
            quiet=True,
        )
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch("agentalloy.install.state._repo_root", return_value=tmp_path):
                rc = _init(args)
        assert rc == 0
        assert len(captured) == 1
        body = captured[0]["body"]
        assert "# My Cool Task" in body
        # Body portion (after frontmatter) must not contain stray braces
        body_part = body.split("---", 2)[2]
        assert "{" not in body_part
        assert "}" not in body_part

    def test_init_end_to_end_full_route(self, tmp_path: Path) -> None:
        """Drive `_init` end to end against a stubbed service (full route).

        Verifies the default "full" route produces a fully resolved contract
        with correct phase and slug in the stored payload.
        """
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands.contract import _init

        captured: list[dict] = []

        def fake_create_contract(payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["contract_id"]}

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.create_contract = fake_create_contract

        args = argparse.Namespace(
            phase="build",
            slug="01-auth",
            route="full",
            json=False,
            quiet=True,
        )
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch("agentalloy.install.state._repo_root", return_value=tmp_path):
                rc = _init(args)
        assert rc == 0
        assert len(captured) == 1
        assert captured[0]["phase"] == "build"
        assert captured[0]["slug"] == "01-auth"
        assert captured[0]["route"] == "full"
        body = captured[0]["body"]
        assert "# 01-auth" in body or "# 01 Auth" in body  # template-dependent


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
    def test_design_scaffolds_nothing_post_migration(self, tmp_path: Path) -> None:
        # spec/design moved to the artifact store (specs/final_migration.md): their
        # exit gates carry `phase`/`name`, not a `path` glob, so
        # `_extract_artifact_contains_specs` (which requires `path`) finds nothing
        # to scaffold — no docs/design/ files appear anymore.
        created = _scaffold_phase_docs("design", "calendar-web-ui", tmp_path)
        assert created == []
        assert not (tmp_path / "docs" / "design").exists()

    def test_qa_scaffolds_nothing_post_migration(self, tmp_path: Path) -> None:
        # qa is store-backed (phase/name gate, no path glob): its verdict is
        # recorded via `agentalloy contract artifact-set --phase qa`, never a
        # disk stub. Post-migration `_scaffold_phase_docs` must therefore
        # produce no docs/qa/ tree at all — lifecycle artifacts live only in
        # the store, and a disk write here would be an invisible orphan the
        # qa exit gate never reads.
        created = _scaffold_phase_docs("qa", "big-calendar-ui", tmp_path)
        assert created == []
        assert not (tmp_path / "docs" / "qa").exists()

    def test_spec_scaffolds_nothing_post_migration(self, tmp_path: Path) -> None:
        # Same reasoning as design above — spec's exit gate is store-backed now.
        created = _scaffold_phase_docs("spec", "big-calendar-ui", tmp_path)
        assert created == []
        assert not (tmp_path / "docs" / "spec").exists()

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
        d = tmp_path / ".agentalloy" / "contracts" / "active" / "design"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.md").write_text(f"---\nphase: design\ntask_slug: {slug}\n---\n\n# {slug}\n")

    def _wire_store(self, tmp_path: Path) -> None:
        """Create a DuckDB store, bind it, and seed design contracts from disk."""
        from agentalloy.api.state_router import _repo_key_for, _stream_key_for
        from agentalloy.storage.state_store import bind_process_store, open_state_store

        store = open_state_store(tmp_path / ".agentalloy" / "state.db")
        # Scope to the test's tmp_path so writes and reads match
        scoped = store.for_repo(
            _repo_key_for(str(tmp_path)), stream_id=_stream_key_for(str(tmp_path))
        )
        bind_process_store(scoped)
        # Seed contracts from disk into store
        contracts_dir = tmp_path / ".agentalloy" / "contracts" / "active" / "design"
        if contracts_dir.is_dir():
            for md_file in sorted(contracts_dir.glob("*.md")):
                content = md_file.read_text()
                front = content.split("---", 2)
                meta = {}
                if len(front) >= 3:
                    import yaml

                    meta = yaml.safe_load(front[1]) or {}
                slug = meta.get("task_slug", md_file.stem)
                scoped.put_contract(
                    md_file.stem,
                    phase="design",
                    slug=slug,
                    body=front[2].strip() if len(front) >= 3 else content.strip(),
                )
        # Seed cursor from disk into store
        cursor_file = tmp_path / ".agentalloy" / "cursor"
        if cursor_file.is_file():
            cursor_val = cursor_file.read_text().strip()
            if cursor_val:
                scoped.write("cursor", cursor_val)

    def test_active_design_slug_from_sole_contract(self, tmp_path: Path) -> None:
        self._seed_design(tmp_path, "knowledge-module")
        self._wire_store(tmp_path)
        assert _active_design_slug(tmp_path) == "knowledge-module"

    def test_active_design_slug_none_when_ambiguous(self, tmp_path: Path) -> None:
        # Two design items, no cursor → can't attribute → None (caller omits stamp).
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        self._wire_store(tmp_path)
        assert _active_design_slug(tmp_path) is None

    def test_active_design_slug_honors_cursor(self, tmp_path: Path) -> None:
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        (tmp_path / ".agentalloy" / "cursor").write_text("active/design/b.md")
        self._wire_store(tmp_path)
        assert _active_design_slug(tmp_path) == "b"

    def test_active_design_slug_rejects_cross_phase_cursor(self, tmp_path: Path) -> None:
        # A cursor drifted to another phase must not mislabel the build contract.
        self._seed_design(tmp_path, "a")
        self._seed_design(tmp_path, "b")
        ship = tmp_path / ".agentalloy" / "contracts" / "active" / "ship"
        ship.mkdir(parents=True)
        (ship / "other.md").write_text("---\nphase: ship\n---\n\n# other\n")
        (tmp_path / ".agentalloy" / "cursor").write_text("active/ship/other.md")
        self._wire_store(tmp_path)
        assert _active_design_slug(tmp_path) is None  # not under contracts/active/design/

    def test_inject_adds_work_item_after_task_slug(self) -> None:
        content = "---\nphase: build\ntask_slug: 01-store\nroute: full\n---\n\n# x\n"
        out = _inject_work_item(content, "knowledge-module")
        assert "task_slug: 01-store\nwork_item: knowledge-module\nroute: full" in out

    def test_inject_noop_when_slug_none_or_already_present(self) -> None:
        content = "---\nphase: build\ntask_slug: 01-store\n---\n\n# x\n"
        assert _inject_work_item(content, None) == content
        stamped = _inject_work_item(content, "km")
        assert _inject_work_item(stamped, "other") == stamped  # idempotent, no second line


class TestInitBodyFileFrontmatter:
    """B3: `contract init --body-file` must not emit a second frontmatter block.

    When the supplied body already carries YAML frontmatter, `_init` merges it
    over the template's block (body's content fields win; the template supplies
    the identity fields) so the stored contract has exactly one frontmatter
    block, and forwards the parsed structured fields to the store (the read
    path pulls them from stored columns, not a body re-parse).
    """

    def _run_init(self, tmp_path: Path, *, body: str | None, body_file: str | None):
        from unittest.mock import MagicMock

        from agentalloy.install.subcommands.contract import _init

        captured: list[dict] = []

        def fake_create_contract(payload: dict) -> dict:
            captured.append(payload)
            return {"contract_id": payload["contract_id"]}

        mock_client = MagicMock(spec=StateClient)
        mock_client.is_running.return_value = True
        mock_client.create_contract = fake_create_contract

        args = argparse.Namespace(
            phase="spec",
            slug="my-task",
            route="full",
            body=body,
            body_file=body_file,
            json=False,
            quiet=True,
        )
        with patch("agentalloy.install.subcommands.contract.StateClient", return_value=mock_client):
            with patch("agentalloy.install.state._repo_root", return_value=tmp_path):
                rc = _init(args)
        return rc, captured

    def test_body_with_frontmatter_yields_single_block(self, tmp_path: Path) -> None:
        body = (
            "---\n"
            "domain_tags:\n"
            "  - NestJS\n"
            "  - JWT\n"
            "scope:\n"
            "  touches:\n"
            "    - src/auth/**\n"
            "  avoids:\n"
            "    - src/billing/**\n"
            "success_criteria:\n"
            "  - Existing auth tests still pass\n"
            "---\n\n"
            "# My Task\n\n"
            "Prose body.\n"
        )
        rc, captured = self._run_init(tmp_path, body=body, body_file=None)
        assert rc == 0
        assert len(captured) == 1
        content = captured[0]["body"]
        # Exactly one frontmatter block: the text starts with '---', has exactly
        # one more '---' delimiter, and nothing else.
        assert content.startswith("---\n")
        assert content.count("\n---\n") == 1
        # The body's prose survived the merge.
        assert "# My Task" in content
        assert "Prose body." in content
        # Structured fields were forwarded to the store payload.
        assert captured[0]["scope_touches"] == ["src/auth/**"]
        assert captured[0]["scope_avoids"] == ["src/billing/**"]
        assert captured[0]["domain_tags"] == ["NestJS", "JWT"]
        assert captured[0]["success_criteria"] == ["Existing auth tests still pass"]

    def test_identity_fields_come_from_template(self, tmp_path: Path) -> None:
        # A body that tries to override the identity fields must not — the
        # template (i.e. the CLI args) supplies phase/task_slug/route.
        body = (
            "---\n"
            "phase: build\n"
            "task_slug: rogue-slug\n"
            "route: fast\n"
            "scope:\n"
            "  touches:\n"
            "    - src/x/**\n"
            "---\n\n"
            "# Body\n"
        )
        rc, captured = self._run_init(tmp_path, body=body, body_file=None)
        assert rc == 0
        content = captured[0]["body"]
        # The stored identity fields reflect the CLI args, not the body.
        assert captured[0]["phase"] == "spec"
        assert captured[0]["slug"] == "my-task"
        assert captured[0]["route"] == "full"
        # The frontmatter block in the body carries the template's identity.
        assert "phase: spec" in content
        assert "task_slug: my-task" in content
        assert "route: full" in content
        # The body's content field still won where it was allowed.
        assert captured[0]["scope_touches"] == ["src/x/**"]

    def test_body_without_frontmatter_keeps_template_block(self, tmp_path: Path) -> None:
        rc, captured = self._run_init(
            tmp_path, body="# Just prose\n\nNo frontmatter here.\n", body_file=None
        )
        assert rc == 0
        content = captured[0]["body"]
        assert content.startswith("---\n")
        assert content.count("\n---\n") == 1
        assert "# Just prose" in content
        # No structured fields forwarded (body had none).
        assert "scope_touches" not in captured[0]
        assert "success_criteria" not in captured[0]

    def test_body_file_path_is_read(self, tmp_path: Path) -> None:
        body = (
            "---\n"
            "scope:\n"
            "  touches:\n"
            "    - src/y/**\n"
            "---\n\n"
            "# From File\n"
        )
        (tmp_path / "body.md").write_text(body)
        rc, captured = self._run_init(
            tmp_path, body=None, body_file=str(tmp_path / "body.md")
        )
        assert rc == 0
        content = captured[0]["body"]
        assert content.count("\n---\n") == 1
        assert "# From File" in content
        assert captured[0]["scope_touches"] == ["src/y/**"]

    def test_malformed_frontmatter_treated_as_prose(self, tmp_path: Path) -> None:
        # Starts with '---' but the block is not closed — keep the whole body as
        # prose. The template's identity block is preserved so the stored contract
        # still parses as a SINGLE frontmatter contract (the stray '---' in the
        # prose is a horizontal rule, not a second parsed block).
        from agentalloy.contracts import _split_frontmatter

        body = "---\nunclosed frontmatter\nstill prose\n"
        rc, captured = self._run_init(tmp_path, body=body, body_file=None)
        assert rc == 0
        content = captured[0]["body"]
        assert content.startswith("---\n")
        # Parses as exactly one frontmatter block with the template's identity.
        fm, prose = _split_frontmatter(content)
        assert fm["phase"] == "spec"
        assert fm["task_slug"] == "my-task"
        assert "unclosed frontmatter" in prose
