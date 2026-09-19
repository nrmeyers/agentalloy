"""Interpreter tests — verify tool-calling loop (T5)."""

import tempfile
from pathlib import Path
from typing import Any

from agentalloy.interpreter import Interpreter
from agentalloy.state_store import StateStore
from agentalloy.tools import ALL_TOOLS


class MockMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockChoice:
    def __init__(self, message: MockMessage):
        self.message = message


class MockResponse:
    def __init__(self, message: MockMessage):
        self.choices = [MockChoice(message)]


class MockToolCall:
    def __init__(self, id: str, name: str, args: str):
        self.id = id
        self.function = MockFunction(name, args)

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class MockFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class MockClient:
    """Mock OpenAI client that returns scripted responses."""

    def __init__(self, responses: list[MockResponse]):
        self.responses = responses
        self.call_count = 0

    @property
    def chat(self) -> Any:
        return self

    @property
    def completions(self) -> Any:
        return self

    def create(self, **kwargs: Any) -> MockResponse:
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def test_interpreter_answer_no_tools() -> None:
    """Model returns final answer without tool calls."""
    client = MockClient(
        [
            MockResponse(MockMessage(content="The answer is 42")),
        ]
    )
    interpreter = Interpreter(client)
    result = interpreter.run([{"role": "user", "content": "What is the answer?"}])
    assert result.stop_reason == "answer"
    assert result.answer == "The answer is 42"
    assert result.steps == 0
    assert len(result.tool_calls) == 0


def test_interpreter_step_budget() -> None:
    """Model keeps calling tools past max_steps."""
    client = MockClient(
        [
            MockResponse(
                MockMessage(
                    tool_calls=[
                        MockToolCall("1", "code_search", '{"query": "test"}'),
                    ]
                )
            ),
            MockResponse(
                MockMessage(
                    tool_calls=[
                        MockToolCall("2", "code_search", '{"query": "test2"}'),
                    ]
                )
            ),
            MockResponse(
                MockMessage(
                    tool_calls=[
                        MockToolCall("3", "code_search", '{"query": "test3"}'),
                    ]
                )
            ),
        ]
    )
    interpreter = Interpreter(client, max_steps=2, hard_cap=3)
    result = interpreter.run([{"role": "user", "content": "Search"}])
    assert result.stop_reason == "step_budget"
    assert result.steps == 2


def test_interpreter_duplicate() -> None:
    """Model calls same tool with same args twice."""
    client = MockClient(
        [
            MockResponse(
                MockMessage(
                    tool_calls=[
                        MockToolCall("1", "code_search", '{"query": "test"}'),
                    ]
                )
            ),
            MockResponse(
                MockMessage(
                    tool_calls=[
                        MockToolCall("2", "code_search", '{"query": "test"}'),
                    ]
                )
            ),
        ]
    )
    interpreter = Interpreter(client)
    result = interpreter.run([{"role": "user", "content": "Search"}])
    assert result.stop_reason == "duplicate"


def test_interpreter_validation_failed() -> None:
    """Invalid args → the error is fed back once, a second invalid call
    ends the run with validation_failed (the documented single retry)."""
    invalid = MockResponse(
        MockMessage(
            tool_calls=[
                MockToolCall("1", "contract_add", '{"slug": "test"}'),  # missing required
            ]
        )
    )
    client = MockClient([invalid, invalid])
    interpreter = Interpreter(client)
    result = interpreter.run([{"role": "user", "content": "Add contract"}])
    assert result.stop_reason == "validation_failed"
    # The retry round saw the validation error as a tool message.
    assert any(
        m.get("role") == "tool" and "validation error" in str(m.get("content"))
        for m in result.messages
    )


def test_tools_count() -> None:
    """Verify 14 tools (11 read + 3 state) per R2."""
    assert len(ALL_TOOLS) == 14


