"""Signal layer integration tests for proxy requests.

Tests ``evaluate_signal()`` and ``SignalResult`` under the announce-once
cadence: a phase's orientation block is emitted exactly once on entry
(``.agentalloy/announced`` != phase), and the transition eval injects only when
the reranker trigger fires AND the gate yields an advisory. A steady-state turn
(already announced, no trigger) is a pure passthrough — the every-turn flood the
old intake bypass produced is gone.
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


# A stable session id used across a "session"'s turns. Orientation is now keyed
# per (phase, session): seeding the announced set with this and passing it as the
# evaluate_signal session_id makes a turn steady-state (already oriented).
SESSION = "sess-test"


def _set_announced(tmp_path: Path, phase: str, *sessions: str) -> None:
    """Mark *phase* already announced for *sessions* (steady-state, not entry).

    Defaults to :data:`SESSION` so the matching ``session_id=SESSION`` turn is quiet.
    """
    d = tmp_path / ".agentalloy"
    d.mkdir(exist_ok=True)
    keys = ",".join(sessions or (SESSION,))
    (d / "announced").write_text(f"{phase}\t{keys}\n")


def _read_announced(tmp_path: Path) -> str | None:
    """Return the announced *phase* (the announced file stores ``phase\\tkeys``)."""
    f = tmp_path / ".agentalloy" / "announced"
    if not f.exists():
        return None
    return f.read_text().strip().partition("\t")[0] or None


def _skill(
    keywords: list[str],
    phases: list[str] | None = None,
    domain_tags: list[str] | None = None,
    raw_prose: str = "Workflow operating instructions for this phase.",
) -> dict[str, Any]:
    return {
        "signal_keywords": keywords,
        "exit_gates": {},
        "applies_to_phases": phases or ["build"],
        "domain_tags": domain_tags,
        "raw_prose": raw_prose,
    }


def _no_transition(qwen: int = 0) -> MagicMock:
    d = MagicMock()
    d.should_transition = False
    d.to_phase = None
    d.gates_met = []
    d.gates_unmet = []
    d.qwen_calls = qwen
    d.advisories = []
    return d


def _transition(to_phase: str, gate_names: list[str], qwen: int = 1) -> MagicMock:
    d = MagicMock()
    d.should_transition = True
    d.to_phase = to_phase
    d.gates_met = [MagicMock(gate_name=n) for n in gate_names]
    d.gates_unmet = []
    d.qwen_calls = qwen
    d.advisories = []
    return d


def _advisory(messages: list[str], qwen: int = 1) -> MagicMock:
    d = MagicMock()
    d.should_transition = False
    d.to_phase = None
    d.gates_met = []
    d.gates_unmet = [MagicMock(gate_name="exit_artifact")]
    d.qwen_calls = qwen
    d.advisories = messages
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

    def test_entry_announces_even_without_trigger(self, tmp_path: Path) -> None:
        """First turn in a phase announces it, even when no trigger fires.

        The trigger is still consulted (no bypass) — but a fresh phase whose
        `announced` marker doesn't match composes its orientation regardless.
        """
        _set_phase(tmp_path, "build")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["deploy"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ) as mock_trigger,
        ):
            result = asyncio.run(evaluate_signal(_req("just writing code"), tmp_path))
        assert result.should_compose is True
        assert result.announce is True
        assert result.phase == "build"
        mock_trigger.assert_called_once()  # consulted, not bypassed
        assert _read_announced(tmp_path) == "build"  # recorded so the next turn is quiet

    def test_intake_entry_announces_once_then_quiet(self, tmp_path: Path) -> None:
        """Intake announces on the first prompt, then stops — no every-turn flood.

        This is the regression guard for the old unconditional intake bypass.
        """
        _set_phase(tmp_path, "intake")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill([], phases=["intake"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            first = asyncio.run(evaluate_signal(_req("hi"), tmp_path, session_id=SESSION))
            second = asyncio.run(evaluate_signal(_req("still here"), tmp_path, session_id=SESSION))
            third = asyncio.run(evaluate_signal(_req("and again"), tmp_path, session_id=SESSION))
        assert first.should_compose is True and first.announce is True
        assert second.should_compose is False  # already announced (same session) → quiet
        assert third.should_compose is False

    def test_already_announced_trigger_miss_is_passthrough(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        _set_announced(tmp_path, "build")
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
            result = asyncio.run(
                evaluate_signal(_req("just writing code"), tmp_path, session_id=SESSION)
            )
        assert result.should_compose is False
        assert result.announce is False
        assert result.phase == "build"

    def test_already_announced_advisory_composes_as_eval(self, tmp_path: Path) -> None:
        """A trigger hit that yields an advisory injects the eval block (not announce)."""
        _set_phase(tmp_path, "build")
        _set_announced(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='deploy'")
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
                return_value=_advisory(["produce docs/spec/*.md to advance"]),
            ),
        ):
            result = asyncio.run(
                evaluate_signal(_req("are we done?"), tmp_path, session_id=SESSION)
            )
        assert result.should_compose is True
        assert result.announce is False  # eval block, not orientation
        assert result.advisories == ["produce docs/spec/*.md to advance"]
        assert result.pre_filter_matched == "keyword='deploy'"

    def test_phase_gate_embed_failure_surfaced_on_result(self, tmp_path: Path) -> None:
        """A semantic-gate embed failure during eval is surfaced on the result.

        decide_transition runs the gates against the shared ctx; here it records
        an embed failure (as the real classifier does on a 500 / unreachable
        embed). evaluate_signal must read that off ctx and flag it for telemetry
        instead of letting the silently-degraded gate vanish into an UNKNOWN.
        """
        _set_phase(tmp_path, "build")
        _set_announced(tmp_path, "build")  # steady-state: result hinges on the eval
        mock_match = PreFilterMatch(name="intent", detail="intent=completion")

        def _fail_embed(**kwargs: Any) -> MagicMock:
            kwargs["ctx"].record_embed_failure()
            return _advisory(["produce docs/spec/*.md to advance"])

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
                side_effect=_fail_embed,
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("are we done?"), tmp_path, MagicMock()))
        assert result.phase_gate_embed_failed is True

    def test_phase_gate_embed_failed_false_on_clean_eval(self, tmp_path: Path) -> None:
        """A healthy gate eval leaves phase_gate_embed_failed False."""
        _set_phase(tmp_path, "build")
        _set_announced(tmp_path, "build")
        mock_match = PreFilterMatch(name="intent", detail="intent=completion")
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
                return_value=_advisory(["produce docs/spec/*.md to advance"]),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("are we done?"), tmp_path, MagicMock()))
        assert result.phase_gate_embed_failed is False

    def test_clean_transition_writes_phase_without_injecting(self, tmp_path: Path) -> None:
        """A clean transition (gates met, no advisory) advances the phase but
        injects nothing this turn — the new phase announces on the next turn."""
        _set_phase(tmp_path, "build")
        _set_announced(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='deploy'")
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
                return_value=_transition("qa", ["test_passed", "lint_clean"]),
            ),
            mock.patch("agentalloy.api.proxy_signal._write_phase_atomic") as mock_write,
        ):
            result = asyncio.run(evaluate_signal(_req("deploy now"), tmp_path, session_id=SESSION))
        assert result.should_compose is False  # nothing to inject this turn
        mock_write.assert_called_once_with(tmp_path, "qa")
        assert result.gates_met == ["test_passed", "lint_clean"]  # carried for telemetry
        assert result.qwen_calls == 1

    def test_phase_write_error_is_logged_not_raised(self, tmp_path: Path) -> None:
        # Entry turn (announce) + a transition whose write fails: the OSError is
        # swallowed and the announce still composes.
        _set_phase(tmp_path, "build")
        mock_match = PreFilterMatch(name="prompt_keyword", detail="keyword='deploy'")
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
                return_value=_transition("qa", []),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal._write_phase_atomic",
                side_effect=OSError("permission denied"),
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("deploy now"), tmp_path))
        assert result.should_compose is True  # announce survives the write failure

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

    def test_announce_carries_workflow_prose_not_workflow_tags(self, tmp_path: Path) -> None:
        """Tier 1 announce carries the workflow skill's prose; it must NOT source
        domain_tags from the workflow skill (those static process tags were the
        hard filter that emptied retrieval). Domain is Tier 2's job (the contract)."""
        _set_phase(tmp_path, "build")  # entry → Tier 1 announce
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(
                    ["test"],
                    domain_tags=["spec-driven-development", "coding"],
                    raw_prose="Build: work tasks.md top to bottom.",
                ),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("run tests"), tmp_path))
        assert result.should_compose is True
        assert result.announce is True
        assert result.workflow_prose == "Build: work tasks.md top to bottom."
        # The workflow's static process tags never become a retrieval filter.
        assert result.domain_tags == []
        # No contract present → no work-item to compose yet.
        assert result.announce_cursor is False
        assert result.current_contract is None


