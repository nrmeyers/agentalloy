"""Grading tests for task 10: database performance runbook.

Covers ``grade_task_10`` from ``eval/tasks.py`` with passing and failing
samples.  The task asks the agent to write a runbook for handling production
database performance regressions, including triage steps, common root causes,
fix strategies, rollback criteria, and a communication checklist.
"""

from __future__ import annotations

from eval.tasks import grade_task_10

# ---------------------------------------------------------------------------
# Sample outputs
# ---------------------------------------------------------------------------

_GOOD_RUNBOOK = """
# Database Performance Regression Runbook

## Triage Steps

1. Check monitoring dashboards for CPU, memory, disk I/O, and connection count.
2. Identify the scope: is this affecting all queries or specific endpoints?
3. Check recent deployments that may have introduced performance regressions.
4. Look for long-running queries in the processlist.

## Common Root Causes

- Missing or stale indexes after schema changes
- Query plan regression due to statistics being outdated
- Connection pool exhaustion from connection leaks
- Lock contention from long transactions
- Disk I/O saturation from backup jobs or bulk operations

## Fix Strategies

- Add missing indexes identified by slow query analysis
- Run `ANALYZE` to update statistics if query plans have regressed
- Kill long-running queries that are causing lock contention
- Scale read replicas to offload read traffic
- Increase connection pool size if under-provisioned

## Rollback Criteria

Roll back the recent deployment if:
- Query latency exceeds 5x the baseline for more than 5 minutes
- Error rate increases above 1%
- Connection pool utilization exceeds 90%
- Database CPU stays above 80% for more than 10 minutes

## Communication Checklist

- Notify the on-call engineering lead within 5 minutes
- Update the status page if user-facing impact is detected
- Send an initial incident update within 15 minutes
- Provide hourly updates until the issue is resolved
- Notify stakeholders when the issue is resolved
""".strip()

_GOOD_RUNBOOK_ALT = """
# DB Perf Runbook

## Triage Step

1. Check dashboards
2. Identify scope
3. Check deployments

## Root Cause

Missing indexes, stale statistics, connection leaks, lock contention.

## Fix

Add indexes, run ANALYZE, kill long queries, scale replicas.

## When to Roll Back

Latency > 5x baseline for 5+ minutes, error rate > 1%.

## Communication

Notify on-call, update status page, send initial update in 15 minutes.
""".strip()

_BAD_TOO_SHORT = """
Check monitoring. Fix indexes. Tell someone.
""".strip()

_BAD_MISSING_COMMUNICATION = """
# Database Performance Runbook

## Triage Steps

1. Check monitoring dashboards.
2. Identify the scope.

## Common Root Causes

- Missing indexes
- Connection pool exhaustion

## Fix Strategies

- Add missing indexes
- Kill long-running queries

## Rollback Criteria

- Latency exceeds 5x baseline
- Error rate above 1%
""".strip()

_BAD_MISSING_ROLLBACK = """
# Database Performance Runbook

## Triage Steps

1. Check monitoring dashboards.
2. Identify the scope.

## Common Root Causes

- Missing indexes
- Connection pool exhaustion

## Fix Strategies

- Add missing indexes
- Kill long-running queries

## Communication Checklist

- Notify on-call lead
- Update status page
""".strip()

