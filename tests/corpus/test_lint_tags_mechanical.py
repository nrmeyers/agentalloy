"""Tag-linter rule semantics — tags are exact-match filter keys, not ranking hints."""

from __future__ import annotations

from agentalloy.lint_tags_mechanical import lint_tags_mechanical


def _rules(verdicts: list[object]) -> list[str]:
    return [v.rule for v in verdicts]  # type: ignore[attr-defined]


def test_title_overlapping_tag_is_not_flagged() -> None:
    """The natural filter key for a skill overlaps its title by construction.

    Regression for retired R2: 'analytics' on "Analytics — Cohorts and
    Sessions" is required for domain_tags=["analytics"] filtering and must
    never be reported as redundant.
    """
    verdicts = lint_tags_mechanical(
        tags=["analytics", "cohorts", "sessions"],
        skill_class="domain",
        canonical_name="Analytics — Cohorts and Sessions",
        tier=None,
    )
    assert "R2" not in _rules(verdicts)
    assert verdicts == []


def test_morphological_duplicate_tags_flagged() -> None:
    verdicts = lint_tags_mechanical(
        tags=["webhook", "webhooks"],
        skill_class="domain",
        canonical_name="Webhook Receivers",
        tier=None,
    )
    assert _rules(verdicts) == ["R3-stem"]
    assert "webhook" in verdicts[0].detail


def test_distinct_concepts_sharing_one_stem_not_flagged() -> None:
    """Any-intersection R3 flagged these as synonyms; equality must not."""
    verdicts = lint_tags_mechanical(
        tags=["clean-code", "code-simplification"],
        skill_class="domain",
        canonical_name="Code Simplification",
        tier=None,
    )
    assert "R3-stem" not in _rules(verdicts)


def test_subset_stem_tags_not_flagged() -> None:
    """'review' vs 'pr-review' are different filter keys at different granularity."""
    verdicts = lint_tags_mechanical(
        tags=["review", "pr-review"],
        skill_class="domain",
        canonical_name="Reviewing Pull Requests",
        tier=None,
    )
    assert "R3-stem" not in _rules(verdicts)


def test_system_skill_with_tags_flagged() -> None:
    verdicts = lint_tags_mechanical(
        tags=["routing"],
        skill_class="system",
        canonical_name="Intake Router",
        tier=None,
    )
    assert _rules(verdicts) == ["system-empty"]


def test_workflow_missing_position_marker_flagged() -> None:
    verdicts = lint_tags_mechanical(
        tags=["something-else"],
        skill_class="workflow",
        canonical_name="Some Workflow",
        tier=None,
    )
    assert "W1" in _rules(verdicts)