class TestAnnounceCadence:
    """The `.agentalloy/announced` marker governs re-announcement across entries."""

    def test_reannounces_after_phase_changes(self, tmp_path: Path) -> None:
        # Announced for build; the phase file now reads qa (a transition advanced
        # it). The mismatch makes this an entry turn for qa → announce again.
        _set_phase(tmp_path, "qa")
        _set_announced(tmp_path, "build")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill([], phases=["qa"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("anything"), tmp_path))
        assert result.should_compose is True
        assert result.announce is True
        assert _read_announced(tmp_path) == "qa"

    def test_announce_not_written_when_skill_missing(self, tmp_path: Path) -> None:
        # Skill load fails before the announce decision → no announced marker is
        # written (the repo isn't actually composed for).
        _set_phase(tmp_path, "build")
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value=None,
        ):
            asyncio.run(evaluate_signal(_req("hi"), tmp_path))
        assert _read_announced(tmp_path) is None


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
        # Explicit `full` behaves exactly as the default (no-config) path: a fresh
        # phase announces on entry.
        _set_phase(tmp_path, "build")
        self._set_mode(tmp_path, "full")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["test"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
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


def _seed_contract(tmp_path: Path, phase: str, name: str) -> None:
    d = tmp_path / ".agentalloy" / "contracts" / phase
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nphase: {phase}\ntask_slug: {name}\ndomain_tags: [pytest]\n---\n# {name}\nbody\n"
    )


