"""Tests for src/agentalloy/api/proxy_telemetry.py.

Covers CompositionTrace construction for proxy requests, field accuracy,
and soft-fail behaviour when the vector store raises.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from agentalloy.api.proxy_telemetry import write_proxy_trace


class TestWriteProxyTrace:
    """Unit tests for write_proxy_trace."""

    def test_composed_request_creates_trace(self):
        """A composed proxy request writes a trace with status=proxy_composed."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="implement feature X",
            status="proxy_composed",
            source_skill_ids=["skill-1", "skill-2"],
            total_latency_ms=150,
        )
        mock_store.record_composition_trace.assert_called_once()
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_composed"
        assert trace.phase == "build"
        assert trace.event_type == "proxy_request"
        assert trace.source_skill_ids == ["skill-1", "skill-2"]
        assert trace.total_latency_ms == 150

    def test_passthrough_request_creates_trace(self):
        """A passthrough proxy request writes a trace with status=proxy_passthrough."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="design",
            task_prompt="review code",
            status="proxy_passthrough",
        )
        mock_store.record_composition_trace.assert_called_once()
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.status == "proxy_passthrough"

    def test_trace_has_required_fields(self):
        """All required fields are present and correctly typed."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="qa",
            task_prompt="run tests",
            status="proxy_composed",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert isinstance(trace.trace_id, str) and len(trace.trace_id) == 36  # UUID
        assert isinstance(trace.request_ts, int)
        assert trace.request_ts > 0
        assert isinstance(trace.phase, str)
        assert isinstance(trace.task_prompt, str)
        assert isinstance(trace.status, str)
        assert isinstance(trace.event_type, str)

    def test_trace_request_ts_is_current(self):
        """request_ts should be close to the current time (within 5 seconds)."""
        mock_store = MagicMock()
        before = int(time.time() * 1000)
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="test",
            status="proxy_composed",
        )
        after = int(time.time() * 1000)
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert before <= trace.request_ts <= after

    def test_task_prompt_truncated_to_500_chars(self):
        """task_prompt is truncated to 500 characters."""
        mock_store = MagicMock()
        long_prompt = "x" * 1000
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt=long_prompt,
            status="proxy_composed",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert len(trace.task_prompt) == 500

    def test_signal_layer_fields(self):
        """Signal-layer fields (gates, pre_filter, qwen_calls) are passed through."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="deploy",
            status="proxy_composed",
            pre_filter_matched="prompt_keyword",
            gates_met=["test_passed", "lint_clean"],
            gates_unmet=["integration_passed"],
            qwen_calls=2,
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.pre_filter_matched == "prompt_keyword"
        assert trace.gates_met == ["test_passed", "lint_clean"]
        assert trace.gates_unmet == ["integration_passed"]
        assert trace.qwen_calls == 2

    def test_phase_gate_embed_failed_field(self):
        """phase_gate_embed_failed flows through to the trace when set."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="design",
            task_prompt="looks done",
            status="proxy_passthrough",
            phase_gate_embed_failed=True,
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.phase_gate_embed_failed is True

    def test_phase_gate_embed_failed_defaults_false(self):
        """phase_gate_embed_failed defaults to False when not passed."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="design",
            task_prompt="plan",
            status="proxy_passthrough",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.phase_gate_embed_failed is False

    def test_error_code_field(self):
        """error_code is written when present."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="broken",
            status="proxy_passthrough",
            error_code="upstream_timeout",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.error_code == "upstream_timeout"

    def test_none_fields_become_empty_lists(self):
        """Optional list fields default to empty lists when not provided."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="design",
            task_prompt="plan",
            status="proxy_passthrough",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.gates_met == []
        assert trace.gates_unmet == []
        assert trace.source_skill_ids == []
        assert trace.qwen_calls == 0

    def test_custom_event_type(self):
        """event_type can be overridden from the default."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="test",
            status="proxy_composed",
            event_type="proxy_stream",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.event_type == "proxy_stream"

    def test_soft_fail_on_vector_store_error(self):
        """If the vector store raises, the function swallows the exception."""
        mock_store = MagicMock()
        mock_store.record_composition_trace.side_effect = RuntimeError("db locked")
        # Should not raise
        write_proxy_trace(
            mock_store,
            phase="build",
            task_prompt="test",
            status="proxy_composed",
        )
        # Still attempted to write
        mock_store.record_composition_trace.assert_called_once()

    def test_soft_fail_on_import_error(self) -> None:
        """If the import chain fails, the function swallows the exception."""

        # This test verifies the try/except wrapping is effective.
        # We can't easily break imports at runtime, but we can verify
        # that a completely broken vector_store doesn't propagate errors.
        class BrokenStore:
            def record_composition_trace(self, trace: Any) -> None:
                raise ImportError("module not found")

        # Should not raise
        write_proxy_trace(
            BrokenStore(),  # type: ignore[arg-type]
            phase="build",
            task_prompt="test",
            status="proxy_composed",
        )

    def test_unspecified_phase(self):
        """Phase defaults to 'unspecified' when no phase file exists."""
        mock_store = MagicMock()
        write_proxy_trace(
            mock_store,
            phase="unspecified",
            task_prompt="no phase",
            status="proxy_passthrough",
        )
        trace = mock_store.record_composition_trace.call_args[0][0]
        assert trace.phase == "unspecified"
