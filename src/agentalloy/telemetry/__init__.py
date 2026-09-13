"""Telemetry writer — persists composition and retrieval traces.

Per v5.3, traces land in DuckDB ``composition_traces`` (same
``skills.duck`` file as fragment_embeddings). Writes are inline before
response; failures log but never propagate.

Phase-event tracing (``PhaseTelemetryWriter``) writes high-level
lifecycle events into a dedicated ``phase_events`` table.
"""

from __future__ import annotations

from agentalloy.telemetry.phase_writer import (
    PhaseEvent,
    PhaseTelemetryWriter,
)
from agentalloy.telemetry.writer import (
    DuckDBTelemetryWriter,
    NullTelemetryWriter,
    TelemetryRecord,
    TelemetryWriter,
)

__all__ = [
    "DuckDBTelemetryWriter",
    "NullTelemetryWriter",
    "PhaseEvent",
    "PhaseTelemetryWriter",
    "TelemetryRecord",
    "TelemetryWriter",
]
