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
from agentalloy.signals.skill_loader import (
    _read_announced as _store_read_announced,  # pyright: ignore[reportPrivateUsage]
)
from tests.support import seed_announced, seed_orientation, seed_phase


def _req(prompt: str, *, tools: bool = True) -> ProxyRequest:
    # Carries a tool array by default, modelling a genuine agent turn. Pass
    # tools=False to model tool-less harnesses; with a session_id the request is
    # still a carrier (session_key present => carrier), and without one it resolves
    # by fingerprint.
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=prompt)],
        tools=[{"name": "Read", "description": "read a file", "input_schema": {}}]
        if tools
        else None,
    )


def _set_phase(tmp_path: Path, phase: str) -> None:
    phase_dir = tmp_path / ".agentalloy"
    phase_dir.mkdir(exist_ok=True)
    seed_phase(tmp_path, phase)


# A stable session id used across a "session"'s turns. Orientation is now keyed
# per (phase, session): seeding the announced set with this and passing it as the
# evaluate_signal session_id makes a turn steady-state (already oriented).
SESSION = "sess-test"


def _set_announced(tmp_path: Path, phase: str, *sessions: str) -> None:
    """Mark *phase* already announced for *sessions* (steady-state, not entry).

    Defaults to :data:`SESSION` so the matching ``session_id=SESSION`` turn is quiet.
    """
    seed_announced(tmp_path, phase, list(sessions or (SESSION,)))


def _read_announced(tmp_path: Path) -> str | None:
    """Return the announced *phase* from the store."""
    return _store_read_announced(tmp_path)


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


class TestExtractTaskFromMessages:
    """Regression guard: the task prompt must track the CURRENT turn.

    A chat-completions request resends the full history every call, so
    scanning forward and returning on the first user message pins
    ``recent_prompt_text`` (and therefore signal-keyword phase transitions) to
    the session's opening line for its entire lifetime.
    """

    def test_uses_latest_user_message_not_first(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        request = ProxyRequest(
            model="gpt-4",
            messages=[
                ProxyMessage(role="user", content="hi"),
                ProxyMessage(role="assistant", content="Hello! How can I help?"),
                ProxyMessage(role="user", content="ready to scope"),
            ],
        )
        result = asyncio.run(evaluate_signal(request, tmp_path))
        assert result.task == "ready to scope"

    def test_flattens_block_content_of_latest_message(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "build")
        request = ProxyRequest(
            model="gpt-4",
            messages=[
                ProxyMessage(role="user", content="hi"),
                ProxyMessage(
                    role="user",
                    content=[{"type": "text", "text": "start spec"}],
                ),
            ],
        )
        result = asyncio.run(evaluate_signal(request, tmp_path))
        assert result.task == "start spec"


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
        # The decision is captured as a pending marker; evaluate_signal no longer
        # writes `.agentalloy/announced` itself — the injection path commits it only
        # after the orientation block is actually delivered.
        assert result.pending_announce is not None and result.pending_announce[0] == "build"
        assert _read_announced(tmp_path) is None

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
            # Simulate the injection path committing after the first turn delivers.
            proxy_signal.commit_markers(
                tmp_path, first, announce_emitted=True, cursor_emitted=False
            )
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
        mock_write.assert_called_once_with(tmp_path, "qa", session_key=SESSION)
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
        # The re-announce decision targets qa; the on-disk marker is untouched by
        # evaluate_signal (still the stale "build") until the injection path commits.
        assert result.pending_announce is not None and result.pending_announce[0] == "qa"
        assert _read_announced(tmp_path) == "build"

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

    def test_off_seeds_no_phase_in_a_fresh_repo(self, tmp_path: Path) -> None:
        """An `off` repo with no phase stays phase-less — the guard runs first.

        This is what lets `add --lifecycle-mode off` leave an existing phase row
        alone instead of clearing it: in `off` the row is never read, so it is
        inert rather than stale, and it is still there if the mode goes back on.
        Reorder the guard below the lazy seed and `off` repos silently start
        acquiring phases.
        """
        from agentalloy.signals.skill_loader import _read_phase  # noqa: PLC0415

        self._set_mode(tmp_path, "off")
        result = asyncio.run(evaluate_signal(_req("run the test suite"), tmp_path, mutate=True))
        assert result.should_compose is False
        assert _read_phase(tmp_path) is None

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

    def test_present_agentalloy_dir_seeds_intake_instead_of_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `.agentalloy/` exists but the repo has no phase row: the root IS
        # visible, so this is a freshly wired repo, not an invisible one. Wiring
        # deliberately seeds no state, so the entry phase is seeded here, on the
        # first request — a wired repo that stayed inert until someone ran
        # `phase set` by hand was the "wired but nothing happens" trap.
        (tmp_path / ".agentalloy").mkdir()
        with caplog.at_level(logging.WARNING, logger="agentalloy.api.proxy_signal"):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path))
        assert result.should_compose is True
        assert result.phase == "intake"
        assert not any("not visible to the proxy" in r.getMessage() for r in caplog.records)
        # The seed lands in the store, not back in the file the migration
        # removed — the one assertion that tells the two destinations apart.
        assert not (tmp_path / ".agentalloy" / "phase").exists()

    def test_phaseless_read_only_evaluation_seeds_nothing(self, tmp_path: Path) -> None:
        """``mutate=False`` evaluates as intake without recording it."""
        from agentalloy.signals.skill_loader import _read_phase  # noqa: PLC0415

        (tmp_path / ".agentalloy").mkdir()
        result = asyncio.run(evaluate_signal(_req("hi"), tmp_path, mutate=False))
        assert result.phase == "intake"
        assert _read_phase(tmp_path) is None


