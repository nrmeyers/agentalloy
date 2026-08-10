"""Tests for agentalloy.contracts — parsing, validation, and file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_contract(*, contract_id: str, phase: str, route: str | None) -> None:
    """Seed an active contract row in the store the readers actually consult.

    The autouse ``_bound_state_store`` fixture binds a fresh store per test, so
    this is the same handle ``_intake_route_hint`` resolves through.
    """
    from agentalloy.storage.state_store import process_store

    store = process_store()
    assert store is not None, "the autouse store fixture should have bound one"
    store.execute(
        "INSERT INTO sdd_contract "
        "(repo, contract_id, slug, domain_tags, work_item, phase, route, status, updated_at) "
        "VALUES (?, ?, 't', '[]', NULL, ?, ?, 'active', CURRENT_TIMESTAMP)",
        [store._repo(), contract_id, phase, route],  # pyright: ignore[reportPrivateUsage]
    )


def _write_contract(
    path: Path,
    *,
    phase: str = "build",
    task_slug: str = "test-task",
    domain_tags: list[str] | None = None,
    scope: dict[str, Any] | None = None,
    success_criteria: list[str] | None = None,
    related_contracts: list[str] | None = None,
    created_at: str | None = None,
    body: str = "Test task description.\n",
    extra_fields: dict[str, Any] | None = None,
) -> Path:
    fm: dict[str, Any] = {
        "phase": phase,
        "task_slug": task_slug,
        "domain_tags": domain_tags or ["NestJS", "JWT"],
        "scope": scope or {"touches": [], "avoids": []},
        "success_criteria": success_criteria or [],
        "related_contracts": related_contracts or [],
    }
    if created_at:
        fm["created_at"] = created_at
    if extra_fields:
        fm.update(extra_fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.dump(fm)}---\n\n{body}", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_contract — valid cases
# ---------------------------------------------------------------------------


def test_parse_contract_minimal_valid(tmp_path: Path):
    from agentalloy.contracts import parse_contract

    f = _write_contract(tmp_path / "c.md")
    contract = parse_contract(f)
    assert contract.phase == "build"
    assert contract.task_slug == "test-task"
    assert contract.domain_tags == ["NestJS", "JWT"]
    assert contract.body.strip() == "Test task description."


def test_parse_contract_full_fields(tmp_path: Path):
    from agentalloy.contracts import parse_contract

    f = _write_contract(
        tmp_path / "c.md",
        scope={"touches": ["src/auth/**"], "avoids": ["src/billing/**"]},
        success_criteria=["Tests pass"],
        created_at="2026-05-21T14:32:11Z",
        body="Full contract body.\n",
    )
    c = parse_contract(f)
    assert c.scope.touches == ["src/auth/**"]
    assert c.scope.avoids == ["src/billing/**"]
    assert c.success_criteria == [{"id": "Tests pass", "text": "Tests pass"}]
    assert c.created_at is not None
    assert c.created_at.year == 2026
    assert c.body.strip() == "Full contract body."


def test_parse_contract_related_contracts_resolved(tmp_path: Path):
    from agentalloy.contracts import parse_contract

    related = tmp_path / "related.md"
    _write_contract(related)
    f = _write_contract(tmp_path / "c.md", related_contracts=["related.md"])
    c = parse_contract(f)
    assert len(c.related_contracts) == 1
    assert c.related_contracts[0] == "related.md"


# ---------------------------------------------------------------------------
# parse_contract — error cases
# ---------------------------------------------------------------------------


def test_parse_contract_missing_frontmatter(tmp_path: Path):
    from agentalloy.contracts import ContractMalformed, parse_contract

    f = tmp_path / "bad.md"
    f.write_text("No frontmatter here.\n")
    with pytest.raises(ContractMalformed, match="---"):
        parse_contract(f)


def test_parse_contract_empty_domain_tags(tmp_path: Path):
    """Empty domain_tags is valid — compose falls back to body-text retrieval."""
    from agentalloy.contracts import parse_contract

    f = tmp_path / "ok.md"
    f.write_text("---\nphase: build\ntask_slug: t\ndomain_tags: []\n---\n\nbody\n")
    contract = parse_contract(f)
    assert contract.domain_tags == []


def test_parse_contract_domain_tags_must_be_list(tmp_path: Path):
    """A present domain_tags that isn't a list is still rejected."""
    from agentalloy.contracts import ContractMalformed, parse_contract

    f = tmp_path / "bad.md"
    f.write_text("---\nphase: build\ntask_slug: t\ndomain_tags: nope\n---\n\nbody\n")
    with pytest.raises(ContractMalformed, match="domain_tags"):
        parse_contract(f)


