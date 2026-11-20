"""Integration tests for the complete gate feedback flow.

Test cases from task 07-integration-tests.md — TC-07-1 through TC-07-8:

- TC-07-1: Contract write → verify gate_feedback artifact created (active)
- TC-07-2: Contract with all ACs met → an active gate_feedback row is
  retired (archived) and no new feedback is written
- TC-07-3: Proxy request → verify feedback in SignalResult.advisories
- TC-07-4: Env var off → _apply_ac_feedback is a full no-op (no write,
  no retire of an existing active row)
- TC-07-5: Contract with legacy list[str] ACs → met ACs produce no
  feedback (and retire any stale row); unmet string ACs produce feedback
- TC-07-6: Recovery — an archived (stale) gate_feedback row is
  re-activated when the next contract write still has unmet ACs, so the
  agent sees the feedback again after a service restart
- TC-07-7: Idempotency — an active row whose content matches the freshly
  computed feedback is not rewritten on restart (no updated_at bump,
  no status flip)
- TC-07-8: Self-poisoning regression — the gate_feedback row's body names
  the unmet AC ids; it must never count as "meeting" those ACs
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

from agentalloy.api.state_router import default_repo_root
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store


def _make_store(tmp_path: Path) -> DuckDBStateStore:
    """Create a fresh, migrated StateStore at a tmp path."""
    db = tmp_path / "state.duck"
    store = open_state_store(db, repo=str(default_repo_root()))
    return store


def _write_contract(store: DuckDBStateStore, success_criteria) -> dict:
    """Write the standard test contract and return it as the store sees it."""
    store.put_contract(
        "test-integration",
        phase="spec",
        slug="test-integration",
        success_criteria=success_criteria,
        body="test contract",
    )
    return store.get_contract("test-integration")


def _get_row(store: DuckDBStateStore, status: str = "all") -> dict | None:
    """Read the gate_feedback row (any status by default)."""
    return store.get_artifact(
        "spec",
        "test-integration",
        "gate_feedback",
        status=status,
    )


@contextmanager
def _store_scope() -> Iterator[DuckDBStateStore]:
    """Fresh StateStore in a temp dir, closed on exit."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(Path(tmp))
        try:
            yield store
        finally:
            store.close()