def _seed_contract(tmp_path: Path, phase: str, name: str) -> None:
    d = tmp_path / ".agentalloy" / "contracts" / "active" / phase
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
        assert result.current_contract.endswith("active/build/01-cache.md")
        # Tier 2 cadence is recorded as a pending marker; evaluate_signal no longer
        # writes `.agentalloy/composed` — the injection path commits it post-delivery.
        from agentalloy.signals.skill_loader import _read_composed

        assert result.pending_composed == "active/build/01-cache.md"
        assert _read_composed(tmp_path) is None

    def test_tier2_quiet_after_compose(self, tmp_path: Path) -> None:
        # Already announced + already composed this cursor, no trigger → quiet.
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _set_announced(tmp_path, "build")
        _set_state(tmp_path, "composed", "active/build/01-cache.md")
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
        _set_state(tmp_path, "composed", "active/build/01-cache.md")
        _set_state(tmp_path, "cursor", "active/build/02-api.md")
        result = self._run(tmp_path)
        assert result.should_compose is True
        assert result.announce is False
        assert result.announce_cursor is True
        assert result.current_contract.endswith("active/build/02-api.md")

    def test_tier2_silent_on_uncursored_fanout(self, tmp_path: Path) -> None:
        # Strict resolver (Outcome B): ≥2 contracts with NO cursor → Tier 2 stays
        # silent rather than guess a mis-scoped work-item. This is the fail-safe
        # floor; in normal flow the cursor is seeded on phase entry (next test).
        # Drive the REAL builder (not a stubbed signal).
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _seed_contract(tmp_path, "build", "02-api")
        _set_announced(tmp_path, "build")  # Tier 1 already announced → quiet
        result = self._run(tmp_path)
        assert result.announce is False  # Tier 1 stays quiet
        assert result.announce_cursor is False  # no cursor → silent, not a guess

    def test_tier2_fires_on_seeded_cursor(self, tmp_path: Path) -> None:
        # Phase entry seeds the cursor to the first work-item (01-, filename order);
        # Tier 2 then fires on THAT seeded task — tag-scoped compose, no free-text
        # fallback. `_set_state(..., "cursor", ...)` stands in for the on-entry seed.
        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _seed_contract(tmp_path, "build", "02-api")
        _set_announced(tmp_path, "build")  # Tier 1 already announced → quiet
        _set_state(tmp_path, "cursor", "active/build/01-cache.md")  # seeded on entry
        result = self._run(tmp_path)
        assert result.announce is False  # Tier 1 stays quiet
        assert result.announce_cursor is True  # Tier 2 fires on the seeded work-item
        assert result.current_contract is not None
        assert result.current_contract.endswith("active/build/01-cache.md")