def test_parse_contract_missing_required_fields(tmp_path: Path):
    from agentalloy.contracts import ContractMalformed, parse_contract

    f = tmp_path / "bad.md"
    f.write_text("---\ntask_slug: t\ndomain_tags: [tag]\n---\n\nbody\n")
    with pytest.raises(ContractMalformed, match="phase"):
        parse_contract(f)


# ---------------------------------------------------------------------------
# validate_contract
# ---------------------------------------------------------------------------


def test_validate_contract_phase_mismatch(tmp_path: Path):
    """validate_contract no longer checks phase-file match (store-based model).

    The function validates glob syntax in scope fields. Phase mismatch is now
    handled by the store layer.
    """
    from agentalloy.contracts import parse_contract, validate_contract

    f = _write_contract(tmp_path / "c.md", phase="build")
    c = parse_contract(f)
    issues = validate_contract(c, tmp_path)
    assert issues == []  # no issues for a valid contract with valid globs


def test_validate_contract_related_contracts_missing(tmp_path: Path):
    """validate_contract no longer checks related-contract existence (store-based).

    Related contracts are resolved by the store layer.
    """
    from agentalloy.contracts import parse_contract, validate_contract

    f = _write_contract(tmp_path / "c.md", related_contracts=["nonexistent.md"])
    c = parse_contract(f)
    issues = validate_contract(c, tmp_path)
    assert issues == []  # no issues — existence check removed


def test_validate_contract_valid(tmp_path: Path):
    from agentalloy.contracts import parse_contract, validate_contract

    f = _write_contract(tmp_path / "c.md")
    c = parse_contract(f)
    issues = validate_contract(c, tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# list_contracts_for_phase and latest_contract
# ---------------------------------------------------------------------------


def test_list_contracts_for_phase_mtime_order(tmp_path: Path):
    import time

    from agentalloy.contracts import list_contracts_for_phase

    _write_contract(tmp_path / ".agentalloy" / "contracts" / "active" / "build" / "old.md")
    time.sleep(0.01)
    _write_contract(tmp_path / ".agentalloy" / "contracts" / "active" / "build" / "new.md")

    files = list_contracts_for_phase(tmp_path, "build")
    assert len(files) == 2
    assert files[0].name == "new.md"


def test_list_contracts_for_phase_missing_dir(tmp_path: Path):
    from agentalloy.contracts import list_contracts_for_phase

    files = list_contracts_for_phase(tmp_path, "nonexistent-phase")
    assert files == []


def test_latest_contract_no_phase_filter(tmp_path: Path):
    import time

    from agentalloy.contracts import latest_contract

    _write_contract(tmp_path / ".agentalloy" / "contracts" / "active" / "build" / "build.md")
    time.sleep(0.01)
    _write_contract(tmp_path / ".agentalloy" / "contracts" / "active" / "spec" / "spec.md")

    latest = latest_contract(tmp_path)
    assert latest is not None
    assert latest.name == "spec.md"


# ---------------------------------------------------------------------------
# safe_contract_path — path-containment guard
# ---------------------------------------------------------------------------


def test_safe_contract_path_accepts_valid_contract(tmp_path: Path):
    from agentalloy.contracts import safe_contract_path

    f = _write_contract(tmp_path / ".agentalloy" / "contracts" / "active" / "build" / "task.md")
    safe, project = safe_contract_path(str(f))
    assert safe is not None
    assert project is not None
    assert safe == f.resolve()
    assert project == tmp_path.resolve()


def test_safe_contract_path_rejects_path_outside_agentalloy(tmp_path: Path):
    from agentalloy.contracts import safe_contract_path

    f = _write_contract(tmp_path / "loose.md")
    safe, project = safe_contract_path(str(f))
    assert safe is None
    assert project is None


def test_safe_contract_path_rejects_nonexistent_path(tmp_path: Path):
    from agentalloy.contracts import safe_contract_path

    safe, _ = safe_contract_path(
        str(tmp_path / ".agentalloy" / "contracts" / "active" / "build" / "nope.md")
    )
    assert safe is None


def test_safe_contract_path_rejects_escape_via_parent(tmp_path: Path):
    """A path containing ``..`` that escapes .agentalloy/contracts/ must be rejected."""
    from agentalloy.contracts import safe_contract_path

    # Write a sibling outside contracts/, then try to reach it via .. from inside.
    outside = tmp_path / "outside.md"
    outside.write_text("---\nphase: build\n---\nbody\n")
    sneaky = (
        tmp_path
        / ".agentalloy"
        / "contracts"
        / "active"
        / "build"
        / ".."
        / ".."
        / ".."
        / "outside.md"
    )
    safe, _ = safe_contract_path(str(sneaky))
    assert safe is None


def test_safe_contract_path_rejects_path_outside_pinned_root(tmp_path: Path):
    """When project_root is pinned, paths outside that root must be rejected even if
    they live in a valid .agentalloy/contracts/ tree of a sibling project."""
    from agentalloy.contracts import safe_contract_path

    other_project = tmp_path / "other"
    other_project.mkdir()
    f = _write_contract(
        other_project / ".agentalloy" / "contracts" / "active" / "build" / "task.md"
    )

    safe, _ = safe_contract_path(str(f), project_root=tmp_path / "this-project")
    assert safe is None


# ---------------------------------------------------------------------------
# route field (fast-lane routing)
# ---------------------------------------------------------------------------


class TestContractRoute:
    def test_route_defaults_full(self, tmp_path: Path) -> None:
        from agentalloy.contracts import parse_contract

        c = parse_contract(_write_contract(tmp_path / "c.md"))
        assert c.route == "full"

    def test_route_fast(self, tmp_path: Path) -> None:
        from agentalloy.contracts import parse_contract

        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "fast"})
        assert parse_contract(f).route == "fast"

    def test_route_add_skill(self, tmp_path: Path) -> None:
        from agentalloy.contracts import parse_contract

        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "add-skill"})
        assert parse_contract(f).route == "add-skill"

    def test_route_invalid_rejected(self, tmp_path: Path) -> None:
        from agentalloy.contracts import ContractMalformed, parse_contract

        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "turbo"})
        with pytest.raises(ContractMalformed):
            parse_contract(f)


