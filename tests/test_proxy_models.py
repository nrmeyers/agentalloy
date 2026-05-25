"""Tests for src/agentalloy/api/proxy_models.py.

Covers model construction, serialization (to_dict / JSON round-trip),
field defaults, and ProxyStreamChunk.set_delta behaviour.
"""

from __future__ import annotations

from agentalloy.api.proxy_models import (
    ProxyChoice,
    ProxyMessage,
    ProxyRequest,
    ProxyResponse,
    ProxyResponseUsage,
    ProxyStreamChunk,
    ProxyStreamDelta,
)

# ── ProxyMessage ──────────────────────────────────────────────────────


class TestProxyMessage:
    def test_minimal_construction(self):
        msg = ProxyMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_all_roles(self):
        for role in ("system", "user", "assistant"):
            msg = ProxyMessage(role=role, content=f"role={role}")
            assert msg.role == role

    def test_serialization(self):
        msg = ProxyMessage(role="system", content="You are helpful.")
        d = msg.model_dump()
        assert d == {"role": "system", "content": "You are helpful."}

    def test_json_roundtrip(self):
        msg = ProxyMessage(role="assistant", content="Done.")
        json_str = msg.model_dump_json()
        restored = ProxyMessage.model_validate_json(json_str)
        assert restored.role == "assistant"
        assert restored.content == "Done."


# ── ProxyRequest ──────────────────────────────────────────────────────


class TestProxyRequest:
    def test_minimal(self):
        req = ProxyRequest(
            model="gpt-4",
            messages=[ProxyMessage(role="user", content="hi")],
        )
        assert req.model == "gpt-4"
        assert len(req.messages) == 1
        assert req.stream is False

    def test_all_optional_fields(self):
        req = ProxyRequest(
            model="gpt-4",
            messages=[ProxyMessage(role="user", content="hi")],
            stream=True,
            temperature=0.7,
            max_tokens=1024,
            top_p=0.9,
            presence_penalty=0.1,
            frequency_penalty=0.2,
            n=2,
            user="user-123",
            metadata={"cwd": "/tmp/project"},
        )
        assert req.stream is True
        assert req.temperature == 0.7
        assert req.max_tokens == 1024
        assert req.user == "user-123"
        assert req.metadata == {"cwd": "/tmp/project"}

    def test_defaults_are_falsey(self):
        req = ProxyRequest(
            model="test",
            messages=[ProxyMessage(role="user", content="x")],
        )
        assert req.stream is False
        assert req.temperature is None
        assert req.max_tokens is None
        assert req.user is None
        assert req.metadata is None

    def test_serialization(self):
        req = ProxyRequest(
            model="test",
            messages=[ProxyMessage(role="user", content="hi")],
        )
        d = req.model_dump(exclude_none=True)
        assert d["model"] == "test"
        assert len(d["messages"]) == 1

    def test_json_roundtrip(self):
        req = ProxyRequest(
            model="test",
            messages=[ProxyMessage(role="user", content="hello")],
            temperature=0.5,
        )
        json_str = req.model_dump_json()
        restored = ProxyRequest.model_validate_json(json_str)
        assert restored.model == "test"
        assert restored.temperature == 0.5


# ── ProxyChoice ───────────────────────────────────────────────────────


class TestProxyChoice:
    def test_minimal(self):
        choice = ProxyChoice(
            index=0,
            message=ProxyMessage(role="assistant", content="ok"),
        )
        assert choice.index == 0
        assert choice.finish_reason is None

    def test_with_finish_reason(self):
        choice = ProxyChoice(
            index=0,
            message=ProxyMessage(role="assistant", content="ok"),
            finish_reason="stop",
        )
        assert choice.finish_reason == "stop"

    def test_finish_reason_stop(self) -> None:
        choice = ProxyChoice(
            index=0,
            message=ProxyMessage(role="assistant", content="x"),
            finish_reason="stop",
        )
        assert choice.finish_reason == "stop"

    def test_finish_reason_length(self) -> None:
        choice = ProxyChoice(
            index=0,
            message=ProxyMessage(role="assistant", content="x"),
            finish_reason="length",
        )
        assert choice.finish_reason == "length"

    def test_finish_reason_content_filter(self) -> None:
        choice = ProxyChoice(
            index=0,
            message=ProxyMessage(role="assistant", content="x"),
            finish_reason="content_filter",
        )
        assert choice.finish_reason == "content_filter"