def _gates_with_sections() -> dict[str, Any]:
    """An exit-gate spec mirroring the spec phase: a path glob + required sections."""
    return {
        "all_of": [
            {"artifact_exists": {"path": "docs/spec/*.md"}},
            {
                "artifact_contains": {
                    "path": "docs/spec/*.md",
                    "sections": ["Acceptance Criteria", "Out of Scope"],
                }
            },
        ]
    }


def _store_gates() -> dict[str, Any]:
    """The post-migration shape: store-backed `phase`+`name`, no filesystem path."""
    return {
        "all_of": [
            {"artifact_exists": {"phase": "design", "name": "approach.md"}},
            {
                "artifact_contains": {
                    "phase": "design",
                    "name": "approach.md",
                    "sections": ["Approach", "Decisions"],
                }
            },
        ]
    }


class _FakeStore:
    """Minimal `list_artifacts` stand-in: `{(phase, name): body}`."""

    def __init__(self, rows: dict[tuple[str, str], str] | None = None) -> None:
        self._rows = rows or {}

    def list_artifacts(
        self, phase: str, *, slug: str | None = None, name_glob: str | None = None
    ) -> list[dict[str, Any]]:
        import fnmatch

        out: list[dict[str, Any]] = []
        for (p, name), body in self._rows.items():
            if p != phase:
                continue
            if name_glob is not None and not fnmatch.fnmatch(name, name_glob):
                continue
            out.append({"phase": p, "slug": slug or "x", "name": name, "content": body})
        return out


class TestStoreBackedBanner:
    """#587 §1 — the directive names the STORE artifact, declaratively."""

    def test_names_the_unrecorded_store_artifact(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _store_gates(), tmp_path, store=_FakeStore())
        assert banner.startswith("[agentalloy · design] approach.md not yet recorded")

    def test_directive_names_no_filesystem_path(self, tmp_path: Path) -> None:
        """The failure #587's draft table would have reintroduced."""
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _store_gates(), tmp_path, store=_FakeStore())
        for token in ("docs/", ".agentalloy/", "/"):
            assert token not in banner.split("]", 1)[1], f"{token!r} leaked into {banner!r}"

    def test_directive_is_declarative_not_imperative(self, tmp_path: Path) -> None:
        """Imperatives in the last USER message trip injected-content defenses."""
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _store_gates(), tmp_path, store=_FakeStore())
        lowered = banner.lower()
        for verb in ("produce ", "create ", "write ", "you must", "run `"):
            assert verb not in lowered, f"imperative {verb!r} in {banner!r}"

    def test_store_sections_scored_and_shown(self, tmp_path: Path) -> None:
        """Progress against store rows — dead before this, since the synthesized
        `docs/<phase>/<name>` glob never exists on disk."""
        from agentalloy.api.proxy_signal import build_banner

        store = _FakeStore({("design", "approach.md"): "# t\n## Approach\nx\n"})
        banner = build_banner("design", _store_gates(), tmp_path, store=store)
        assert "1/2 sections" in banner
        assert "(missing: Decisions)" in banner
        # Recorded → the directive drops back to the plain pointer.
        assert "not yet recorded" not in banner

    def test_no_store_degrades_to_plain_directive(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _store_gates(), tmp_path, store=None)
        assert banner.startswith("[agentalloy · design] approach.md not yet recorded")

    @pytest.mark.parametrize(
        "gates",
        [
            {"all_of": [{"artifact_exists": {"path": ".agentalloy/contracts/build/*.md"}}]},
            {"all_of": [{"artifact_exists": {"path": "docs/fast/*.md"}}]},
            {"all_of": [{"artifact_exists": {"path": "docs/spec/<slug>/spec.md"}}]},
            {
                "all_of": [
                    {"artifact_exists": {"path": "docs/design/**/tasks.md"}},
                    {
                        "artifact_contains": {
                            "path": "docs/design/**/tasks.md",
                            "sections": ["Tasks"],
                        }
                    },
                ]
            },
        ],
    )
    @pytest.mark.parametrize("phase", ["design", "build", "sdd-fast", "mystery"])
    def test_legacy_disk_gate_never_reaches_the_banner(
        self, tmp_path: Path, phase: str, gates: dict[str, Any]
    ) -> None:
        """A legacy pack's disk-path gate must not surface a path, on any phase.

        This is the real-repo case: a repo wired before the store migration still
        carries `path:` gates. Both branches are covered — a KNOWN phase (plain
        directive + filtered checkpoint) and an unknown one (gate-derived fallback).
        The banner is the highest-frequency injection surface there is, so a path
        here is a standing instruction to write lifecycle files to disk.
        """
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner(phase, gates, tmp_path, slug="feat", store=_FakeStore())
        body = banner.split("]", 1)[1]
        for token in ("docs/", ".agentalloy", ".md", "contracts"):
            assert token not in body, f"{token!r} leaked into {banner!r}"

    def test_glob_name_is_not_rendered_as_a_filename(self, tmp_path: Path) -> None:
        """`*.md not yet recorded` reads as a filename to create."""
        from agentalloy.api.proxy_signal import build_banner

        gates = {"all_of": [{"artifact_exists": {"phase": "sdd-fast", "name": "*.md"}}]}
        banner = build_banner("sdd-fast", gates, tmp_path, store=_FakeStore())
        assert "*.md" not in banner
        assert "its artifact not yet recorded" in banner


