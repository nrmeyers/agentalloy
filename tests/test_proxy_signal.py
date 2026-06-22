"""Signal layer integration tests for proxy requests.

Tests evaluate_signal() and SignalResult -- covers the full signal flow:
no phase, no skill, pre-filter miss, pre-filter hit, gate evaluation,
and phase transitions.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest

from agentalloy.api import proxy_signal
from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.signals.prefilter import PreFilterMatch


def _req(prompt: str) -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=prompt)],
    )


def _set_phase(tmp_path: Path, phase: str) -> None:
    phase_dir = tmp_path / ".agentalloy"
    phase_dir.mkdir(exist_ok=True)
    (phase_dir / "phase").write_text(f"phase: {phase}\n")


def _skill(
    keywords: list[str], phases: list[str] | None = None, domain_tags: list[str] | None = None
) -> dict[str, Any]:
    return {
        "signal_keywords": keywords,
        "exit_gates": {},
        "applies_to_phases": phases or ["build"],
        "domain_tags": domain_tags,
    }


def _no_transition(qwen: int = 0) -> MagicMock:
    d = MagicMock()
    d.should_transition = False
    d.gates_met = []
    d.gates_unmet = []
    d.qwen_calls = qwen
    return d


class TestEvaluateSignal:
    def test_no_phase_file_returns_passthrough(self, tmp_path: Path) -> None:
        result = asyncio.run(evaluate_signal(_req("hello"), tmp_path))
        assert result.should_compose is False
        assert result.phase is None

    def test_phase_exists_no_skill_returns_passthrough(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value=None,
        ):
            result = asyncio.run(evaluate_signal(_req("hello"), tmp_path))
        assert result.should_compose is False
        assert result.phase == "build"
        assert result.task == "hello"

    def test_phase_exists_pre_filter_no_match(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["deploy", "release"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("just writing code"), tmp_path))
        assert result.should_compose is False
        assert result.phase == "build"

    def test_phase_exists_pre_filter_match_composes(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='test'")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["test", "deploy"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=_no_transition(),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("run the test suite"), tmp_path))
        assert result.should_compose is True
        assert result.phase == "build"
        assert result.task == "run the test suite"
        assert result.pre_filter_matched == "keyword='test'"

    def test_intake_phase_bypasses_prefilter(self, tmp_path: Path) -> None:
        """Intake composes on the first prompt regardless of signal keywords.

        The intake entry workflow must engage before any keyword exists, so the
        pre-filter is bypassed entirely when phase == intake.
        """
        _set_phase(tmp_path, "intake")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                # Empty keywords + a non-matching prompt: check_prefilter would
                # return None, yet intake must still compose.
                return_value=_skill([], phases=["intake"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ) as mock_prefilter,
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=_no_transition(),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("literally anything at all"), tmp_path))
        assert result.should_compose is True
        assert result.phase == "intake"
        mock_prefilter.assert_not_called()  # bypassed, not consulted
        assert result.pre_filter_matched == "intake phase composes unconditionally"

    def test_phase_transition_on_gates_met(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='deploy'")
        decision = MagicMock()
        decision.should_transition = True
        decision.to_phase = "qa"
        decision.gates_met = [
            MagicMock(gate_name="test_passed"),
            MagicMock(gate_name="lint_clean"),
        ]
        decision.gates_unmet = []
        decision.qwen_calls = 1
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["deploy"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=decision,
            ),
            mock.patch("agentalloy.api.proxy_signal._write_phase_atomic") as mock_write,
        ):
            result = asyncio.run(evaluate_signal(_req("deploy now"), tmp_path))
        assert result.should_compose is True
        mock_write.assert_called_once_with(tmp_path, "qa")
        assert result.gates_met == ["test_passed", "lint_clean"]
        assert result.qwen_calls == 1

    def test_phase_write_error_is_logged_not_raised(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='deploy'")
        decision = MagicMock()
        decision.should_transition = True
        decision.to_phase = "qa"
        decision.gates_met = []
        decision.gates_unmet = []
        decision.qwen_calls = 0
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["deploy"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=decision,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal._write_phase_atomic",
                side_effect=OSError("permission denied"),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("deploy now"), tmp_path))
        assert result.should_compose is True

    def test_manual_force_check_triggers(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="manual", detail="AGENTALLOY_FORCE_CHECK=1")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill([]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=_no_transition(),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("anything"), tmp_path))
        assert result.should_compose is True
        assert result.pre_filter_matched == "AGENTALLOY_FORCE_CHECK=1"

    def test_empty_user_message_returns_none_task(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value=None,
        ):
            req = ProxyRequest(
                model="gpt-4",
                messages=[
                    ProxyMessage(role="system", content="helpful"),
                    ProxyMessage(role="user", content=""),
                ],
            )
            result = asyncio.run(evaluate_signal(req, tmp_path))
        assert result.should_compose is False
        assert result.task is None

    def test_domain_tags_from_skill(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='test'")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["test"], domain_tags=["auth", "payments"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=_no_transition(),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("run tests"), tmp_path))
        assert result.should_compose is True
        assert result.domain_tags == ["auth", "payments"]


class TestProxyLifecycleMode:
    """The proxy honors per-repo lifecycle_mode: any non-`full` mode (`off`, and
    the legacy `assist` which now reads as `off`) defers to plain passthrough even
    when a phase file is present and would otherwise compose."""

    @staticmethod
    def _set_mode(tmp_path: Path, mode: str) -> None:
        d = tmp_path / ".agentalloy"
        d.mkdir(exist_ok=True)
        (d / "config").write_text(f"lifecycle_mode: {mode}\n")

    def test_off_passthrough_even_with_phase(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        self._set_mode(tmp_path, "off")
        # Tripwire: the guard must short-circuit before any skill load / trigger.
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            side_effect=AssertionError("must not evaluate the lifecycle in off mode"),
        ):
            result = asyncio.run(evaluate_signal(_req("run the test suite"), tmp_path))
        assert result.should_compose is False

    def test_legacy_assist_defers_as_off(self, tmp_path: Path) -> None:
        # `assist` was removed with the hook transport; a repo still carrying it
        # reads as `off` and must defer (compose nothing).
        _set_phase(tmp_path, "build")
        self._set_mode(tmp_path, "assist")
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            side_effect=AssertionError("legacy assist must read as off and not evaluate"),
        ):
            result = asyncio.run(evaluate_signal(_req("run the test suite"), tmp_path))
        assert result.should_compose is False

    def test_explicit_full_still_composes(self, tmp_path: Path) -> None:
        # Explicit `full` behaves exactly as the default (no-config) path.
        _set_phase(tmp_path, "build")
        self._set_mode(tmp_path, "full")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='test'")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["test"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=mock_match,
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.decide_transition",
                return_value=_no_transition(),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("run the test suite"), tmp_path))
        assert result.should_compose is True


class TestMissingProjectRootWarning:
    """An unmounted project root (no `.agentalloy/` visible) must warn, not pass silently."""

    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        proxy_signal._warned_missing_root.clear()

    def test_missing_agentalloy_dir_warns_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No `.agentalloy/` at all → lifecycle defaults to "full", phase read
        # fails → the "not visible to the proxy" warning fires exactly once per cwd.
        with caplog.at_level(logging.WARNING, logger="agentalloy.api.proxy_signal"):
            r1 = asyncio.run(evaluate_signal(_req("hi"), tmp_path))
            r2 = asyncio.run(evaluate_signal(_req("hi"), tmp_path))
        assert r1.should_compose is False
        assert r2.should_compose is False
        warns = [r for r in caplog.records if "not visible to the proxy" in r.getMessage()]
        assert len(warns) == 1

    def test_present_agentalloy_dir_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `.agentalloy/` exists but holds no phase file: the root IS visible, so
        # this is a legitimate passthrough — no "not visible" warning.
        (tmp_path / ".agentalloy").mkdir()
        with caplog.at_level(logging.WARNING, logger="agentalloy.api.proxy_signal"):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path))
        assert result.should_compose is False
        assert not any("not visible to the proxy" in r.getMessage() for r in caplog.records)
