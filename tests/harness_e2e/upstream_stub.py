"""A real listening stub upstream for the harness e2e matrix.

Serves both proxy upstream surfaces on a real socket so actual harness
binaries (and the proxy's own httpx client) can talk to it:

- ``POST /v1/chat/completions`` — OpenAI Chat Completions (JSON + SSE)
- ``POST /v1/messages`` — Anthropic Messages (JSON + SSE)

Every request body is captured (with its path) for assertions. Responses are
canned: a short assistant turn with no tool calls, so headless harness runs
terminate after one round trip.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CANNED_TEXT = "READY"


@dataclass
class CapturedRequest:
    path: str
    payload: dict[str, Any]


@dataclass
class UpstreamStub:
    """Handle onto the running stub: port + captured request log."""

    port: int
    captured: list[CapturedRequest] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def user_texts(requests: list[CapturedRequest]) -> list[str]:
    """Last-user-message text per captured request (all three API shapes)."""
    texts: list[str] = []
    for req in requests:
        raw_input = req.payload.get("input")
        if isinstance(raw_input, str):
            texts.append(raw_input)
            continue
        if isinstance(raw_input, list):
            # Responses API: message items with input_text blocks.
            user_items = [
                i
                for i in raw_input
                if isinstance(i, dict) and i.get("type") == "message" and i.get("role") == "user"
            ]
            if user_items:
                content = user_items[-1].get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    texts.append(
                        "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
                    )
                continue
        messages = req.payload.get("messages", [])
        user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        if not user_msgs:
            continue
        content = user_msgs[-1].get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            # Anthropic content blocks: [{"type": "text", "text": ...}, ...]
            texts.append("\n".join(b.get("text", "") for b in content if isinstance(b, dict)))
    return texts


def _openai_json(model: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": CANNED_TEXT},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _openai_sse(model: str) -> list[str]:
    chunk = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": CANNED_TEXT},
                "finish_reason": None,
            }
        ],
    }
    done = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return [
        f"data: {json.dumps(chunk)}\n\n",
        f"data: {json.dumps(done)}\n\n",
        "data: [DONE]\n\n",
    ]


def _anthropic_json(model: str) -> dict[str, Any]:
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": CANNED_TEXT}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _anthropic_sse(model: str) -> list[str]:
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stub",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": CANNED_TEXT},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events]


def _responses_message_item() -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_stub",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": CANNED_TEXT, "annotations": []}],
    }


def _responses_body(model: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp_stub",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": model,
        "output": [_responses_message_item()] if status == "completed" else [],
        "error": None,
        "incomplete_details": None,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _responses_sse(model: str) -> list[str]:
    item = _responses_message_item()
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "response.created",
            {"type": "response.created", "response": _responses_body(model, "in_progress")},
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            },
        ),
        (
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": "msg_stub",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": "msg_stub",
                "output_index": 0,
                "content_index": 0,
                "delta": CANNED_TEXT,
            },
        ),
        (
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": "msg_stub",
                "output_index": 0,
                "content_index": 0,
                "text": CANNED_TEXT,
            },
        ),
        (
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": "msg_stub",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": CANNED_TEXT, "annotations": []},
            },
        ),
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ),
        ("response.completed", {"type": "response.completed", "response": _responses_body(model)}),
    ]
    frames: list[str] = []
    for seq, (name, data) in enumerate(events):
        data["sequence_number"] = seq
        frames.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")
    return frames


def start_upstream_stub() -> UpstreamStub:
    """Start the stub on an OS-assigned free port; caller must ``stop()`` it."""
    stub = UpstreamStub(port=0)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep pytest output clean

        def _read_payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            return parsed if isinstance(parsed, dict) else {}

        def _send_json(self, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_sse(self, frames: list[str]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for frame in frames:
                self.wfile.write(frame.encode())
                self.wfile.flush()

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            # Some SDKs probe endpoints; answer 200 on anything GET.
            self._send_json({"ok": True})

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            payload = self._read_payload()
            with lock:
                stub.captured.append(CapturedRequest(path=self.path, payload=payload))
            model = str(payload.get("model", "stub-model"))
            stream = bool(payload.get("stream", False))
            if self.path.endswith("/messages"):
                if stream:
                    self._send_sse(_anthropic_sse(model))
                else:
                    self._send_json(_anthropic_json(model))
            elif self.path.endswith("/responses"):
                if stream:
                    self._send_sse(_responses_sse(model))
                else:
                    self._send_json(_responses_body(model))
            else:
                # Default to the OpenAI chat-completions shape for anything else.
                if stream:
                    self._send_sse(_openai_sse(model))
                else:
                    self._send_json(_openai_json(model))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.port = server.server_address[1]
    stub._server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub._thread = thread
    return stub
