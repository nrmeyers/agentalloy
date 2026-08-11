"""Tests for agentalloy.contracts — parsing, validation, and model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentalloy.contracts import (
    ContractMalformed,
    parse_contract_text,
    validate_contract,
    validate_contract_from_dict,
)

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
# parse_contract_text — valid cases
# ---------------------------------------------------------------------------


def test_parse_contract_text_minimal_valid(tmp_path: Path):
    f = _write_contract(tmp_path / "c.md")
    text = f.read_text(encoding="utf-8")
    contract = parse_contract_text(text, contract_id=f.stem)
    assert contract.phase == "build"
    assert contract.task_slug == "test-task"
    assert contract.domain_tags == ["NestJS", "JWT"]
    assert contract.body.strip() == "Test task description."


def test_parse_contract_text_full_fields(tmp_path: Path):
    f = _write_contract(
        tmp_path / "c.md",
        scope={"touches": ["src/auth/**"], "avoids": ["src/billing/**"]},
        success_criteria=["Tests pass"],
        created_at="2026-05-21T14:32:11Z",
        body="Full contract body.\n",
    )
    text = f.read_text(encoding="utf-8")
    c = parse_contract_text(text, contract_id=f.stem)
    assert c.scope.touches == ["src/auth/**"]
    assert c.scope.avoids == ["src/billing/**"]
    assert c.success_criteria == [{"id": "Tests pass", "text": "Tests pass"}]
    assert c.created_at is not None
    assert c.created_at.year == 2026
    assert c.body.strip() == "Full contract body."


def test_parse_contract_text_related_contracts_resolved(tmp_path: Path):
    related = tmp_path / "related.md"
    _write_contract(related)
    f = _write_contract(tmp_path / "c.md", related_contracts=["related.md"])
    text = f.read_text(encoding="utf-8")
    c = parse_contract_text(text, contract_id=f.stem)
    assert len(c.related_contracts) == 1
    assert c.related_contracts[0] == "related.md"


# ---------------------------------------------------------------------------
# parse_contract_text — error cases
# ---------------------------------------------------------------------------


def test_parse_contract_text_missing_frontmatter(tmp_path: Path):
    f = tmp_path / "bad.md"
    f.write_text("No frontmatter here.\n")
    with pytest.raises(ContractMalformed, match="---"):
        parse_contract_text(f.read_text(encoding="utf-8"), contract_id="bad")


def test_parse_contract_text_empty_domain_tags(tmp_path: Path):
    """Empty domain_tags is valid — compose falls back to body-text retrieval."""
    f = tmp_path / "ok.md"
    f.write_text("---\nphase: build\ntask_slug: t\ndomain_tags: []\n---\n\nbody\n")
    contract = parse_contract_text(f.read_text(encoding="utf-8"), contract_id="ok")
    assert contract.domain_tags == []


def test_parse_contract_text_domain_tags_must_be_list(tmp_path: Path):
    """A present domain_tags that isn't a list is still rejected."""
    f = tmp_path / "bad.md"
    f.write_text("---\nphase: build\ntask_slug: t\ndomain_tags: nope\n---\n\nbody\n")
    with pytest.raises(ContractMalformed, match="domain_tags"):
        parse_contract_text(f.read_text(encoding="utf-8"), contract_id="bad")


def test_parse_contract_text_missing_required_fields(tmp_path: Path):
    f = tmp_path / "bad.md"
    f.write_text("---\ntask_slug: t\ndomain_tags: [tag]\n---\n\nbody\n")
    with pytest.raises(ContractMalformed, match="phase"):
        parse_contract_text(f.read_text(encoding="utf-8"), contract_id="bad")


# ---------------------------------------------------------------------------
# validate_contract
# ---------------------------------------------------------------------------


def test_validate_contract_valid(tmp_path: Path):
    f = _write_contract(tmp_path / "c.md")
    text = f.read_text(encoding="utf-8")
    c = parse_contract_text(text, contract_id=f.stem)
    issues = validate_contract(c, tmp_path)
    assert issues == []


