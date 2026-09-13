"""Upstream streaming utilities.

Provides helpers for safely forwarding upstream LLM requests (both streaming
and non-streaming) and for emitting structured error SSE events when upstream
calls fail.
"""

from agentalloy.api.upstream.error_sse import (
    error_sse_event,
    error_sse_plain,
)

__all__ = ["error_sse_event", "error_sse_plain"]