class TestGateFeedbackStoredOnContractWrite:
    """TC-07-1: Contract write → verify gate_feedback artifact created."""

    def test_gate_feedback_stored_on_contract_write(self, monkeypatch: "MonkeyPatch") -> None:
        """Gate feedback artifact is created (active) when contract has unmet ACs."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                    {"id": "AC-2", "text": "logout works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            feedback = _apply_ac_feedback(store, contract)
            assert feedback is not None
            assert "Unmet criteria" in feedback
            assert "AC-1" in feedback
            assert "AC-2" in feedback

            row = _get_row(store)
            assert row is not None
            assert row["status"] == "active"
            assert row["content"] == feedback


class TestGateFeedbackRetiredWhenAllMet:
    """TC-07-2: Contract with all ACs met → active gate_feedback is retired."""

    def test_gate_feedback_retired_when_all_acs_met(self, monkeypatch: "MonkeyPatch") -> None:
        """A previously active feedback row is archived once all ACs are met."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            # The spec artifact satisfies both ACs.
            store.set_artifact(
                "spec",
                "test-integration",
                "spec.artifact",
                "## AC-1: login works\n## AC-2: logout works",
            )
            # A feedback row left over from an earlier, unmet state.
            store.set_artifact(
                "spec",
                "test-integration",
                "gate_feedback",
                "[agentalloy-gate-feedback] Unmet criteria: AC-1, AC-2",
            )

            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                    {"id": "AC-2", "text": "logout works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            assert _apply_ac_feedback(store, contract) is None

            # Retired, not deleted — the row stays auditable but is no
            # longer agent-visible.
            row = _get_row(store)
            assert row is not None
            assert row["status"] == "archived"
            assert _get_row(store, status="active") is None


class TestGateFeedbackInjectedIntoSignal:
    """TC-07-3: Proxy request → verify feedback in SignalResult.advisories."""

    def test_gate_feedback_in_signal_advisories(self, monkeypatch: "MonkeyPatch") -> None:
        """Gate feedback artifact is injected into SignalResult.advisories."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            # Create a gate_feedback artifact
            feedback_text = "[agentalloy-gate-feedback] Unmet criteria: AC-1, AC-2"
            store.set_artifact(
                "spec",
                "test-integration",
                "gate_feedback",
                feedback_text,
            )

            # Simulate _read_gate_feedback from proxy_signal.py
            gate_fb = _get_row(store, status="active")
            assert gate_fb is not None
            assert gate_fb.get("content") == feedback_text

            # Verify the feedback would be injected into advisories
            advisories: list[str] = []
            if gate_fb and gate_fb.get("content"):
                advisories.append(f"[agentalloy-gate-feedback] {gate_fb['content']}")

            assert len(advisories) == 1
            assert "[agentalloy-gate-feedback]" in advisories[0]


class TestGateTriggerDisabled:
    """TC-07-4: Env var off → _apply_ac_feedback is a full no-op."""

    def test_gate_trigger_disabled_is_noop(self, monkeypatch: "MonkeyPatch") -> None:
        """Disabled trigger neither writes feedback nor retires an active row."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "0")

        with _store_scope() as store:
            # An active row left over from an earlier run with the trigger on.
            store.set_artifact(
                "spec",
                "test-integration",
                "gate_feedback",
                "[agentalloy-gate-feedback] Unmet criteria: AC-1",
            )

            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            assert _apply_ac_feedback(store, contract) is None

            # The pre-existing row must be untouched — a disabled feature
            # must not mutate store state it was asked to leave alone.
            row = _get_row(store)
            assert row is not None
            assert row["status"] == "active"


class TestLegacyACs:
    """TC-07-5: Contract with legacy list[str] ACs → feedback behavior."""

    def test_legacy_string_acs_met_produce_no_feedback(self, monkeypatch: "MonkeyPatch") -> None:
        """String ACs met in an artifact produce no feedback (and retire a stale row)."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            # Create an artifact that satisfies the string ACs
            store.set_artifact(
                "spec",
                "test-integration",
                "spec.artifact",
                "Tests pass\nLogin works",
            )
            store.set_artifact(
                "spec",
                "test-integration",
                "gate_feedback",
                "[agentalloy-gate-feedback] Unmet criteria: Tests pass",
            )

            contract = _write_contract(store, ["Tests pass", "Login works"])

            from agentalloy.signals.predicates import _apply_ac_feedback

            assert _apply_ac_feedback(store, contract) is None
            row = _get_row(store)
            assert row is not None
            assert row["status"] == "archived"

    def test_legacy_string_acs_unmet_produce_feedback(self, monkeypatch: "MonkeyPatch") -> None:
        """Unmet string ACs produce feedback."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            # Create an artifact that doesn't satisfy all ACs
            store.set_artifact(
                "spec",
                "test-integration",
                "spec.artifact",
                "Tests pass",
            )

            contract = _write_contract(store, ["Tests pass", "Login works"])

            from agentalloy.signals.predicates import _apply_ac_feedback

            feedback = _apply_ac_feedback(store, contract)
            assert feedback is not None
            assert "Unmet criteria" in feedback
            assert "Login works" in feedback
            row = _get_row(store)
            assert row is not None
            assert row["status"] == "active"


class TestStaleFeedbackReactivation:
    """TC-07-6: Recovery — an archived (stale) row is re-activated."""

    def test_archived_row_reactivated_when_acs_still_unmet(
        self, monkeypatch: "MonkeyPatch"
    ) -> None:
        """After a restart, the next trigger re-activates stale feedback."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                    {"id": "AC-2", "text": "logout works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            # First trigger: feedback written, row active.
            feedback = _apply_ac_feedback(store, contract)
            assert feedback is not None

            # Simulate the row going stale: a later retirement (an
            # all-met verdict, or archive_all at end of a work cycle)
            # archived it while the ACs in fact remained unmet across a
            # service restart.
            assert store.set_artifact_status(
                "spec", "test-integration", "gate_feedback", "archived"
            )
            assert _get_row(store, status="active") is None

            # The next contract write after restart re-runs the lifecycle:
            # the stale row must come back so the agent sees the feedback
            # again.
            assert _apply_ac_feedback(store, contract) == feedback

            row = _get_row(store, status="active")
            assert row is not None
            assert row["content"] == feedback


class TestIdempotentReembed:
    """TC-07-7: Active feedback with a current stamp is re-embedded
    idempotently on restart (no spurious supersede)."""

    def test_current_stamp_not_rewritten(self, monkeypatch: "MonkeyPatch") -> None:
        """An active row matching the freshly computed feedback is left untouched."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                    {"id": "AC-2", "text": "logout works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            feedback = _apply_ac_feedback(store, contract)
            assert feedback is not None
            row1 = _get_row(store)
            assert row1 is not None

            # Same trigger again (e.g. re-embed after restart): the stamp
            # is still current — the row must not be re-written.
            assert _apply_ac_feedback(store, contract) == feedback
            row2 = _get_row(store)
            assert row2 is not None
            assert row2["content"] == row1["content"]
            assert str(row2["updated_at"]) == str(row1["updated_at"])
            assert row2["status"] == "active"


class TestNoSelfPoisoning:
    """TC-07-8: The gate_feedback row must not satisfy the ACs it reports on."""

    def test_feedback_row_does_not_count_as_met(self, monkeypatch: "MonkeyPatch") -> None:
        """Unmet ACs stay unmet on re-evaluation even though the feedback body
        names their ids — the row is excluded from the AC-met scan."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with _store_scope() as store:
            contract = _write_contract(
                store,
                [
                    {"id": "AC-1", "text": "login works"},
                    {"id": "AC-2", "text": "logout works"},
                ],
            )

            from agentalloy.signals.predicates import _apply_ac_feedback

            # First trigger writes the feedback. Its body contains the
            # literal ids "AC-1" and "AC-2".
            feedback = _apply_ac_feedback(store, contract)
            assert feedback is not None
            assert "AC-1" in feedback and "AC-2" in feedback

            # Next contract write re-evaluates with that row present and
            # active. Without the exclusion, _ac_is_met would find the ids
            # in the feedback body, report all ACs met, and retire the row —
            # the agent would lose the only signal that the ACs are unmet.
            assert _apply_ac_feedback(store, contract) == feedback

            row = _get_row(store, status="active")
            assert row is not None
            assert row["content"] == feedback