# ── ProxyStreamDelta ──────────────────────────────────────────────────


class TestProxyStreamDelta:
    def test_empty_delta(self):
        delta = ProxyStreamDelta()
        assert delta.role is None
        assert delta.content is None

    def test_full_delta(self):
        delta = ProxyStreamDelta(role="assistant", content="partial")
        assert delta.role == "assistant"
        assert delta.content == "partial"

    def test_model_dump_excludes_none(self):
        delta = ProxyStreamDelta(content="hello")
        d = delta.model_dump(exclude_none=True)
        assert d == {"content": "hello"}
        assert "role" not in d


# ── ProxyStreamChunk ──────────────────────────────────────────────────


class TestProxyStreamChunk:
    def test_minimal(self):
        chunk = ProxyStreamChunk(
            id="chunk-1",
            created=1234567890,
            model="test",
            choices=[],
        )
        assert chunk.object == "chat.completion.chunk"

    def test_set_delta_creates_entry(self):
        chunk = ProxyStreamChunk(
            id="chunk-1",
            created=1234567890,
            model="test",
            choices=[],
        )
        delta = ProxyStreamDelta(role="assistant", content="hi")
        chunk.set_delta(0, delta)
        assert len(chunk.choices) == 1
        assert chunk.choices[0]["delta"] == {"role": "assistant", "content": "hi"}

    def test_set_delta_with_finish_reason(self):
        chunk = ProxyStreamChunk(
            id="chunk-1",
            created=1234567890,
            model="test",
            choices=[],
        )
        delta = ProxyStreamDelta(content="done")
        chunk.set_delta(0, delta, finish_reason="stop")
        assert chunk.choices[0]["finish_reason"] == "stop"

    def test_set_delta_updates_existing(self):
        chunk = ProxyStreamChunk(
            id="chunk-1",
            created=1234567890,
            model="test",
            choices=[{"index": 0, "delta": {"content": "first"}}],
        )
        delta = ProxyStreamDelta(content="second")
        chunk.set_delta(0, delta)
        assert chunk.choices[0]["delta"] == {"content": "second"}

    def test_set_delta_multiple_indices(self):
        chunk = ProxyStreamChunk(
            id="chunk-1",
            created=1234567890,
            model="test",
            choices=[],
        )
        chunk.set_delta(0, ProxyStreamDelta(content="a"))
        chunk.set_delta(1, ProxyStreamDelta(content="b"))
        assert len(chunk.choices) == 2
        assert chunk.choices[0]["delta"] == {"content": "a"}
        assert chunk.choices[1]["delta"] == {"content": "b"}


# ── ProxyResponseUsage ────────────────────────────────────────────────


class TestProxyResponseUsage:
    def test_construction(self):
        usage = ProxyResponseUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        assert usage.prompt_tokens == 10
        assert usage.total_tokens == 30

    def test_serialization(self):
        usage = ProxyResponseUsage(prompt_tokens=5, completion_tokens=0, total_tokens=5)
        d = usage.model_dump()
        assert d == {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}


# ── ProxyResponse ─────────────────────────────────────────────────────


class TestProxyResponse:
    def test_minimal(self):
        resp = ProxyResponse(
            id="resp-1",
            created=1234567890,
            model="test",
            choices=[
                ProxyChoice(
                    index=0,
                    message=ProxyMessage(role="assistant", content="ok"),
                )
            ],
        )
        assert resp.object == "chat.completion"
        assert resp.usage is None

    def test_with_usage(self):
        usage = ProxyResponseUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        resp = ProxyResponse(
            id="resp-1",
            created=1234567890,
            model="test",
            choices=[
                ProxyChoice(
                    index=0,
                    message=ProxyMessage(role="assistant", content="ok"),
                )
            ],
            usage=usage,
        )
        assert resp.usage is not None
        assert resp.usage.total_tokens == 15

    def test_serialization(self):
        resp = ProxyResponse(
            id="resp-1",
            created=1234567890,
            model="test",
            choices=[
                ProxyChoice(
                    index=0,
                    message=ProxyMessage(role="assistant", content="ok"),
                )
            ],
        )
        d = resp.model_dump(exclude_none=True)
        assert d["id"] == "resp-1"
        assert d["object"] == "chat.completion"
        assert len(d["choices"]) == 1