_BAD_MISSING_FIX = """
# Database Performance Runbook

## Triage Steps

1. Check monitoring dashboards.
2. Identify the scope.

## Common Root Causes

- Missing indexes
- Connection pool exhaustion

## Rollback Criteria

- Latency exceeds 5x baseline
- Error rate above 1%

## Communication Checklist

- Notify on-call lead
- Update status page
""".strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGradeTask10:
    """Verify grade_task_10 produces correct results."""

    def test_good_runbook_passes_all_criteria(self) -> None:
        grades = grade_task_10(_GOOD_RUNBOOK)
        assert grades["has_triage_section"] is True
        assert grades["has_root_causes_section"] is True
        assert grades["has_fix_strategies_section"] is True
        assert grades["has_rollback_section"] is True
        assert grades["has_communication_section"] is True
        assert all(grades.values()), f"expected all True, got {grades}"

    def test_good_runbook_alt_passes_all_criteria(self) -> None:
        grades = grade_task_10(_GOOD_RUNBOOK_ALT)
        assert grades["has_triage_section"] is True
        assert grades["has_root_causes_section"] is True
        assert grades["has_fix_strategies_section"] is True
        assert grades["has_rollback_section"] is True
        assert grades["has_communication_section"] is True

    def test_good_runbook_triage_step(self) -> None:
        """'triage step' is accepted as a triage section."""
        grades = grade_task_10(
            "## Triage Step\n\nCheck dashboards.\n\nRoot cause: missing index.\n\nFix: add index.\n\nRollback if latency high.\n\nNotify on-call."
        )
        assert grades["has_triage_section"] is True

    def test_good_runbook_bold_triage(self) -> None:
        """'**triage**' is accepted as a triage section."""
        grades = grade_task_10(
            "**triage**:\n\nCheck dashboards.\n\nRoot cause: missing index.\n\nFix: add index.\n\nRollback if latency high.\n\nNotify on-call."
        )
        assert grades["has_triage_section"] is True

    def test_good_runbook_when_to_roll_back(self) -> None:
        """'when to roll back' is accepted as a rollback section."""
        grades = grade_task_10(
            "## Triage\n\n## Root Cause\n\n## Fix\n\nWhen to roll back: latency > 5x.\n\n## Communication\n\nNotify on-call."
        )
        assert grades["has_rollback_section"] is True

    def test_good_runbook_communication_section(self) -> None:
        """'## communication' is accepted as a communication section."""
        grades = grade_task_10(
            "## Triage\n\n## Root Cause\n\n## Fix\n\n## Rollback\n\n## communication\n\nNotify on-call."
        )
        assert grades["has_communication_section"] is True

    def test_good_runbook_stakeholder(self) -> None:
        """'stakeholder' is accepted as a communication section."""
        grades = grade_task_10(
            "## Triage\n\n## Root Cause\n\n## Fix\n\n## Rollback\n\nNotify stakeholders."
        )
        assert grades["has_communication_section"] is True

    def test_good_runbook_comms_checklist(self) -> None:
        """'comms checklist' is accepted as a communication section."""
        grades = grade_task_10(
            "## Triage\n\n## Root Cause\n\n## Fix\n\n## Rollback\n\nComms checklist: notify on-call."
        )
        assert grades["has_communication_section"] is True

    def test_bad_too_short_fails_all_sections(self) -> None:
        grades = grade_task_10(_BAD_TOO_SHORT)
        assert grades["has_triage_section"] is False
        assert grades["has_root_causes_section"] is False
        assert grades["has_fix_strategies_section"] is False
        assert grades["has_rollback_section"] is False
        assert grades["has_communication_section"] is False

    def test_bad_missing_communication_fails_one_criteria(self) -> None:
        grades = grade_task_10(_BAD_MISSING_COMMUNICATION)
        assert grades["has_triage_section"] is True
        assert grades["has_root_causes_section"] is True
        assert grades["has_fix_strategies_section"] is True
        assert grades["has_rollback_section"] is True
        assert grades["has_communication_section"] is False

    def test_bad_missing_rollback_fails_one_criteria(self) -> None:
        grades = grade_task_10(_BAD_MISSING_ROLLBACK)
        assert grades["has_triage_section"] is True
        assert grades["has_root_causes_section"] is True
        assert grades["has_fix_strategies_section"] is True
        assert grades["has_rollback_section"] is False
        assert grades["has_communication_section"] is True

    def test_bad_missing_fix_fails_one_criteria(self) -> None:
        grades = grade_task_10(_BAD_MISSING_FIX)
        assert grades["has_triage_section"] is True
        assert grades["has_root_causes_section"] is True
        assert grades["has_fix_strategies_section"] is False
        assert grades["has_rollback_section"] is True
        assert grades["has_communication_section"] is True

    def test_empty_output_fails_everything(self) -> None:
        grades = grade_task_10("")
        assert all(v is False for v in grades.values()), (
            f"expected all False for empty output, got {grades}"
        )

    def test_fix_section_heading_works(self) -> None:
        """'## fix' is accepted as a fix strategies section."""
        grades = grade_task_10(
            "## Triage\n\n## Root Cause\n\n## Fix\n\nAdd indexes.\n\n## Rollback\n\n## Communication\n\nNotify."
        )
        assert grades["has_fix_strategies_section"] is True

    def test_root_cause_section_heading_works(self) -> None:
        """'## root cause' is accepted as a root causes section."""
        grades = grade_task_10(
            "## Triage\n\n## root cause\n\nMissing index.\n\n## Fix\n\nAdd index.\n\n## Rollback\n\n## Communication\n\nNotify."
        )
        assert grades["has_root_causes_section"] is True
