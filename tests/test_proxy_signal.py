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


def _req(prompt: str, *, tools: bool = True) -> ProxyRequest:
    # Carries a tool array by default, modelling a genuine agent turn. Pass
    # tools=False (with a session_id header at the call site) to model a background
    # micro-request that must not burn markers; tool-less requests without a header
    # are fingerprint-keyed and DO carry.
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
        # Tier 2 cadence is recorded as a pending marker; evaluate_signal no longer
        # writes `.agentalloy/composed` — the injection path commits it post-delivery.
        from agentalloy.signals.skill_loader import _read_composed

        assert result.pending_composed == "build/01-cache.md"
        assert _read_composed(tmp_path) is None

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

    def test_tier2_fires_on_fanout_without_cursor(self, tmp_path: Path) -> None:
        # The production condition: a phase holding ≥2 contracts with no cursor
        # (contracts accumulate; the cursor is rarely managed). Pre-fix this went
        # silent → every such compose fell to the free-text path (contract_tags in
        # 0/11,162 telemetry rows). Now Tier 2 fires on the newest work-item so the
        # compose stays tag-scoped. Drive the REAL builder (not a stubbed signal).
        import os

        _set_phase(tmp_path, "build")
        _seed_contract(tmp_path, "build", "01-cache")
        _seed_contract(tmp_path, "build", "02-api")
        _set_announced(tmp_path, "build")  # Tier 1 already announced → quiet
        # 02-api is unambiguously newest regardless of write order / mtime coarseness.
        d = tmp_path / ".agentalloy" / "contracts" / "build"
        os.utime(d / "01-cache.md", (1000, 1000))
        os.utime(d / "02-api.md", (3000, 3000))
        result = self._run(tmp_path)
        assert result.announce is False  # Tier 1 stays quiet
        assert result.announce_cursor is True  # Tier 2 no longer silent
        assert result.current_contract is not None
        assert result.current_contract.endswith("build/02-api.md")


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
            {"artifact_exists": {"path": ".agentalloy/contracts/build/*.md"}},
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

        # A known SDD phase uses its hand-tuned MUST directive; no artifact yet → no
        # progress suffix.
        banner = build_banner("spec", _gates_with_sections(), tmp_path)
        assert banner == (
            "[agentalloy · spec] MUST write docs/spec/<slug>.md "
            "(Acceptance Criteria + Out of Scope) before designing or coding"
        )

    def test_unknown_phase_falls_back_to_gate_path(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        # An unrecognized phase derives the directive from the first gate path.
        banner = build_banner("mystery", {"artifact_exists": {"path": "out.md"}}, tmp_path)
        assert banner == "[agentalloy · mystery] MUST produce out.md before advancing"

    def test_unknown_phase_no_path_falls_back_to_satisfy_gate(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("mystery", {}, tmp_path)
        assert (
            banner == "[agentalloy · mystery] MUST satisfy the mystery exit gate before advancing"
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
        assert banner == "[agentalloy · mystery] MUST produce out.md before advancing"

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

    def test_build_contract_checkpoint_surfaced_then_cleared(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        self._write_design_docs(tmp_path, slug="feat", which={"approach", "tasks", "test-plan"})
        banner = build_banner("design", _design_gates(), tmp_path, slug="feat")
        assert "· 0 build contracts (need ≥1)" in banner
        # Satisfied once any build contract exists → the checkpoint line disappears.
        bc = tmp_path / ".agentalloy" / "contracts" / "build"
        bc.mkdir(parents=True)
        (bc / "01-task.md").write_text("x")
        banner2 = build_banner("design", _design_gates(), tmp_path, slug="feat")
        assert "build contracts" not in banner2

    def test_slug_resolved_in_directive(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _design_gates(), tmp_path, slug="calendar-web-ui")
        assert "docs/design/calendar-web-ui/" in banner
        assert "<slug>" not in banner

    def test_slug_left_literal_when_unknown(self, tmp_path: Path) -> None:
        from agentalloy.api.proxy_signal import build_banner

        banner = build_banner("design", _design_gates(), tmp_path)
        assert "<slug>" in banner


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

    def test_tool_less_request_leaves_banner_none(self, tmp_path: Path) -> None:
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
            # tools=None on a header-keyed session → non-carrier → no banner.
            # (A session header is what makes a tool-less turn a background ping;
            # tool-less fingerprint sessions — aider — DO carry.)
            result = asyncio.run(
                evaluate_signal(_req("work", tools=False), tmp_path, session_id="sess-bg")
            )
        assert result.banner is None

    def test_lifecycle_off_leaves_banner_none(self, tmp_path: Path) -> None:
        d = tmp_path / ".agentalloy"
        d.mkdir()
        (d / "phase").write_text("phase: spec\n")
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

    def test_throttled_to_default_cadence_of_five(self, tmp_path: Path) -> None:
        # Emits on the phase's first carrier turn (count 0) and again every 5th turn,
        # not on every turn.
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
        assert emitted == [True, False, False, False, False, True]

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

    def test_env_override_restores_every_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTALLOY_BANNER_TURN_CADENCE", "1")
        _set_phase(tmp_path, "spec")
        with (
            mock.patch(
                "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
                return_value=self._spec_skill(["spec"]),
            ),
            mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
        ):
            emitted = [
                asyncio.run(evaluate_signal(_req("x"), tmp_path, session_id=SESSION)).banner
                is not None
                for _ in range(3)
            ]
        assert emitted == [True, True, True]