class TestIntakeRouteHint:
    """_intake_route_hint determines the next lane by checking which downstream
    phase has an active contract (the intake contract was removed):

    - ``spec`` → ``None`` (default sdd-full lane)
    - ``sdd-fast`` → ``"sdd-fast"``
    - ``add-skill`` → ``"add-skill"``
    - ``sdd-flow`` → ``"sdd-flow"``

    The check runs in phase-priority order (spec > sdd-fast > add-skill > sdd-flow);
    the first phase with an active contract wins. Best-effort — any store failure
    returns ``None`` (default full route).
    """

    def _create_downstream_contract(self, tmp_path: Path, phase: str) -> None:
        """Create a downstream contract in the given phase."""
        _write_contract(
            tmp_path / ".agentalloy" / "contracts" / "active" / phase / "t.md",
            phase=phase,
        )
        _insert_contract(contract_id=f"{phase}-t", phase=phase, route=None)

    def test_spec_contract_hints_none(self, tmp_path: Path) -> None:
        """spec contract → None (default sdd-full lane)."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._create_downstream_contract(tmp_path, "spec")
        assert _intake_route_hint(tmp_path) is None

    def test_sdd_fast_contract_hints_sdd_fast(self, tmp_path: Path) -> None:
        """sdd-fast contract → sdd-fast lane."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._create_downstream_contract(tmp_path, "sdd-fast")
        assert _intake_route_hint(tmp_path) == "sdd-fast"

    def test_add_skill_contract_hints_add_skill(self, tmp_path: Path) -> None:
        """add-skill contract → add-skill lane."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._create_downstream_contract(tmp_path, "add-skill")
        assert _intake_route_hint(tmp_path) == "add-skill"

    def test_sdd_flow_contract_hints_sdd_flow(self, tmp_path: Path) -> None:
        """sdd-flow contract → sdd-flow lane."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._create_downstream_contract(tmp_path, "sdd-flow")
        assert _intake_route_hint(tmp_path) == "sdd-flow"

    def test_spec_wins_over_sdd_fast(self, tmp_path: Path) -> None:
        """Priority order: spec checked first, wins over sdd-fast."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._create_downstream_contract(tmp_path, "spec")
        self._create_downstream_contract(tmp_path, "sdd-fast")
        assert _intake_route_hint(tmp_path) is None  # spec wins

    def test_no_contract_hints_none(self, tmp_path: Path) -> None:
        """No downstream contracts → None (default full lane)."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        assert _intake_route_hint(tmp_path) is None

    def test_store_failure_returns_none(self, tmp_path: Path) -> None:
        """Store failures are best-effort → None (default full lane)."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        # Store is already bound by fixture — this just verifies the function
        # doesn't raise.
        assert _intake_route_hint(tmp_path) is None


# ---------------------------------------------------------------------------
# code_index_query_params — contract → /code/search/* query construction
# ---------------------------------------------------------------------------


def _init_git_origin(path: Path, origin_url: str) -> None:
    """Init a real git repo at ``path`` with a single ``origin`` remote."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin_url],
        check=True,
        capture_output=True,
    )