def _set_state(tmp_path: Path, name: str, value: str) -> None:
    d = tmp_path / ".agentalloy"
    d.mkdir(exist_ok=True)
    (d / name).write_text(f"{value}\n")


class TestTier2Cadence:
    """`.agentalloy/cursor` vs `.agentalloy/composed` govern per-work-item domain
    injection (Tier 2), independent of the phase announce (Tier 1)."""

    def _run(self, tmp_path: Path):
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=_skill(["test"]),
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            return asyncio.run(evaluate_signal(_req("work the task"), tmp_path, session_id=SESSION))

    def test_tier2_fires_on_entry_with_incoming_contract(self, tmp_path: Path) -> None:
        # Fresh build entry with an incoming contract → both tiers fire.
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        result = self._run(tmp_path)
        assert result.announce is True  # Tier 1
        assert result.announce_cursor is True  # Tier 2
        assert result.current_contract is not None
        assert result.current_contract.endswith("build/01-cache.md")
        # Composed cadence recorded so the next steady turn stays quiet.
        from agentalloy.signals.skill_loader import _read_composed

        assert _read_composed(tmp_path) == "build/01-cache.md"

    def test_tier2_quiet_after_compose(self, tmp_path: Path) -> None:
        # Already announced + already composed this cursor, no trigger → quiet.
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _set_announced(tmp_path, "build")
        _set_state(tmp_path, "composed", "build/01-cache.md")
        result = self._run(tmp_path)
        assert result.should_compose is False
        assert result.announce is False
        assert result.announce_cursor is False

    def test_tier2_refires_after_task_next(self, tmp_path: Path) -> None:
        # Cursor advanced to a new task (task next), phase already announced →
        # Tier 1 stays quiet, Tier 2 fires for the new work-item only.
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _seed_contract(tmp_path, "build", "02-api")
        _set_announced(tmp_path, "build")
        _set_state(tmp_path, "composed", "build/01-cache.md")
        _set_state(tmp_path, "cursor", "build/02-api.md")
        result = self._run(tmp_path)
        assert result.should_compose is True
        assert result.announce is False
        assert result.announce_cursor is True
        assert result.current_contract.endswith("build/02-api.md")
