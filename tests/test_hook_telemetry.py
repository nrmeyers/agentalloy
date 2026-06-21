"""Tests for src/agentalloy/api/hook_telemetry.py.

Covers CompositionTrace construction for each hook event, the column reuse
(event_type/status/correlation_id), soft-fail, and a real-store write→aggregate
roundtrip proving the new rows are counted by ``aggregate_hook_coverage`` without
inflating ``aggregate_savings`` (compose-only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from agentalloy.api.hook_telemetry import write_hook_trace
from agentalloy.storage.vector_store import open_or_create


class TestWriteHookTrace:
    def test_session_start_maps_to_session_intake(self) -> None:
        store = MagicMock()
        write_hook_trace(
            store,
            hook_event="session_start",
            phase="intake",
            status="intake",
            workflow_skill_ids=["sdd-intake"],
        )
        trace = store.record_composition_trace.call_args[0][0]
        assert trace.event_type == "session_intake"
        assert trace.status == "intake"
        assert trace.workflow_skill_ids == ["sdd-intake"]

    def test_prompt_submit_records_decision_and_cache_status(self) -> None:
        store = MagicMock()
        write_hook_trace(
            store,
            hook_event="user_prompt_submit",
            phase="build",
            status="no_compose",
            task_prompt="just chatting",
            correlation_id="cached",
        )
        trace = store.record_composition_trace.call_args[0][0]
        assert trace.event_type == "prompt_submit"
        assert trace.status == "no_compose"
        assert trace.correlation_id == "cached"
        assert trace.task_prompt == "just chatting"

    def test_pre_tool_use_records_system_skills_and_tool(self) -> None:
        store = MagicMock()
        write_hook_trace(
            store,
            hook_event="pre_tool_use",
            phase="build",
            status="system_skill",
            system_skill_ids=["sys-vcs-forge", "sys-ci"],
            correlation_id="Edit",
            total_latency_ms=7,
        )
        trace = store.record_composition_trace.call_args[0][0]
        assert trace.event_type == "system_skill_applied"
        assert trace.system_skill_ids == ["sys-vcs-forge", "sys-ci"]
        assert trace.correlation_id == "Edit"
        assert trace.total_latency_ms == 7

    def test_none_vector_store_is_noop(self) -> None:
        # Runtime not loaded — must not raise.
        write_hook_trace(None, hook_event="user_prompt_submit", phase="build", status="no_compose")

    def test_empty_phase_defaults_to_unspecified(self) -> None:
        store = MagicMock()
        write_hook_trace(store, hook_event="user_prompt_submit", phase="", status="no_compose")
        trace = store.record_composition_trace.call_args[0][0]
        assert trace.phase == "unspecified"

    def test_task_prompt_truncated_to_500(self) -> None:
        store = MagicMock()
        write_hook_trace(
            store,
            hook_event="user_prompt_submit",
            phase="build",
            status="composed",
            task_prompt="x" * 1000,
        )
        trace = store.record_composition_trace.call_args[0][0]
        assert len(trace.task_prompt) == 500

    def test_list_fields_default_empty(self) -> None:
        store = MagicMock()
        write_hook_trace(store, hook_event="user_prompt_submit", phase="build", status="composed")
        trace = store.record_composition_trace.call_args[0][0]
        assert trace.workflow_skill_ids == []
        assert trace.system_skill_ids == []
        assert trace.selected_fragment_ids == []

    def test_soft_fail_on_store_error(self) -> None:
        store = MagicMock()
        store.record_composition_trace.side_effect = RuntimeError("db locked")
        # Must not propagate — telemetry never blocks a hook.
        write_hook_trace(store, hook_event="pre_tool_use", phase="qa", status="system_skill")
        store.record_composition_trace.assert_called_once()

    def test_soft_fail_on_broken_store(self) -> None:
        class Broken:
            def record_composition_trace(self, trace: Any) -> None:
                raise ImportError("boom")

        write_hook_trace(
            Broken(),  # type: ignore[arg-type]
            hook_event="session_start",
            phase="intake",
            status="intake",
        )


class TestHookCoverageRoundtrip:
    """Real DuckDB store: written hook events are counted by coverage, and the
    compose-only savings total is unaffected by the new statuses."""

    def test_write_then_aggregate(self, tmp_path: Path) -> None:
        store = open_or_create(tmp_path / "trace.duck")
        try:
            write_hook_trace(store, hook_event="session_start", phase="intake", status="intake")
            write_hook_trace(
                store,
                hook_event="user_prompt_submit",
                phase="build",
                status="composed",
                task_prompt="add feature",
            )
            write_hook_trace(
                store,
                hook_event="user_prompt_submit",
                phase="build",
                status="no_compose",
                task_prompt="thanks!",
            )
            write_hook_trace(
                store,
                hook_event="pre_tool_use",
                phase="build",
                status="system_skill",
                system_skill_ids=["sys-ci"],
            )

            cov = store.aggregate_hook_coverage()
            assert cov["prompts_total"] == 2
            assert cov["prompts_composed"] == 1
            assert cov["prompts_no_compose"] == 1
            assert cov["system_skill_pulls"] == 1
            assert cov["intake_injections"] == 1

            # The hook statuses are distinct from 'compose' — savings unaffected.
            assert store.aggregate_savings()["total_composes"] == 0
        finally:
            store.close()


class TestContractComposeDiscriminator:
    """PostToolUse-driven composes carry an origin tag (``requesting_agent`` ->
    trace ``correlation_id``) so they're attributable, while still counting as a
    real compose in savings."""

    def test_origin_maps_to_correlation_id_and_coverage(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from agentalloy.telemetry import DuckDBTelemetryWriter, TelemetryRecord

        store = open_or_create(tmp_path / "t.duck")
        try:
            DuckDBTelemetryWriter(store).write(
                TelemetryRecord(
                    composition_id="c1",
                    timestamp=datetime.now(UTC),
                    phase="build",
                    task_prompt="implement",
                    result_type="compose",
                    requesting_agent="post_tool_use",
                )
            )
            assert store.aggregate_hook_coverage()["contract_composes"] == 1
            # It IS a compose — still counted in the token-savings total.
            assert store.aggregate_savings()["total_composes"] == 1
        finally:
            store.close()

    def test_direct_compose_has_no_origin(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from agentalloy.telemetry import DuckDBTelemetryWriter, TelemetryRecord

        store = open_or_create(tmp_path / "t.duck")
        try:
            DuckDBTelemetryWriter(store).write(
                TelemetryRecord(
                    composition_id="c2",
                    timestamp=datetime.now(UTC),
                    phase="build",
                    task_prompt="direct",
                    result_type="compose",
                )  # no requesting_agent
            )
            # A direct /compose is not attributed to the contract hook.
            assert store.aggregate_hook_coverage()["contract_composes"] == 0
            assert store.aggregate_savings()["total_composes"] == 1
        finally:
            store.close()


class TestRenderCoverage:
    """The `agentalloy telemetry coverage` human renderer formats without error."""

    def test_renders_counts_and_per_phase(self, capsys: Any) -> None:
        from agentalloy.install.subcommands.telemetry import _render_coverage

        _render_coverage(
            {
                "prompts_total": 5,
                "prompts_composed": 3,
                "prompts_no_compose": 2,
                "system_skill_pulls": 4,
                "intake_injections": 1,
                "by_event": [{"event_type": "prompt_submit", "status": "composed", "count": 3}],
                "per_phase_prompts": [{"phase": "build", "prompts": 5, "composed": 3}],
            }
        )
        out = capsys.readouterr().out
        assert "Hook Coverage" in out
        assert "build" in out

    def test_empty_is_graceful(self, capsys: Any) -> None:
        from agentalloy.install.subcommands.telemetry import _render_coverage

        _render_coverage(
            {
                "prompts_total": 0,
                "prompts_composed": 0,
                "prompts_no_compose": 0,
                "system_skill_pulls": 0,
                "intake_injections": 0,
                "by_event": [],
                "per_phase_prompts": [],
            }
        )
        assert "No hook activity" in capsys.readouterr().out
