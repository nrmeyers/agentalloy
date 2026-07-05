"""Tests for agentalloy.contracts — parsing, validation, and file discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    assert c.success_criteria == ["Tests pass"]
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
    assert c.related_contracts[0].is_absolute()


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
    from agentalloy.contracts import parse_contract, validate_contract

    # Write a phase file saying 'design'
    phase_file = tmp_path / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True)
    phase_file.write_text("phase: design\n")

    f = _write_contract(tmp_path / "c.md", phase="build")
    c = parse_contract(f)
    issues = validate_contract(c, tmp_path)
    assert any("design" in i and "build" in i for i in issues)


def test_validate_contract_related_contracts_missing(tmp_path: Path):
    from agentalloy.contracts import parse_contract, validate_contract

    f = _write_contract(tmp_path / "c.md", related_contracts=["nonexistent.md"])
    c = parse_contract(f)
    issues = validate_contract(c, tmp_path)
    assert any("nonexistent" in i for i in issues)


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

    _write_contract(tmp_path / ".agentalloy" / "contracts" / "build" / "old.md")
    time.sleep(0.01)
    _write_contract(tmp_path / ".agentalloy" / "contracts" / "build" / "new.md")

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

    _write_contract(tmp_path / ".agentalloy" / "contracts" / "build" / "build.md")
    time.sleep(0.01)
    _write_contract(tmp_path / ".agentalloy" / "contracts" / "spec" / "spec.md")

    latest = latest_contract(tmp_path)
    assert latest is not None
    assert latest.name == "spec.md"


# ---------------------------------------------------------------------------
# safe_contract_path — path-containment guard
# ---------------------------------------------------------------------------


def test_safe_contract_path_accepts_valid_contract(tmp_path: Path):
    from agentalloy.contracts import safe_contract_path

    f = _write_contract(tmp_path / ".agentalloy" / "contracts" / "build" / "task.md")
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

    safe, _ = safe_contract_path(str(tmp_path / ".agentalloy" / "contracts" / "build" / "nope.md"))
    assert safe is None


def test_safe_contract_path_rejects_escape_via_parent(tmp_path: Path):
    """A path containing ``..`` that escapes .agentalloy/contracts/ must be rejected."""
    from agentalloy.contracts import safe_contract_path

    # Write a sibling outside contracts/, then try to reach it via .. from inside.
    outside = tmp_path / "outside.md"
    outside.write_text("---\nphase: build\n---\nbody\n")
    sneaky = tmp_path / ".agentalloy" / "contracts" / "build" / ".." / ".." / ".." / "outside.md"
    safe, _ = safe_contract_path(str(sneaky))
    assert safe is None


def test_safe_contract_path_rejects_path_outside_pinned_root(tmp_path: Path):
    """When project_root is pinned, paths outside that root must be rejected even if
    they live in a valid .agentalloy/contracts/ tree of a sibling project."""
    from agentalloy.contracts import safe_contract_path

    other_project = tmp_path / "other"
    other_project.mkdir()
    f = _write_contract(other_project / ".agentalloy" / "contracts" / "build" / "task.md")

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
    """_intake_route_hint is authoritative on the intake contract's ``route`` field:
    ``fast`` → sdd-fast lane, ``full`` → full lane (spec). When no intake contract is
    readable it falls back to the prior-authors-next cascade (contracts/sdd-fast/)."""

    def _write_intake(self, tmp_path: Path, route: str) -> None:
        _write_contract(
            tmp_path / ".agentalloy" / "contracts" / "intake" / "t.md",
            phase="intake",
            extra_fields={"route": route},
        )

    def test_fast_route_field_hints_sdd_fast(self, tmp_path: Path) -> None:
        """route: fast is honored from the field alone — no sdd-fast/ folder needed."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._write_intake(tmp_path, "fast")
        assert _intake_route_hint(tmp_path) == "sdd-fast"

    def test_add_skill_route_field_hints_add_skill(self, tmp_path: Path) -> None:
        """route: add-skill routes to the custom-skill authoring lane (1:1 with
        the phase name — no fast/sdd-fast style indirection)."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._write_intake(tmp_path, "add-skill")
        assert _intake_route_hint(tmp_path) == "add-skill"

    def test_full_route_field_hints_none(self, tmp_path: Path) -> None:
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._write_intake(tmp_path, "full")
        assert _intake_route_hint(tmp_path) is None

    def test_full_route_field_wins_over_stray_fast_folder(self, tmp_path: Path) -> None:
        """The field is authoritative: route: full → full lane even if a stray
        contracts/sdd-fast/ work-item exists (inverse-disagreement guard)."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        self._write_intake(tmp_path, "full")
        _write_contract(
            tmp_path / ".agentalloy" / "contracts" / "sdd-fast" / "stray.md",
            phase="sdd-fast",
            extra_fields={"route": "fast"},
        )
        assert _intake_route_hint(tmp_path) is None

    def test_no_intake_contract_falls_back_to_fast_folder(self, tmp_path: Path) -> None:
        """Cascade fallback preserved: no intake contract + a sdd-fast/ work-item
        → fast lane."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        _write_contract(
            tmp_path / ".agentalloy" / "contracts" / "sdd-fast" / "t.md",
            phase="sdd-fast",
            extra_fields={"route": "fast"},
        )
        assert _intake_route_hint(tmp_path) == "sdd-fast"

    def test_malformed_intake_contract_falls_back(self, tmp_path: Path) -> None:
        """A malformed intake contract doesn't raise; falls back to directory
        presence (here: none) → full lane."""
        from agentalloy.signals.skill_loader import _intake_route_hint

        bad = tmp_path / ".agentalloy" / "contracts" / "intake" / "bad.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("no frontmatter here\n", encoding="utf-8")
        assert _intake_route_hint(tmp_path) is None

    def test_no_contract_hints_none(self, tmp_path: Path) -> None:
        from agentalloy.signals.skill_loader import _intake_route_hint

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
        _init_git_origin(tmp_path, "git@github.com:nrmeyers/agentalloy.git")

        params = code_index_query_params(contract, tmp_path)

        # Canonical slug — byte-identical to the key the code-index module
        # stores each per-repo index under.
        assert params.repo == "nrmeyers__agentalloy"
        assert params.semantic_q == "Add Auth Middleware"
        assert params.lexical_q == "NestJS JWT validation"
        assert "src/auth/**" in params.path_globs

    def test_empty_scope_touches_whole_repo(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path, touches=[])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            params = code_index_query_params(contract, tmp_path)

        assert params.path_globs == []

    def test_non_github_remote_falls_back_to_basename(self, tmp_path: Path) -> None:
        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)
        _init_git_origin(tmp_path, "https://gitlab.com/myorg/myrepo.git")

        # Non-GitHub host → directory basename, same as the slug rule the
        # code-index module keys its indexes by off GitHub.
        params = code_index_query_params(contract, tmp_path)
        assert params.repo == tmp_path.name

    def test_no_git_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from agentalloy.contracts import code_index_query_params

        contract = self._contract(tmp_path)
        with patch("subprocess.run", side_effect=OSError("no git")):
            params = code_index_query_params(contract, tmp_path)

        assert params.repo == tmp_path.name