def _design_gates() -> dict[str, Any]:
    """Mirror the real design exit gate: three docs each in their OWN file (one section
    apiece) plus the section-less build-contract checkpoint."""
    return {
        "all_of": [
            {"artifact_exists": {"path": "docs/design/**/approach.md"}},
            {"artifact_contains": {"path": "docs/design/**/approach.md", "sections": ["Approach"]}},
            {"artifact_exists": {"path": "docs/design/**/tasks.md"}},
            {"artifact_contains": {"path": "docs/design/**/tasks.md", "sections": ["Tasks"]}},
            {"artifact_exists": {"path": "docs/design/**/test-plan.md"}},
            {
                "artifact_contains": {
                    "path": "docs/design/**/test-plan.md",
                    "sections": ["Test Cases"],
                }
            },
            {"artifact_exists": {"path": ".agentalloy/contracts/active/build/*.md"}},
        ]
    }


class TestExtractGateSections:
    """`_extract_gate_sections` pulls `artifact_contains.sections` from a gate spec."""

    def test_pulls_sections_in_order(self) -> None:
        from agentalloy.signals.prefilter import _extract_gate_sections

        assert _extract_gate_sections(_gates_with_sections()) == [
            "Acceptance Criteria",
            "Out of Scope",
        ]

    def test_empty_when_no_artifact_contains(self) -> None:
        from agentalloy.signals.prefilter import _extract_gate_sections

        assert _extract_gate_sections({"artifact_exists": {"path": "x.md"}}) == []
        assert _extract_gate_sections({}) == []

    def test_dedups_repeated_sections(self) -> None:
        from agentalloy.signals.prefilter import _extract_gate_sections

        spec = {
            "any_of": [
                {"artifact_contains": {"path": "a.md", "sections": ["A", "B"]}},
                {"artifact_contains": {"path": "b.md", "sections": ["B", "C"]}},
            ]
        }
        assert _extract_gate_sections(spec) == ["A", "B", "C"]