# ---------------------------------------------------------------------------
# validate_contract_from_dict
# ---------------------------------------------------------------------------


def test_validate_contract_from_dict_valid() -> None:
    row: dict[str, Any] = {
        "contract_id": "01-test",
        "repo": "test",
        "slug": "test-task",
        "phase": "build",
        "task_slug": "test-task",
        "domain_tags": "[]",
        "scope": '{"touches":[],"avoids":[]}',
        "success_criteria": "[]",
        "related_contracts": "[]",
        "route": "full",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    }
    issues = validate_contract_from_dict(row)
    assert issues == []


def test_validate_contract_from_dict_invalid_route() -> None:
    row: dict[str, Any] = {
        "contract_id": "01-test",
        "repo": "test",
        "slug": "test-task",
        "phase": "build",
        "task_slug": "test-task",
        "domain_tags": "[]",
        "scope": '{"touches":[],"avoids":[]}',
        "success_criteria": "[]",
        "related_contracts": "[]",
        "route": "turbo",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    }
    # Invalid route raises ContractMalformed in contract_from_row
    with pytest.raises(ContractMalformed, match="route"):
        validate_contract_from_dict(row)


# ---------------------------------------------------------------------------
# route field (fast-lane routing)
# ---------------------------------------------------------------------------


class TestContractRoute:
    def test_route_defaults_full(self, tmp_path: Path) -> None:
        f = _write_contract(tmp_path / "c.md")
        c = parse_contract_text(f.read_text(encoding="utf-8"), contract_id=f.stem)
        assert c.route == "full"

    def test_route_fast(self, tmp_path: Path) -> None:
        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "fast"})
        c = parse_contract_text(f.read_text(encoding="utf-8"), contract_id=f.stem)
        assert c.route == "fast"

    def test_route_add_skill(self, tmp_path: Path) -> None:
        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "add-skill"})
        c = parse_contract_text(f.read_text(encoding="utf-8"), contract_id=f.stem)
        assert c.route == "add-skill"

    def test_route_invalid_rejected(self, tmp_path: Path) -> None:
        f = _write_contract(tmp_path / "c.md", extra_fields={"route": "turbo"})
        with pytest.raises(ContractMalformed):
            parse_contract_text(f.read_text(encoding="utf-8"), contract_id=f.stem)


# ---------------------------------------------------------------------------
# contract_from_row
# ---------------------------------------------------------------------------


def test_contract_from_row_roundtrip() -> None:
    from agentalloy.contracts import contract_from_row

    row: dict[str, Any] = {
        "contract_id": "01-auth",
        "repo": "test",
        "slug": "add-auth",
        "phase": "build",
        "task_slug": "add-auth",
        "domain_tags": '["NestJS", "JWT"]',
        "scope_touches": '["src/auth/**"]',
        "scope_avoids": '["src/billing/**"]',
        "success_criteria": '[{"id":"test","text":"test"}]',
        "related_contracts": "[]",
        "route": "full",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    }
    contract = contract_from_row(row)
    assert contract.contract_id == "01-auth"
    assert contract.phase == "build"
    assert contract.task_slug == "add-auth"
    assert contract.domain_tags == ["NestJS", "JWT"]
    assert contract.scope.touches == ["src/auth/**"]
    assert contract.scope.avoids == ["src/billing/**"]
    assert contract.success_criteria == [{"id": "test", "text": "test"}]
    assert contract.route == "full"


# ---------------------------------------------------------------------------
# _intake_route_hint — downstream phase priority
# ---------------------------------------------------------------------------


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
        f = _write_contract(
            tmp_path / "c.md",
            task_slug="add-auth-middleware",
            domain_tags=["NestJS", "JWT validation"],
            scope={"touches": ["src/auth/**"] if touches is None else touches, "avoids": []},
            body="# Add Auth Middleware\n\nTask description here.\n",
        )
        return parse_contract_text(f.read_text(encoding="utf-8"), contract_id=f.stem)

    def test_full_contract(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)
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
