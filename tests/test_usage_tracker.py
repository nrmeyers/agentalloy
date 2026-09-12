"""Usage tracker tests — verify cumulative token tracking."""

import tempfile
from pathlib import Path

from agentalloy.usage_tracker import UsageTracker


def test_record_and_totals() -> None:
    """Record requests and verify cumulative totals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "usage.duck")
        tracker = UsageTracker(db_path)

        tracker.record(prompt_tokens=100, completion_tokens=50, injected_tokens=10)
        tracker.record(prompt_tokens=200, completion_tokens=75, injected_tokens=15)

        totals = tracker.get_totals()
        assert totals["prompt_tokens"] == 300
        assert totals["completion_tokens"] == 125
        assert totals["injected_tokens"] == 25
        assert totals["total_requests"] == 2
        assert totals["total_tokens"] == 425

        tracker.close()


def test_empty_totals() -> None:
    """Empty tracker returns zero totals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "usage.duck")
        tracker = UsageTracker(db_path)

        totals = tracker.get_totals()
        assert totals["prompt_tokens"] == 0
        assert totals["total_requests"] == 0

        tracker.close()


def test_history() -> None:
    """History returns recent records in reverse order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "usage.duck")
        tracker = UsageTracker(db_path)

        tracker.record(prompt_tokens=100, completion_tokens=50, injected_tokens=10)
        tracker.record(prompt_tokens=200, completion_tokens=75, injected_tokens=15)

        history = tracker.get_history()
        assert len(history) == 2
        assert history[0].prompt_tokens == 200  # Most recent first
        assert history[1].prompt_tokens == 100

        tracker.close()


def test_history_limit() -> None:
    """History respects the limit parameter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "usage.duck")
        tracker = UsageTracker(db_path)

        for i in range(10):
            tracker.record(prompt_tokens=i * 10, completion_tokens=5, injected_tokens=1)

        history = tracker.get_history(limit=3)
        assert len(history) == 3

        tracker.close()
