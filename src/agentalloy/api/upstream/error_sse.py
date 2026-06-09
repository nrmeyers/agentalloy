"""Error SSE helpers for streaming endpoints.

When an upstream httpx call fails during streaming, these helpers produce
well-formed SSE error events so the client receives structured error
information instead of a raw 500.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plain-text SSE error (used by the OpenAI-compatible proxy)
# ---------------------------------------------------------------------------


def error_sse_plain(message: str, status_code: int | None = None) -> str:
    """Return a plain SSE error chunk.

    Format::

        data: {"error": "message"}\\n\\n
    """
    payload: dict[str, Any] = {"error": message}
    if status_code is not None:
        payload["status_code"] = status_code
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Structured SSE error (used by the Anthropic proxy)
# ---------------------------------------------------------------------------


def error_sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Return a structured SSE error event.

    Format::

        event: <event_type>\\ndata: <json>\\n\\n
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def make_http_error_sse(status_code: int, detail: str, model: str | None = None) -> list[str]:
    """Build a sequence of SSE events for an HTTP error from upstream.

    Returns events that can be yielded from a streaming generator.  The
    sequence starts with a ``message_start`` event so the client has a
    valid message id, followed by an ``error`` event with the actual
    problem description.
    """
    msg_id = f"msg_error_{status_code}"
    events: list[str] = []

    # message_start (so client has a valid message structure)
    msg_start = {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model or "unknown",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    events.append(error_sse_event("message_start", msg_start))

    # error event
    error_data = {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": f"Upstream returned HTTP {status_code}: {detail}",
            "status_code": str(status_code),
        },
    }
    events.append(error_sse_event("error", error_data))

    # message_stop to close the stream
    events.append("event: message_stop\ndata: {}\n\n")

    return events


def make_upstream_http_error_sse(status_code: int) -> list[str]:
    """SSE events for a generic upstream HTTP error.

    Used when the status code is known but no body text is available yet.
    """
    return make_http_error_sse(status_code, "Upstream server error")


def make_network_error_sse(exc: Exception, model: str | None = None) -> list[str]:
    """SSE events for a network / connection error from upstream.

    Catches ``httpx`` exceptions (ConnectError, TimeoutException,
    HTTPError, etc.) and emits structured error events.
    """
    error_type = type(exc).__name__
    detail = str(exc)
    logger.warning("Upstream network error (%s): %s", error_type, detail)
    return make_http_error_sse(503, f"Upstream connection error ({error_type}): {detail}", model)