class TestCodeIndexQueryParams:
    def _contract(self, tmp_path: Path, *, touches: list[str] | None = None):
        from agentalloy.contracts import parse_contract

        f = _write_contract(
            tmp_path / "c.md",
            task_slug="add-auth-middleware",
            domain_tags=["NestJS", "JWT validation"],
            scope={"touches": ["src/auth/**"] if touches is None else touches, "avoids": []},
            body="# Add Auth Middleware\n\nTask description here.\n",
        )
        return parse_contract(f)

    def test_full_contract(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)

        # code_index_query_params now takes only the contract (no project_root).
        # It derives the repo from contract.task_slug.
        params = code_index_query_params(contract)
        assert params.repo == "add-auth-middleware"
        assert params.semantic_q == "Add Auth Middleware"
        assert params.lexical_q == "NestJS JWT validation"
        assert "src/auth/**" in params.path_globs

    def test_empty_scope_touches_whole_repo(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path, touches=[])
        params = code_index_query_params(contract)
        assert params.path_globs == []

    def test_non_github_remote_yields_host_qualified_slug(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)
        # Repo slug is derived from task_slug, not git remote
        params = code_index_query_params(contract)
        assert params.repo == "add-auth-middleware"

    def test_no_git_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)
        params = code_index_query_params(contract)
        # Repo is task_slug, not dir name
        assert params.repo == "add-auth-middleware"


# ---------------------------------------------------------------------------
# Contracts tree layout + migration planner
# ---------------------------------------------------------------------------


