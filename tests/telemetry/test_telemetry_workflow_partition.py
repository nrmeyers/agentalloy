"""Phase D.5: tests for workflow_skill_ids partition in telemetry."""

from __future__ import annotations

from datetime import UTC, datetime

from agentalloy.storage.protocols import CompositionTrace
from agentalloy.telemetry.writer import TelemetryRecord


class TestTelemetryRecordWorkflowFields:
    def test_workflow_skill_ids_defaults_none(self) -> None:
        rec = TelemetryRecord(
            composition_id="x",
            timestamp=datetime.now(UTC),
            phase="build",
            task_prompt="test",
            result_type="compose",
        )
        assert rec.workflow_skill_ids is None
        assert rec.prompt_version is None

    def test_workflow_skill_ids_can_be_set(self) -> None:
        rec = TelemetryRecord(
            composition_id="x",
            timestamp=datetime.now(UTC),
            phase="build",
            task_prompt="test",
            result_type="compose",
            workflow_skill_ids=["sdd-spec", "sdd-design"],
        )
        assert rec.workflow_skill_ids == ["sdd-spec", "sdd-design"]


class TestCompositionTraceWorkflowFields:
    def test_workflow_skill_ids_defaults_empty(self) -> None:
        trace = CompositionTrace(
            trace_id="t1",
            request_ts=0,
            phase="build",
            task_prompt="test",
            status="compose",
        )
        assert trace.workflow_skill_ids == []
        assert trace.prompt_version is None

    def test_workflow_skill_ids_can_be_set(self) -> None:
        trace = CompositionTrace(
            trace_id="t1",
            request_ts=0,
            phase="build",
            task_prompt="test",
            status="compose",
            workflow_skill_ids=["sdd-plan"],
        )
        assert trace.workflow_skill_ids == ["sdd-plan"]


class TestWorkflowSkillIdPartition:
    """Tests the partition logic itself — independent of DB or LLM."""

    def test_partition_extracts_workflow_skill_ids(self) -> None:
        """Simulate what compose.py does: filter fragments by skill_class."""
        from dataclasses import dataclass

        @dataclass
        class FakeFragment:
            skill_id: str
            fragment_id: str
            skill_class: str

        candidates = [
            FakeFragment("sdd-spec", "f1", "workflow"),
            FakeFragment("python-async", "f2", "domain"),
            FakeFragment("sdd-design", "f3", "workflow"),
            FakeFragment("sdd-design", "f4", "workflow"),  # duplicate skill_id
        ]
        workflow_skill_ids = list(
            dict.fromkeys(f.skill_id for f in candidates if f.skill_class == "workflow")
        )
        assert workflow_skill_ids == ["sdd-spec", "sdd-design"]  # deduped, order preserved
        assert "python-async" not in workflow_skill_ids

    def test_partition_empty_when_no_workflow_fragments(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class FakeFragment:
            skill_id: str
            skill_class: str

        candidates = [
            FakeFragment("python-async", "domain"),
            FakeFragment("typescript-generics", "domain"),
        ]
        workflow_skill_ids = list(
            dict.fromkeys(f.skill_id for f in candidates if f.skill_class == "workflow")
        )
        assert workflow_skill_ids == []