class MockStreamDelta:
    def __init__(self, content: str | None = None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or None


class MockStreamChoice:
    def __init__(self, delta: MockStreamDelta):
        self.delta = delta


class MockStreamChunk:
    def __init__(self, delta: MockStreamDelta):
        self.choices = [MockStreamChoice(delta)]


class MockStream:
    """Upstream stream with the context-manager protocol (like the OpenAI client)."""

    def __init__(self, chunks: list[MockStreamChunk]):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def __enter__(self) -> "MockStream":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closed = True


class MockStreamingClient:
    """Mock OpenAI client whose create(stream=True) returns a MockStream."""

    def __init__(self, chunks: list[MockStreamChunk]):
        self.stream = MockStream(chunks)

    @property
    def chat(self) -> Any:
        return self

    @property
    def completions(self) -> Any:
        return self

    def create(self, **kwargs: Any) -> MockStream:
        return self.stream


def test_run_streaming_abandoned_generator_closes_upstream_stream() -> None:
    """Abandoning the event generator (client disconnect) must close the
    upstream LFM stream — regression test for the CLOSE-WAIT socket leak."""
    chunks = [
        MockStreamChunk(MockStreamDelta(content="Hel")),
        MockStreamChunk(MockStreamDelta(content="lo")),
    ]
    client = MockStreamingClient(chunks)
    interpreter = Interpreter(client)
    gen = interpreter.run_streaming([{"role": "user", "content": "hi"}])

    first = next(gen)
    assert first["type"] == "token"
    assert first["content"] == "Hel"

    # Consumer goes away without draining (harness disconnect).
    gen.close()

    assert client.stream.closed is True


def test_run_streaming_normal_completion_closes_upstream_stream() -> None:
    """Normal completion also closes the upstream stream."""
    chunks = [MockStreamChunk(MockStreamDelta(content="Hi there"))]
    client = MockStreamingClient(chunks)
    interpreter = Interpreter(client)

    events = list(interpreter.run_streaming([{"role": "user", "content": "hi"}]))

    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "Hi there"
    assert client.stream.closed is True


def test_interpreter_state_persists_to_duckdb() -> None:
    """Interpreter state executors persist to real DuckDB store (TC-7.5)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # Mock client emits contract_add + artifact_record, then final answer
        client = MockClient(
            [
                MockResponse(
                    MockMessage(
                        tool_calls=[
                            MockToolCall(
                                "1",
                                "contract_add",
                                '{"slug": "sdd-build", "domain_tags": ["rust"], "touches": "core"}',
                            ),
                        ]
                    )
                ),
                MockResponse(
                    MockMessage(
                        tool_calls=[
                            MockToolCall(
                                "2",
                                "artifact_record",
                                '{"phase": "build", "name": "build-log", "body": "built ok"}',
                            ),
                        ]
                    )
                ),
                MockResponse(MockMessage(content="Done")),
            ]
        )

        interpreter = Interpreter(client, max_steps=3, state_store=store)
        result = interpreter.run([{"role": "user", "content": "Build it"}])

        assert result.stop_reason == "answer"
        assert len(result.tool_calls) == 2

        # Verify contract persisted in DuckDB
        contract = store.get_contract("sdd-build")
        assert contract is not None
        assert contract["slug"] == "sdd-build"
        assert contract["domain_tags"] == ["rust"]
        assert contract["touches"] == "core"

        # Verify artifact persisted in DuckDB
        body = store.get_artifact("build", "build-log")
        assert body == "built ok"

        store.close()


def test_interpreter_phase_advance_rejected_without_exit_artifact() -> None:
    """AC-9: phase_advance rejected when no exit artifact recorded (gate enforcement)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # phase_advance without exit artifact → rejected
        client = MockClient(
            [
                MockResponse(
                    MockMessage(
                        tool_calls=[
                            MockToolCall(
                                "1", "phase_advance", '{"target": "design", "approved": true}'
                            ),
                        ]
                    )
                ),
                MockResponse(MockMessage(content="Rejected")),
            ]
        )

        interpreter = Interpreter(client, max_steps=3, state_store=store)
        result = interpreter.run([{"role": "user", "content": "Advance"}])

        assert result.stop_reason == "answer"
        # Phase should NOT have advanced — gate blocked it
        assert store.get_current_phase() == "intake"

        store.close()


def test_interpreter_phase_advance_allowed_with_exit_artifact() -> None:
    """AC-9: phase_advance succeeds after exit artifact recorded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "state.duck")
        store = StateStore(db_path)

        # Start in spec, record exit artifact and approval (spec→design is
        # an approval gate)
        store.record_artifact("intake", "intake-exit", "full")
        store.advance_phase("spec")
        digest = store.record_artifact("spec", "spec-exit", "spec complete")
        store.record_approval("spec→design", digest)

        client = MockClient(
            [
                MockResponse(
                    MockMessage(
                        tool_calls=[
                            MockToolCall(
                                "1", "phase_advance", '{"target": "design", "approved": true}'
                            ),
                        ]
                    )
                ),
                MockResponse(MockMessage(content="Advanced")),
            ]
        )

        interpreter = Interpreter(client, max_steps=3, state_store=store)
        result = interpreter.run([{"role": "user", "content": "Advance"}])

        assert result.stop_reason == "answer"
        # Phase SHOULD have advanced — exit artifact satisfied the gate
        assert store.get_current_phase() == "design"

        store.close()