class TestTreeLayoutHelpers:
    def test_active_and_archive_dirs(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, archive_dir, contracts_root

        assert contracts_root(tmp_path) == tmp_path / ".agentalloy" / "contracts"
        assert active_dir(tmp_path, "build") == (
            tmp_path / ".agentalloy" / "contracts" / "active" / "build"
        )
        assert archive_dir(tmp_path, "qa") == (
            tmp_path / ".agentalloy" / "contracts" / "archive" / "qa"
        )


class TestMigrationPlanner:
    def _c(self, tmp_path: Path, rel: str, *, phase: str = "build") -> Path:
        """Write a contract at contracts/<rel> with the given frontmatter phase."""
        return _write_contract(
            tmp_path / ".agentalloy" / "contracts" / rel, phase=phase, task_slug=Path(rel).stem
        )

    def test_no_contracts_dir_is_empty_plan(self, tmp_path: Path):
        from agentalloy.contracts import plan_contracts_migration

        plan = plan_contracts_migration(tmp_path)
        assert plan.is_empty

    def test_flat_root_file_goes_to_active_by_frontmatter_phase(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, plan_contracts_migration

        src = self._c(tmp_path, "add-auth.md", phase="spec")
        plan = plan_contracts_migration(tmp_path)

        assert len(plan.moves) == 1
        mv = plan.moves[0]
        assert mv.src == src
        assert mv.dst == active_dir(tmp_path, "spec") / "add-auth.md"
        assert mv.archived is False
        assert not plan.collisions and not plan.unreadable

    def test_legacy_per_phase_dir_goes_to_active(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, plan_contracts_migration

        src = self._c(tmp_path, "build/task.md", phase="build")
        plan = plan_contracts_migration(tmp_path)

        assert [m.dst for m in plan.moves] == [active_dir(tmp_path, "build") / "task.md"]
        assert plan.moves[0].src == src

    def test_flat_archive_file_goes_to_archive_phase(self, tmp_path: Path):
        from agentalloy.contracts import archive_dir, plan_contracts_migration

        self._c(tmp_path, "archive/old.md", phase="ship")
        plan = plan_contracts_migration(tmp_path)

        assert len(plan.moves) == 1
        assert plan.moves[0].dst == archive_dir(tmp_path, "ship") / "old.md"
        assert plan.moves[0].archived is True

    def test_superseded_goes_to_archive(self, tmp_path: Path):
        from agentalloy.contracts import archive_dir, plan_contracts_migration

        self._c(tmp_path, "_superseded/dead.md", phase="design")
        plan = plan_contracts_migration(tmp_path)

        assert plan.moves[0].dst == archive_dir(tmp_path, "design") / "dead.md"
        assert plan.moves[0].archived is True

    def test_already_migrated_active_and_archive_are_skipped(self, tmp_path: Path):
        from agentalloy.contracts import plan_contracts_migration

        self._c(tmp_path, "active/build/live.md", phase="build")
        self._c(tmp_path, "archive/qa/done.md", phase="qa")
        plan = plan_contracts_migration(tmp_path)

        assert plan.is_empty

    def test_unreadable_phase_is_reported_not_moved(self, tmp_path: Path):
        from agentalloy.contracts import plan_contracts_migration

        # A markdown file with no valid frontmatter phase.
        bad = tmp_path / ".agentalloy" / "contracts" / "junk.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("no frontmatter here\n", encoding="utf-8")

        plan = plan_contracts_migration(tmp_path)
        assert plan.unreadable == [bad]
        assert not plan.moves

    def test_collision_when_two_sources_target_same_dst(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, plan_contracts_migration

        # Same filename, same frontmatter phase, two legacy locations → one dst.
        self._c(tmp_path, "dup.md", phase="build")  # flat root
        self._c(tmp_path, "build/dup.md", phase="build")  # legacy per-phase
        plan = plan_contracts_migration(tmp_path)

        dst = active_dir(tmp_path, "build") / "dup.md"
        # Exactly one wins as a move; the other is a collision.
        assert len(plan.moves) == 1 and plan.moves[0].dst == dst
        assert len(plan.collisions) == 1 and plan.collisions[0][1] == dst

    def test_collision_when_dst_already_occupied_on_disk(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, plan_contracts_migration

        # Destination already holds a DIFFERENT file (a real prior contract).
        self._c(tmp_path, "active/build/keep.md", phase="build")
        src = self._c(tmp_path, "keep.md", phase="build")  # flat root, same name
        plan = plan_contracts_migration(tmp_path)

        dst = active_dir(tmp_path, "build") / "keep.md"
        assert not plan.moves
        assert plan.collisions == [(src, dst)]

    def test_mixed_repo_plans_all_buckets(self, tmp_path: Path):
        from agentalloy.contracts import active_dir, archive_dir, plan_contracts_migration

        self._c(tmp_path, "intake-item.md", phase="intake")  # → active/intake
        self._c(tmp_path, "spec/s.md", phase="spec")  # → active/spec
        self._c(tmp_path, "archive/a.md", phase="ship")  # → archive/ship
        self._c(tmp_path, "active/build/already.md", phase="build")  # skip
        plan = plan_contracts_migration(tmp_path)

        dsts = {m.dst for m in plan.moves}
        assert dsts == {
            active_dir(tmp_path, "intake") / "intake-item.md",
            active_dir(tmp_path, "spec") / "s.md",
            archive_dir(tmp_path, "ship") / "a.md",
        }
        assert not plan.collisions and not plan.unreadable


class TestMigrationExecutor:
    def _c(self, tmp_path: Path, rel: str, *, phase: str = "build") -> Path:
        return _write_contract(
            tmp_path / ".agentalloy" / "contracts" / rel, phase=phase, task_slug=Path(rel).stem
        )

    def test_apply_moves_files_and_is_idempotent(self, tmp_path: Path):
        from agentalloy.contracts import (
            active_dir,
            apply_contracts_migration,
            plan_contracts_migration,
        )

        src = self._c(tmp_path, "build/01.md", phase="build")
        dst = active_dir(tmp_path, "build") / "01.md"

        plan = plan_contracts_migration(tmp_path)
        done = apply_contracts_migration(plan)

        assert [m.dst for m in done] == [dst]
        assert dst.is_file() and not src.exists()

        # Re-applying the SAME (now-stale) plan is a safe no-op — src is gone.
        assert apply_contracts_migration(plan) == []
        # And a freshly planned repo has nothing left to do.
        assert plan_contracts_migration(tmp_path).is_empty

    def test_apply_never_touches_collisions(self, tmp_path: Path):
        from agentalloy.contracts import (
            active_dir,
            apply_contracts_migration,
            plan_contracts_migration,
        )

        self._c(tmp_path, "active/build/keep.md", phase="build")  # occupies dst
        src = self._c(tmp_path, "keep.md", phase="build")  # collides
        plan = plan_contracts_migration(tmp_path)
        done = apply_contracts_migration(plan)

        assert done == []
        assert src.exists()  # collision left in place, not moved
        assert (active_dir(tmp_path, "build") / "keep.md").is_file()

    def test_cursor_rewritten_to_new_location(self, tmp_path: Path):
        from agentalloy.contracts import (
            contracts_root,
            cursor_after_migration,
            plan_contracts_migration,
        )

        self._c(tmp_path, "build/01.md", phase="build")
        plan = plan_contracts_migration(tmp_path)
        root = contracts_root(tmp_path)

        assert cursor_after_migration("build/01.md", plan.moves, root) == "active/build/01.md"

    def test_cursor_unchanged_when_not_moved_or_absent(self, tmp_path: Path):
        from agentalloy.contracts import contracts_root, cursor_after_migration

        root = contracts_root(tmp_path)
        assert cursor_after_migration(None, [], root) is None
        assert cursor_after_migration("build/other.md", [], root) == "build/other.md"


class TestArchivePlanner:
    def _active(self, tmp_path: Path, phase: str, name: str) -> Path:
        return _write_contract(
            tmp_path / ".agentalloy" / "contracts" / "active" / phase / name,
            phase=phase,
            task_slug=Path(name).stem,
        )

    def test_archive_all_live_phases(self, tmp_path: Path):
        from agentalloy.contracts import archive_dir, plan_archive

        self._active(tmp_path, "build", "01.md")
        self._active(tmp_path, "ship", "s.md")
        plan = plan_archive(tmp_path)

        dsts = {m.dst for m in plan.moves}
        assert dsts == {
            archive_dir(tmp_path, "build") / "01.md",
            archive_dir(tmp_path, "ship") / "s.md",
        }
        assert all(m.archived for m in plan.moves)

    def test_archive_restricted_to_phase(self, tmp_path: Path):
        from agentalloy.contracts import archive_dir, plan_archive

        self._active(tmp_path, "build", "01.md")
        self._active(tmp_path, "ship", "s.md")
        plan = plan_archive(tmp_path, phase="ship")

        assert [m.dst for m in plan.moves] == [archive_dir(tmp_path, "ship") / "s.md"]

    def test_archive_restricted_to_slug(self, tmp_path: Path):
        from agentalloy.contracts import plan_archive

        self._active(tmp_path, "build", "keep-me.md")
        self._active(tmp_path, "build", "other.md")
        plan = plan_archive(tmp_path, slug="keep-me")

        assert [m.src.name for m in plan.moves] == ["keep-me.md"]

    def test_archive_collision_when_dst_occupied(self, tmp_path: Path):
        from agentalloy.contracts import archive_dir, plan_archive

        self._active(tmp_path, "build", "dup.md")
        occ = archive_dir(tmp_path, "build") / "dup.md"
        occ.parent.mkdir(parents=True, exist_ok=True)
        occ.write_text("different")
        plan = plan_archive(tmp_path)

        assert not plan.moves and len(plan.collisions) == 1

    def test_archive_empty_when_no_active(self, tmp_path: Path):
        from agentalloy.contracts import plan_archive

        assert plan_archive(tmp_path).is_empty
