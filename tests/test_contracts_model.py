"""Tests for contract model functions in src/agentalloy/contracts.py.

Covers validate_contract_from_dict for:
- A valid store row
- A malformed row (missing required frontmatter fields)
- A row whose phase is ahead of the active phase

Test cases from docs/design/contract-store-and-write-gating/test-plan.md.
"""

from __future__ import annotations

import pytest

from agentalloy.contracts import (
    ContractMalformed,
    contract_from_row,
    parse_contract_text,
    validate_contract,
    validate_contract_from_dict,
)

# ---------------------------------------------------------------------------
# validate_contract_from_dict — valid row
# ---------------------------------------------------------------------------


class TestValidateContractFromDictValid:
    """validate_contract_from_dict returns [] for a well-formed store row."""

    def test_valid_minimal_row(self) -> None:
        row = {
            "contract_id": "build/01-auth",
            "phase": "build",
            "slug": "01-auth",
            "body": "# Auth Middleware\n\nSome description.",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        issues = validate_contract_from_dict(row)
        assert issues == []

    def test_valid_full_row(self) -> None:
        row = {
            "contract_id": "build/02-api",
            "phase": "build",
            "slug": "02-api",
            "work_item": "calendar-ui",
            "route": "full",
            "domain_tags": ["api-design", "state-management"],
            "scope_touches": ["src/api/", "tests/api/"],
            "scope_avoids": ["src/billing/"],
            "success_criteria": ["API tests pass", "No regressions"],
            "body": "# API Layer\n\nImplement the REST API.",
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        issues = validate_contract_from_dict(row)
        assert issues == []

    def test_valid_row_with_empty_scope(self) -> None:
        """Empty scope lists are valid (not all contracts touch files)."""
        row = {
            "contract_id": "design/01-approach",
            "phase": "design",
            "slug": "01-approach",
            "scope_touches": [],
            "scope_avoids": [],
            "body": "# Approach\n\nDesign doc.",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        assert issues == []


# ---------------------------------------------------------------------------
# validate_contract_from_dict — malformed row
# ---------------------------------------------------------------------------


class TestValidateContractFromDictMalformed:
    """validate_contract_from_dict catches missing or empty required fields."""

    def test_missing_phase(self) -> None:
        row = {
            "contract_id": "build/01-bad",
            "slug": "01-bad",
            "body": "# Bad",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        assert any("phase" in issue.lower() or "empty" in issue.lower() for issue in issues)

    def test_missing_slug(self) -> None:
        row = {
            "contract_id": "build/01-bad",
            "phase": "build",
            "body": "# Bad",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        assert any("task_slug" in issue.lower() or "slug" in issue.lower() for issue in issues)

    def test_empty_phase(self) -> None:
        row = {
            "contract_id": "build/01-bad",
            "phase": "",
            "slug": "01-bad",
            "body": "# Bad",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        assert any("phase" in issue.lower() for issue in issues)

    def test_empty_slug(self) -> None:
        row = {
            "contract_id": "build/01-bad",
            "phase": "build",
            "slug": "",
            "body": "# Bad",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        assert any("task_slug" in issue.lower() or "slug" in issue.lower() for issue in issues)

    def test_cannot_parse_row(self) -> None:
        """A row that causes contract_from_row to fail returns a parse error."""
        row = {}  # missing contract_id entirely
        issues = validate_contract_from_dict(row)
        assert len(issues) >= 1
        assert "parse" in issues[0].lower() or "cannot" in issues[0].lower()


# ---------------------------------------------------------------------------
# validate_contract_from_dict — phase ahead of active phase
# ---------------------------------------------------------------------------


class TestValidateContractFromDictPhaseAhead:
    """A row whose phase is ahead of the active phase should not fire a spurious
    phase mismatch.  The follow-up in docs/followups.md §'SDD tooling' specifies
    that validation should only fire when the contract's phase is *behind* the
    current phase, not ahead.

    The current validate_contract_from_dict does not check the active phase at
    all (it validates the row structure, not phase ordering), so this case
    should pass cleanly — no spurious phase mismatch error."""

    def test_build_contract_validated_at_design_no_spurious_error(self) -> None:
        """A build contract validated while in the design phase should not
        report a phase mismatch.  validate_contract_from_dict validates row
        structure only; it has no knowledge of the active phase."""
        row = {
            "contract_id": "build/01-auth",
            "phase": "build",
            "slug": "01-auth",
            "body": "# Auth Middleware",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        # Should not contain a phase mismatch error
        phase_issues = [i for i in issues if "phase" in i.lower() and "mismatch" in i.lower()]
        assert phase_issues == [], f"Spurious phase mismatch: {phase_issues}"

    def test_design_contract_validated_at_build_no_spurious_error(self) -> None:
        """A design contract validated while in the build phase should not
        report a phase mismatch from validate_contract_from_dict."""
        row = {
            "contract_id": "design/01-approach",
            "phase": "design",
            "slug": "01-approach",
            "body": "# Approach",
            "status": "active",
        }
        issues = validate_contract_from_dict(row)
        phase_issues = [i for i in issues if "phase" in i.lower() and "mismatch" in i.lower()]
        assert phase_issues == [], f"Spurious phase mismatch: {phase_issues}"


# ---------------------------------------------------------------------------
# contract_from_row — round-trip
# ---------------------------------------------------------------------------


class TestContractFromRow:
    """contract_from_row constructs a Contract from a store row dict."""

    def test_round_trip(self) -> None:
        row = {
            "contract_id": "build/01-auth",
            "phase": "build",
            "slug": "01-auth",
            "domain_tags": ["api-design"],
            "scope_touches": ["src/auth/"],
            "scope_avoids": ["src/billing/"],
            "success_criteria": ["tests pass"],
            "body": "# Auth Middleware",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        contract = contract_from_row(row)
        assert contract.contract_id == "build/01-auth"
        assert contract.phase == "build"
        assert contract.task_slug == "01-auth"
        assert contract.domain_tags == ["api-design"]
        assert contract.scope.touches == ["src/auth/"]
        assert contract.scope.avoids == ["src/billing/"]
        assert contract.success_criteria == [{"id": "tests pass", "text": "tests pass"}]
        assert contract.body == "# Auth Middleware"

    def test_missing_optional_fields(self) -> None:
        """Missing optional fields default to empty lists / empty strings."""
        row = {
            "contract_id": "build/01-minimal",
            "phase": "build",
            "slug": "01-minimal",
        }
        contract = contract_from_row(row)
        assert contract.domain_tags == []
        assert contract.scope.touches == []
        assert contract.scope.avoids == []
        assert contract.body == ""
        assert contract.route == "full"


# ---------------------------------------------------------------------------
# parse_contract_text — frontmatter validation
# ---------------------------------------------------------------------------


class TestParseContractText:
    """parse_contract_text raises ContractMalformed on bad input."""

    def test_valid_text(self) -> None:
        text = (
            "---\n"
            "phase: build\n"
            "task_slug: 01-auth\n"
            "route: full\n"
            "domain_tags: []\n"
            "scope:\n"
            "  touches: []\n"
            "  avoids: []\n"
            "success_criteria: []\n"
            "created_at: 2026-01-01T00:00:00Z\n"
            "---\n\n"
            "# Auth Middleware\n"
        )
        contract = parse_contract_text(text, contract_id="build/01-auth")
        assert contract.phase == "build"
        assert contract.task_slug == "01-auth"

    def test_missing_frontmatter(self) -> None:
        with pytest.raises(ContractMalformed, match="must begin"):
            parse_contract_text("# No frontmatter", contract_id="x")

    def test_missing_phase(self) -> None:
        text = "---\ntask_slug: x\n---\n\n# x\n"
        with pytest.raises(ContractMalformed, match="phase"):
            parse_contract_text(text, contract_id="x")

    def test_missing_task_slug(self) -> None:
        text = "---\nphase: build\n---\n\n# x\n"
        with pytest.raises(ContractMalformed, match="task_slug"):
            parse_contract_text(text, contract_id="x")

    def test_invalid_route(self) -> None:
        text = "---\nphase: build\ntask_slug: x\nroute: invalid\n---\n\n# x\n"
        with pytest.raises(ContractMalformed, match="route"):
            parse_contract_text(text, contract_id="x")


# ---------------------------------------------------------------------------
# validate_contract — glob pattern validation
# ---------------------------------------------------------------------------


class TestValidateContract:
    """validate_contract catches invalid glob patterns in scope."""

    def test_valid_globs(self) -> None:
        from agentalloy.contracts import Contract, ContractScope

        c = Contract(
            contract_id="x",
            phase="build",
            task_slug="x",
            domain_tags=[],
            scope=ContractScope(touches=["src/**/*.py"], avoids=["tests/**"]),
            success_criteria=[],
            related_contracts=[],
            created_at=None,
            body="",
        )
        issues = validate_contract(c, None)
        assert issues == []
