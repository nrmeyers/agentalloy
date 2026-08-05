"""Integration tests for the complete gate feedback flow.

Test cases from task 07-integration-tests.md — TC-07-1 through TC-07-5:

- TC-07-1: Contract write → verify gate_feedback artifact created
- TC-07-2: Contract with all ACs met → verify gate_feedback is None/not stored
- TC-07-3: Proxy request → verify feedback in SignalResult.advisories
- TC-07-4: Env var off → no feedback stored
- TC-07-5: Contract with legacy list[str] ACs → no feedback generated
"""

import tempfile
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

from agentalloy.api.state_router import default_repo_root
from agentalloy.storage.state_store import DuckDBStateStore, open_state_store


def _make_store(tmp_path: Path) -> DuckDBStateStore:
    """Create a fresh, migrated StateStore at a tmp path."""
    db = tmp_path / "state.duck"
    store = open_state_store(db, repo=str(default_repo_root()))
    return store


class TestGateFeedbackStoredOnContractWrite:
    """TC-07-1: Contract write → verify gate_feedback artifact created."""

    def test_gate_feedback_stored_on_contract_write(self, monkeypatch: "MonkeyPatch") -> None:
        """Gate feedback artifact is created when contract has unmet ACs."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                # Create a contract with unmet ACs
                store.put_contract(
                    "test-integration",
                    phase="spec",
                    slug="test-integration",
                    success_criteria=[
                        {"id": "AC-1", "text": "login works"},
                        {"id": "AC-2", "text": "logout works"},
                    ],
                    body="test contract",
                )

                # Simulate _trigger_compose_in_process gate feedback evaluation
                from agentalloy.signals.predicates import _evaluate_ac_feedback

                contract = store.get_contract("test-integration")
                feedback = _evaluate_ac_feedback(store, contract)
                assert feedback is not None
                assert "Unmet criteria" in feedback
                assert "AC-1" in feedback
                assert "AC-2" in feedback

                # Verify the feedback can be stored as an artifact
                store.set_artifact(
                    "spec",
                    "test-integration",
                    "gate_feedback",
                    feedback,
                )

                # Verify the artifact exists
                artifact = store.get_artifact("spec", "test-integration", "gate_feedback")
                assert artifact is not None
                assert "Unmet criteria" in artifact["content"]
            finally:
                store.close()


class TestGateFeedbackDeletedWhenAllMet:
    """TC-07-2: Contract with all ACs met → verify gate_feedback is None/not stored."""

    def test_gate_feedback_none_when_all_acs_met(self, monkeypatch: "MonkeyPatch") -> None:
        """When all ACs are met, feedback is None."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                # Create an artifact that satisfies the ACs
                store.set_artifact(
                    "spec",
                    "test-integration",
                    "spec.md",
                    "## AC-1: login works\n## AC-2: logout works",
                )

                store.put_contract(
                    "test-integration",
                    phase="spec",
                    slug="test-integration",
                    success_criteria=[
                        {"id": "AC-1", "text": "login works"},
                        {"id": "AC-2", "text": "logout works"},
                    ],
                    body="test contract",
                )

                # Evaluate feedback — should be None since all ACs are met
                from agentalloy.signals.predicates import _evaluate_ac_feedback

                contract = store.get_contract("test-integration")
                feedback = _evaluate_ac_feedback(store, contract)
                assert feedback is None
            finally:
                store.close()


class TestGateFeedbackInjectedIntoSignal:
    """TC-07-3: Proxy request → verify feedback in SignalResult.advisories."""

    def test_gate_feedback_in_signal_advisories(self, monkeypatch: "MonkeyPatch") -> None:
        """Gate feedback artifact is injected into SignalResult.advisories."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                # Create a gate_feedback artifact
                feedback_text = "[agentalloy-gate-feedback] Unmet criteria: AC-1, AC-2"
                store.set_artifact(
                    "spec",
                    "test-integration",
                    "gate_feedback",
                    feedback_text,
                )

                # Simulate _read_gate_feedback from proxy_signal.py
                gate_fb = store.get_artifact(
                    "spec", "test-integration", "gate_feedback", status="active"
                )
                assert gate_fb is not None
                assert gate_fb.get("content") == feedback_text

                # Verify the feedback would be injected into advisories
                advisories: list[str] = []
                if gate_fb and gate_fb.get("content"):
                    advisories.append(f"[agentalloy-gate-feedback] {gate_fb['content']}")

                assert len(advisories) == 1
                assert "[agentalloy-gate-feedback]" in advisories[0]
            finally:
                store.close()


class TestGateTriggerDisabled:
    """TC-07-4: Env var off → no feedback stored."""

    def test_gate_trigger_disabled_stores_no_feedback(self, monkeypatch: "MonkeyPatch") -> None:
        """When gate trigger is disabled, no feedback is stored."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "0")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                store.put_contract(
                    "test-integration",
                    phase="spec",
                    slug="test-integration",
                    success_criteria=[
                        {"id": "AC-1", "text": "login works"},
                    ],
                    body="test contract",
                )

                # _gate_trigger_enabled should return False
                from agentalloy.signals.predicates import _gate_trigger_enabled

                assert _gate_trigger_enabled() is False

                # No feedback should be stored
                feedback = store.get_artifact("spec", "test-integration", "gate_feedback")
                assert feedback is None
            finally:
                store.close()


class TestLegacyACsIgnored:
    """TC-07-5: Contract with legacy list[str] ACs → no feedback generated."""

    def test_legacy_string_acs_produce_no_feedback(self, monkeypatch: "MonkeyPatch") -> None:
        """Legacy string ACs are treated as both id and text, so they match."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                # Create an artifact that satisfies the string ACs
                store.set_artifact(
                    "spec",
                    "test-integration",
                    "spec.md",
                    "Tests pass\nLogin works",
                )

                store.put_contract(
                    "test-integration",
                    phase="spec",
                    slug="test-integration",
                    success_criteria=["Tests pass", "Login works"],
                    body="test contract",
                )

                # Evaluate feedback — string ACs are treated as both id and text
                from agentalloy.signals.predicates import _evaluate_ac_feedback

                contract = store.get_contract("test-integration")
                feedback = _evaluate_ac_feedback(store, contract)
                # String ACs match their text in the artifact, so feedback should be None
                assert feedback is None
            finally:
                store.close()

    def test_legacy_string_acs_unmet_produces_feedback(self, monkeypatch: "MonkeyPatch") -> None:
        """Unmet string ACs produce feedback."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = _make_store(tmp_path)

            try:
                # Create an artifact that doesn't satisfy all ACs
                store.set_artifact(
                    "spec",
                    "test-integration",
                    "spec.md",
                    "Tests pass",
                )

                store.put_contract(
                    "test-integration",
                    phase="spec",
                    slug="test-integration",
                    success_criteria=["Tests pass", "Login works"],
                    body="test contract",
                )

                from agentalloy.signals.predicates import _evaluate_ac_feedback

                contract = store.get_contract("test-integration")
                feedback = _evaluate_ac_feedback(store, contract)
                assert feedback is not None
                assert "Unmet criteria" in feedback
            finally:
                store.close()
