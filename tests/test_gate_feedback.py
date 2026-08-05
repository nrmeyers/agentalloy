"""Tests for gate feedback functions in agentalloy.signals.predicates.

Test cases from task 03-gate-feedback.md — TC-03-1 through TC-03-4:

- TC-03-1: _gate_trigger_enabled() returns True by default
- TC-03-2: _gate_trigger_enabled() returns False when env var is "0"
- TC-03-3: _evaluate_ac_feedback returns None when all ACs are met
- TC-03-4: _evaluate_ac_feedback returns feedback string when ACs are unmet
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from _pytest.monkeypatch import MonkeyPatch

from agentalloy.signals.predicates import (
    _ac_is_met,
    _evaluate_ac_feedback,
    _gate_trigger_enabled,
)


class TestGateTriggerEnabled:
    """_gate_trigger_enabled reads AGENTALLOY_GATE_TRIGGER_ENABLED env var."""

    def test_default_returns_true(self, monkeypatch: "MonkeyPatch") -> None:
        """TC-03-1: _gate_trigger_enabled() returns True by default."""
        monkeypatch.delenv("AGENTALLOY_GATE_TRIGGER_ENABLED", raising=False)
        assert _gate_trigger_enabled() is True

    def test_env_var_zero_returns_false(self, monkeypatch: "MonkeyPatch") -> None:
        """TC-03-2: _gate_trigger_enabled() returns False when env var is '0'."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "0")
        assert _gate_trigger_enabled() is False

    def test_env_var_false_returns_false(self, monkeypatch: "MonkeyPatch") -> None:
        """_gate_trigger_enabled() returns False when env var is 'false'."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "false")
        assert _gate_trigger_enabled() is False

    def test_env_var_1_returns_true(self, monkeypatch: "MonkeyPatch") -> None:
        """_gate_trigger_enabled() returns True when env var is '1'."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "1")
        assert _gate_trigger_enabled() is True


class TestAcIsMet:
    """_ac_is_met checks if an AC ID or text appears in artifacts."""

    def test_ac_text_found_in_artifact(self) -> None:
        """AC text found in artifact content returns True."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            # Create a mock artifact
            artifact = {
                "name": "spec.md",
                "content": "## AC-1: login works\n\nThis artifact covers AC-1.",
            }
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                result = _ac_is_met(None, "spec", "test-slug", "AC-1", "login works")
                assert result is True

    def test_ac_text_not_found(self) -> None:
        """AC text not found in any artifact returns False."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            artifact = {"name": "spec.md", "content": "## Other AC"}
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                result = _ac_is_met(None, "spec", "test-slug", "AC-1", "login works")
                assert result is False

    def test_ac_id_found_in_content(self) -> None:
        """AC ID found as substring returns True."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            artifact = {"name": "spec.md", "content": "We verified AC-1 passes."}
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                result = _ac_is_met(None, "spec", "test-slug", "AC-1", "something else")
                assert result is True


class TestEvaluateAcFeedback:
    """_evaluate_ac_feedback checks AC completeness and returns feedback."""

    def test_all_acs_met_returns_none(self) -> None:
        """TC-03-3: _evaluate_ac_feedback returns None when all ACs are met."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            artifact = {"name": "spec.md", "content": "## AC-1: login works\n## AC-2: logout works"}
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                contract = {
                    "phase": "spec",
                    "slug": "test-slug",
                    "success_criteria": [
                        {"id": "AC-1", "text": "login works"},
                        {"id": "AC-2", "text": "logout works"},
                    ],
                }
                result = _evaluate_ac_feedback(None, contract)
                assert result is None

    def test_unmet_ac_returns_feedback(self) -> None:
        """TC-03-4: _evaluate_ac_feedback returns feedback string when ACs are unmet."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            artifact = {"name": "spec.md", "content": "## AC-1: login works"}
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                contract = {
                    "phase": "spec",
                    "slug": "test-slug",
                    "success_criteria": [
                        {"id": "AC-1", "text": "login works"},
                        {"id": "AC-2", "text": "logout works"},
                    ],
                }
                result = _evaluate_ac_feedback(None, contract)
                assert result is not None
                assert "Unmet criteria" in result
                assert "AC-2" in result

    def test_no_success_criteria_returns_none(self) -> None:
        """Empty success_criteria returns None."""
        contract = {"phase": "spec", "slug": "test-slug", "success_criteria": []}
        result = _evaluate_ac_feedback(None, contract)
        assert result is None

    def test_missing_phase_returns_none(self) -> None:
        """Missing phase returns None."""
        contract = {"slug": "test-slug", "success_criteria": [{"id": "AC-1", "text": "login"}]}
        result = _evaluate_ac_feedback(None, contract)
        assert result is None

    def test_string_criteria_treated_as_id_and_text(self) -> None:
        """String criteria are treated as both id and text."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)  # noqa: F841
            artifact = {"name": "spec.md", "content": "Tests pass"}
            with patch(
                "agentalloy.signals.predicates._list_store_artifacts",
                return_value=[artifact],
            ):
                contract = {
                    "phase": "spec",
                    "slug": "test-slug",
                    "success_criteria": ["Tests pass"],
                }
                result = _evaluate_ac_feedback(None, contract)
                assert result is None

    def test_feedback_disabled_by_env_var(self, monkeypatch: "MonkeyPatch") -> None:
        """When gate trigger is disabled, returns None even with unmet ACs."""
        monkeypatch.setenv("AGENTALLOY_GATE_TRIGGER_ENABLED", "0")
        contract = {
            "phase": "spec",
            "slug": "test-slug",
            "success_criteria": [{"id": "AC-1", "text": "login works"}],
        }
        result = _evaluate_ac_feedback(None, contract)
        assert result is None
