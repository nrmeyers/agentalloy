"""Grading tests for task 08: postmortem.

Covers ``grade_task_8`` from ``eval/tasks.py`` with passing and failing
samples.  The task asks the agent to write an incident postmortem for a
30-minute auth-service outage caused by database connection-pool exhaustion.
"""

from __future__ import annotations

from eval.tasks import grade_task_8

# ---------------------------------------------------------------------------
# Sample outputs
# ---------------------------------------------------------------------------

_GOOD_POSTMORTEM = """
## Timeline

- 14:00 – Deploy v2.3.1 to production
- 14:05 – Auth service latency spikes to 3 s
- 14:10 – Connection pool exhausted; requests start failing
- 14:30 – Incident resolved; pool size increased

## Root Cause

The new database driver in v2.3.1 leaked connections under load. The
connection pool reached its maximum size (100) and new requests could not
obtain a connection, causing the auth service to fail.

## Contributing Factors

- No connection-pool monitoring alert existed
- The driver change was not flagged in code review

## Action Items

1. Add connection-pool metrics to Grafana dashboard
2. Require pool-size review in change checklist
3. Write a postmortem runbook for pool exhaustion
""".strip()

_GOOD_POSTMORTEM_FOLLOWUP = """
# Timeline

14:00 Deploy. 14:10 Pool exhaustion. 14:30 Resolved.

## Root Cause

Connection pool exhausted due to a leak in the new driver.

## Follow-up Actions

- Add monitoring
- Review change process
""".strip()

_BAD_TOO_LONG = (
    """
"""
    + "\n".join(f"Line {i}" for i in range(700))
    + """
## Timeline

Some timeline.
""".strip()
)

_BAD_MISSING_SECTIONS = """
The outage was caused by a connection pool issue. We need to fix it.

- Add monitoring
- Review code
""".strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGradeTask8:
    """Verify grade_task_8 produces correct results."""

    def test_good_postmortem_passes_all_criteria(self) -> None:
        grades = grade_task_8(_GOOD_POSTMORTEM)
        assert grades["has_timeline_section"] is True
        assert grades["has_root_cause_section"] is True
        assert grades["has_action_items_section"] is True
        assert grades["mentions_connection_pool"] is True
        assert grades["under_600_words"] is True
        # All five should be True.
        assert all(grades.values()), f"expected all True, got {grades}"

    def test_good_postmortem_with_followup_passes_action_items(self) -> None:
        """'Follow-up' counts as an action-items section."""
        grades = grade_task_8(_GOOD_POSTMORTEM_FOLLOWUP)
        assert grades["has_timeline_section"] is True
        assert grades["has_root_cause_section"] is True
        assert grades["has_action_items_section"] is True
        assert grades["mentions_connection_pool"] is True

    def test_good_postmortem_with_hash_timeline(self) -> None:
        """'# timeline' also counts as a timeline section."""
        grades = grade_task_8("# Timeline\n\nPool issue.\n\nAction items:\n- fix it")
        assert grades["has_timeline_section"] is True

    def test_good_postmortem_with_bold_timeline(self) -> None:
        """'**timeline**' also counts as a timeline section."""
        grades = grade_task_8("**timeline**:\n\nPool issue.\n\nAction items:\n- fix it")
        assert grades["has_timeline_section"] is True

    def test_good_postmortem_with_bold_cause(self) -> None:
        """'**cause**' also counts as a root-cause section."""
        grades = grade_task_8(
            "## Timeline\n\n**cause**: pool exhausted.\n\nAction items:\n- fix it"
        )
        assert grades["has_root_cause_section"] is True

    def test_bad_too_long_fails_word_count(self) -> None:
        grades = grade_task_8(_BAD_TOO_LONG)
        assert grades["under_600_words"] is False

    def test_bad_missing_sections_fails_sections(self) -> None:
        grades = grade_task_8(_BAD_MISSING_SECTIONS)
        assert grades["has_timeline_section"] is False
        assert grades["has_root_cause_section"] is False
        assert grades["has_action_items_section"] is False
        assert grades["mentions_connection_pool"] is True  # "connection pool" is present

    def test_bad_missing_sections_fails_connection_pool(self) -> None:
        grades = grade_task_8("Timeline:\n\nRoot cause: unknown.\n\nAction items:\n- fix")
        assert grades["has_timeline_section"] is False
        assert grades["has_root_cause_section"] is False
        assert grades["has_action_items_section"] is True  # "action item" matches
        assert grades["mentions_connection_pool"] is False

    def test_empty_output_fails_everything(self) -> None:
        grades = grade_task_8("")
        assert all(v is False for v in grades.values()), (
            f"expected all False for empty output, got {grades}"
        )

    def test_pool_exhaust_synonym_works(self) -> None:
        """'pool exhaust' and 'pool size' are also accepted."""
        for phrase in ["pool exhaust", "pool size"]:
            grades = grade_task_8(f"## Timeline\n\n{phrase} reached max.")
            assert grades["mentions_connection_pool"] is True, phrase