class TestBuildBanner:
    """`build_banner` renders the one-line `[agentalloy · {phase}] {directive}{progress}`."""

    def test_directive_from_phase_map(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # A known SDD phase uses its hand-tuned directive; no artifact yet → no
        # progress suffix. Directive now points to system prompt.
        banner = build_banner("spec", _gates_with_sections(), tmp_path)
        assert banner == "[agentalloy · spec] phase instructions: system prompt"

    def test_unknown_phase_does_not_name_a_disk_path(self, tmp_path: Path) -> None:
        """An unknown phase's directive must NOT echo its gate's filesystem path.

        It used to render "out.md not yet produced". An unrecognized phase is by
        definition a custom pack, and a legacy one still carries disk-path gates like
        `.agentalloy/contracts/build/*.md` — naming that path in the banner is a
        standing instruction to write lifecycle artifacts to disk, at the highest
        frequency injection point there is.
        """
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("mystery", {"artifact_exists": {"path": "out.md"}}, tmp_path)
        assert "out.md" not in banner
        assert banner.startswith("[agentalloy · mystery] mystery exit gate not yet satisfied")

    def test_unknown_phase_no_path_falls_back_to_satisfy_gate(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("mystery", {}, tmp_path)
        assert (
            banner == "[agentalloy · mystery] mystery exit gate not yet satisfied · "
            "phase instructions: system prompt"
        )

    def test_progress_appended_when_artifact_exists(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        (tmp_path / "docs" / "spec").mkdir(parents=True)
        (tmp_path / "docs" / "spec" / "f.md").write_text("# T\n## Acceptance Criteria\nx\n")
        banner = build_banner("spec", _gates_with_sections(), tmp_path)
        assert "1/2 sections" in banner
        assert "(missing: Out of Scope)" in banner

    def test_full_progress_no_missing_suffix(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        (tmp_path / "docs" / "spec").mkdir(parents=True)
        (tmp_path / "docs" / "spec" / "f.md").write_text(
            "## Acceptance Criteria\nx\n## Out of Scope\ny\n"
        )
        banner = build_banner("spec", _gates_with_sections(), tmp_path)
        assert banner.endswith("2/2 sections")
        assert "missing" not in banner

    def test_no_progress_without_required_sections(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # Gate has a path but no `sections` → no progress suffix even if file exists.
        (tmp_path / "out.md").write_text("# T\n## Anything\n")
        banner = build_banner("mystery", {"artifact_exists": {"path": "out.md"}}, tmp_path)
        assert "sections" not in banner
        assert banner.startswith("[agentalloy · mystery] mystery exit gate not yet satisfied")

    def _write_design_docs(self, tmp_path: Path, *, slug: str, which: set[str]) -> None:
        d = tmp_path / "docs" / "design" / slug
        d.mkdir(parents=True, exist_ok=True)
        files = {
            "approach": ("approach.md", "## Approach"),
            "tasks": ("tasks.md", "## Tasks"),
            "test-plan": ("test-plan.md", "## Test Cases"),
        }
        for key in which:
            name, heading = files[key]
            (d / name).write_text(f"# {slug}\n{heading}\n\nbody\n")

    def test_sections_scored_per_gate_against_own_file(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # Each required heading lives in ITS OWN file. The fixed banner scores each
        # section against its gate's path → 3/3, no missing. (The old bug scored all
        # three against approach.md only and reported "1/3 (missing: Tasks)".)
        self._write_design_docs(tmp_path, slug="feat", which={"approach", "tasks", "test-plan"})
        banner = build_banner("design", _design_gates(), tmp_path, slug="feat")
        assert "3/3 sections" in banner
        assert "missing" not in banner

    def test_missing_sections_joined_across_files(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # Only approach.md written → Tasks and Test Cases both missing, both shown.
        self._write_design_docs(tmp_path, slug="feat", which={"approach"})
        banner = build_banner("design", _design_gates(), tmp_path, slug="feat")
        assert "1/3 sections" in banner
        assert "(missing: Tasks, Test Cases)" in banner

    def test_contract_disk_checkpoint_is_not_surfaced(self, tmp_path: Path) -> None:
        """A `.agentalloy/contracts/**` checkpoint no longer reaches the banner.

        Contracts are store-backed; the real design pack declares no such gate. A
        legacy pack that still does must not get its disk location advertised on the
        highest-frequency injection surface.
        """
        from agentalloy.api.proxy_signal import build_banner

        self._write_design_docs(tmp_path, slug="feat", which={"approach", "tasks", "test-plan"})
        banner = build_banner("design", _design_gates(), tmp_path, slug="feat")
        assert "build contracts" not in banner
        assert ".agentalloy" not in banner

    def test_code_checkpoint_surfaced_then_cleared(self, tmp_path: Path) -> None:
        """`src/**` IS a real disk deliverable, so its checkpoint still surfaces."""
        from agentalloy.api.proxy_signal import build_banner

        gates = {"all_of": [{"artifact_exists": {"path": "src/**"}}]}
        banner = build_banner("build", gates, tmp_path)
        assert "· 0 src (need ≥1)" in banner
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text("x = 1\n")
        assert "need ≥1" not in build_banner("build", gates, tmp_path)

    def test_slug_resolved_in_directive(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # Directive is a generic pointer; slug is resolved in the progress suffix.
        banner = build_banner("design", _design_gates(), tmp_path, slug="calendar-web-ui")
        assert "[agentalloy · design] phase instructions: system prompt" in banner
        assert "<slug>" not in banner

    def test_slug_left_literal_when_unknown(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # Directive is a generic pointer; no <slug> placeholder in the directive itself.
        banner = build_banner("design", _design_gates(), tmp_path)
        assert "[agentalloy · design] phase instructions: system prompt" in banner


class TestEvaluateSignalBanner:
    """`evaluate_signal` sets `banner` on carrier turns under the active mode only."""

    def test_carrier_turn_sets_banner(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value={
                    "signal_keywords": [],
                    "exit_gates": _gates_with_sections(),
                    "applies_to_phases": ["spec"],
                    "raw_prose": "spec prose",
                },
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            result = asyncio.run(evaluate_signal(_req("work"), tmp_path))
        assert result.banner is not None
        assert result.banner.startswith("[agentalloy · spec]")

    def test_tool_less_carrier_request_sets_banner(self, tmp_path: Path) -> None:
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value={
                    "signal_keywords": [],
                    "exit_gates": _gates_with_sections(),
                    "applies_to_phases": ["spec"],
                    "raw_prose": "spec prose",
                },
            ),
            mock.patch(
                "agentalloy.api.proxy_signal.check_transition_trigger",
                return_value=None,
            ),
        ):
            # Unified carrier gate: session_key present => carrier, regardless of tools.
            # A tool-less request with a session_id now gets the same treatment as a
            # tool-bearing one (including banner). This is the fix for the
            # "no orientation block" bug where header-sourced sessions were starved.
            result = asyncio.run(
                evaluate_signal(_req("work", tools=False), tmp_path, session_id="sess-bg")
            )
        assert result.banner is not None

    def test_lifecycle_off_leaves_banner_none(self, tmp_path: Path) -> None:
        d = tmp_path / ".agentalloy"
        d.mkdir()
        seed_phase(tmp_path, "spec")
        (d / "config").write_text("lifecycle_mode: off\n")
        result = asyncio.run(evaluate_signal(_req("work"), tmp_path))
        assert result.should_compose is False
        assert result.banner is None

    def test_banner_set_even_when_no_workflow_skill(self, tmp_path: Path) -> None:
        # No profile/packs skill for the phase, but a carrier turn still gets a
        # best-effort banner derived from the packaged exit gate (corpus-free).
        _set_phase(tmp_path, "spec")
        with mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value=None,
        ):
            result = asyncio.run(evaluate_signal(_req("work"), tmp_path))
        assert result.should_compose is False
        assert result.banner is not None
        assert result.banner.startswith("[agentalloy · spec]")


class TestBannerCadence:
    """The per-turn banner is throttled to once every N carrier turns (token saving)."""

    @staticmethod
    def _spec_skill(phases: list[str]) -> dict[str, Any]:
        return {
            "signal_keywords": [],
            "exit_gates": _gates_with_sections(),
            "applies_to_phases": phases,
            "raw_prose": "p",
        }

    def test_static_banner_emits_once_per_phase(self, tmp_path: Path) -> None:
        """Phase entry emits; identical repeats are suppressed by the content hash.

        Two throttles compose (#587): the adaptive cadence decides which turns MAY
        emit (spec = every 3rd), and the content hash decides whether the text
        actually differs from what the agent already has. With nothing changing, the
        net is one emission per phase — the issue's stated goal (~1-2 per 30 turns
        vs 6). Progress landing in the store changes the text and re-emits; that is
        covered by `test_store_progress_change_re_emits`.
        """
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._spec_skill(["spec"]),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            emitted = [
                asyncio.run(evaluate_signal(_req("same task"), tmp_path, session_id=SESSION)).banner
                is not None
                for _ in range(6)
            ]
        assert emitted == [True, False, False, False, False, False]

    def test_disk_progress_change_re_emits(self, tmp_path: Path) -> None:
        """A banner whose text changed is emitted on the next cadence tick.

        The dedup must suppress *redundancy*, not *news*. This covers the legacy
        filesystem scorer (`_gates_with_sections` is a `docs/spec/*.md` gate); the
        store path is covered by `test_store_progress_change_re_emits`.
        """
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._spec_skill(["spec"]),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            first = asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
            assert first is not None
            # Turns 2-3 are within cadence and identical → suppressed.
            for _ in range(2):
                assert (
                    asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
                    is None
                )
            # Land a section: the rendered banner now differs.
            spec_dir = tmp_path / "docs" / "spec"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "f.md").write_text("# T\n## Acceptance Criteria\nx\n")
            later = [
                asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
                for _ in range(3)
            ]
        changed = [b for b in later if b is not None]
        assert changed, "content changed but the banner never re-emitted"
        assert "sections" in changed[0]

    def test_store_progress_change_re_emits(self, tmp_path: Path) -> None:
        """An artifact landing via `artifact-set` re-emits, end to end.

        This is the interaction that matters: spec/design/plan are all store-backed,
        so if the hash dedup swallowed store progress the agent would see one banner
        on phase entry and nothing thereafter — no matter how much it recorded.
        """
        _set_phase(tmp_path, "design")
        store = _FakeStore()
        skill = {
            "signal_keywords": [],
            "exit_gates": _store_gates(),
            "applies_to_phases": ["design"],
            "raw_prose": "p",
        }
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase", return_value=skill
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
            mock.patch("agentalloy.api.proxy_signal._banner_store", return_value=store),
        ):
            first = asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
            assert first is not None
            assert "approach.md not yet recorded" in first
            # Identical turns inside the cadence: suppressed.
            for _ in range(3):
                assert (
                    asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
                    is None
                )
            # Record the artifact with one of its two sections.
            store._rows[("design", "approach.md")] = "# t\n## Approach\nx\n"
            later = [
                asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
                for _ in range(5)
            ]
        changed = [b for b in later if b is not None]
        assert changed, "store artifact recorded but the banner never re-emitted"
        assert "not yet recorded" not in changed[0]
        assert "sections" in changed[0]

    def test_re_emits_on_phase_change_within_cadence(self, tmp_path: Path) -> None:
        # A phase change resets the cadence so the banner re-fires on phase entry even
        # before the next tick (it aligns with the once-per-phase orientation block).
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._spec_skill(["spec", "design"]),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            b1 = asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
            b2 = asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
            _set_phase(tmp_path, "design")
            b3 = asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
        assert b1 is not None  # turn 1 (count 0) emits
        assert b2 is None  # turn 2 (count 1) suppressed
        assert b3 is not None and b3.startswith("[agentalloy · design]")  # reset + emit

    def test_env_override_restores_every_turn_cadence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env override still makes EVERY turn a cadence tick.

        The content hash is a separate, later gate, so identical text is still
        suppressed — the override controls throttling, not dedup. Asserted via
        `_adaptive_banner_cadence` directly since the end-to-end banner is
        additionally subject to the hash.
        """
        from agentalloy.api.proxy_signal import _adaptive_banner_cadence

        monkeypatch.setenv("AGENTALLOY_BANNER_TURN_CADENCE", "1")
        # Overrides the phase base (build would otherwise be 10) and the stretch.
        assert _adaptive_banner_cadence("build", 0) == 1
        assert _adaptive_banner_cadence("build", 100) == 1
        assert _adaptive_banner_cadence("spec", 0) == 1

    def test_adaptive_cadence_is_phase_aware_and_stretches(self) -> None:
        """Build is wide (heads-down coding); intake/spec are tight (drift-prone)."""
        from agentalloy.api.proxy_signal import _adaptive_banner_cadence

        assert _adaptive_banner_cadence("build", 0) == 10
        assert _adaptive_banner_cadence("intake", 0) == 2
        assert _adaptive_banner_cadence("spec", 0) == 3
        assert _adaptive_banner_cadence("nonesuch", 0) == 5
        # Stretches as the phase's shape is internalized.
        assert _adaptive_banner_cadence("spec", 25) == 4  # 3 * 1.5
        assert _adaptive_banner_cadence("spec", 60) == 6  # 3 * 2
        # Never suppressed entirely.
        assert _adaptive_banner_cadence("intake", 999) >= 1


class TestOrientationAnnounceCadence:
    """The orientation marker fires once per (phase, session), tracked via its own
    cadence file (_read_orientation_announced / _write_orientation_announced_atomic)."""

    @staticmethod
    def _build_skill() -> dict[str, Any]:
        return {
            "signal_keywords": [],
            "exit_gates": _gates_with_sections(),
            "applies_to_phases": ["build"],
            "raw_prose": "Workflow instructions.",
        }

    def test_orientation_fires_on_new_session(self, tmp_path: Path) -> None:
        """Orientation fires on the first request of a session for the current phase."""
        _set_phase(tmp_path, "build")
        # Orientation cadence is fresh (no seed_orientation call).
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._build_skill(),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path, session_id=SESSION))
        assert result.announce_orientation is True
        assert result.pending_orientation is not None
        assert result.pending_orientation[0] == "build"

    def test_orientation_does_not_fire_when_already_oriented(self, tmp_path: Path) -> None:
        """Orientation does not fire when the session is already oriented for this phase."""
        _set_phase(tmp_path, "build")
        seed_orientation(tmp_path, "build", [SESSION])
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._build_skill(),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path, session_id=SESSION))
        assert result.announce_orientation is False
        assert result.pending_orientation is None

    def test_orientation_fires_on_phase_change(self, tmp_path: Path) -> None:
        """Orientation fires when the phase changes, even for an already-oriented session."""
        _set_phase(tmp_path, "spec")
        seed_orientation(tmp_path, "build", [SESSION])
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value={
                    "signal_keywords": [],
                    "exit_gates": _gates_with_sections(),
                    "applies_to_phases": ["spec"],
                    "raw_prose": "Spec workflow.",
                },
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path, session_id=SESSION))
        assert result.announce_orientation is True
        assert result.pending_orientation is not None
        assert result.pending_orientation[0] == "spec"

    def test_orientation_does_not_fire_for_anonymous_requests(self, tmp_path: Path) -> None:
        """Orientation does not fire when there is no session key (anonymous request)."""
        _set_phase(tmp_path, "build")
        # No session_id passed — the request is not a carrier for orientation.
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._build_skill(),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path))
        assert result.announce_orientation is False
        assert result.pending_orientation is None

    def test_orientation_session_key_capped_at_max(self, tmp_path: Path) -> None:
        """Orientation session keys are capped at _MAX_ANNOUNCED_SESSIONS (8)."""
        _set_phase(tmp_path, "build")
        # Seed with 8 sessions already — adding a 9th should cap.
        sessions = [f"sess-{i}" for i in range(8)]
        seed_orientation(tmp_path, "build", sessions)
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._build_skill(),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            # sess-8 is new — should announce and cap.
            result = asyncio.run(evaluate_signal(_req("hi"), tmp_path, session_id="sess-8"))
        assert result.announce_orientation is True
        assert result.pending_orientation is not None
        _, pending_sessions = result.pending_orientation
        assert len(pending_sessions) <= proxy_signal._MAX_ANNOUNCED_SESSIONS
