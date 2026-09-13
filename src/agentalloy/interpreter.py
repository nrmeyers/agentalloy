"""Interpreter: tool-calling agent loop (LangGraph ToolNode pattern).

The interpreter receives a user request, calls the local orchestrator
sidecar (config.interp_model — MiniCPM5-2B by default) with 14 tools,
and loops until the model emits a final answer or an exit condition is hit.

Exit conditions (R1):
- answer: model emits no tool call (final answer)
- step_budget: exceeded max_steps (default 6 via config — room for
  discovery reads before contract/skill work; the sidecar's tokens are cheap)
- duplicate: same (tool, args) called twice
- tool_failed: tool execution failed
- validation_failed: args failed validation after one retry (the validation
  error is fed back to the model once before giving up)
"""

from dataclasses import dataclass, field
from typing import Any

from agentalloy.executors import execute_tool, set_run_store, set_store
from agentalloy.state_store import StateStore
from agentalloy.tools import ALL_TOOLS
from agentalloy.validate import validate_tool_call


@dataclass
class InterpreterResult:
    """Result of an interpreter run."""

    answer: str | None
    stop_reason: str  # "answer", "step_budget", "duplicate", "tool_failed", "validation_failed"
    steps: int
    tool_calls: list[dict[str, Any]]
    # The full message history of the run (input + assistant/tool turns).
    # The caller's input list is never mutated; follow-up completions that
    # want the tool context read it from here.
    messages: list[dict[str, Any]] = field(default_factory=list)


class Interpreter:
    """Tool-calling interpreter loop."""

    def __init__(
        self,
        client: Any,  # OpenAI-compatible client
        max_steps: int = 6,
        hard_cap: int = 6,
        state_store: StateStore | None = None,
        model: str = "minicpm5-2b",
    ):
        self.client = client
        self.max_steps = max_steps
        self.hard_cap = hard_cap
        self.state_store = state_store
        self.model = model

    def run(
        self,
        messages: list[dict[str, Any]],
        state_store: StateStore | None = None,
    ) -> InterpreterResult:
        """Run the interpreter loop.

        Args:
            messages: Initial messages (user request)
            state_store: optional per-run store (e.g. a project-scoped view);
                bound thread-locally so concurrent runs don't cross scopes.

        Returns:
            InterpreterResult with answer, stop_reason, steps, tool_calls
        """
        try:
            set_run_store(state_store or None)
            return self._run(messages)
        finally:
            set_run_store(None)

    def _run(self, messages: list[dict[str, Any]]) -> InterpreterResult:
        # Never mutate the caller's list; the run's history is returned on
        # the result instead.
        messages = list(messages)
        seen_calls: set[tuple[str, str]] = set()
        tool_calls_log: list[dict[str, Any]] = []
        step = 0
        validation_retried = False

        # Inject real store into executors
        set_store(self.state_store)

        def done(answer: str | None, stop_reason: str) -> InterpreterResult:
            return InterpreterResult(
                answer=answer,
                stop_reason=stop_reason,
                steps=step,
                tool_calls=tool_calls_log,
                messages=messages,
            )

        while step < self.hard_cap:
            # Call model with tools
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=ALL_TOOLS,
                temperature=0.0,
            )

            choice = response.choices[0]
            message = choice.message

            # No tool call → final answer
            if not message.tool_calls:
                return done(message.content or "", "answer")

            # ONE assistant message carrying the whole tool_calls array —
            # N separate assistant messages for N calls are rejected by
            # strict chat templates. The turn is staged and appended only
            # when it completes, so an early exit never leaves a dangling
            # assistant message without its tool results.
            assistant_msg = {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    tc.model_dump() if hasattr(tc, "model_dump") else tc
                    for tc in message.tool_calls
                ],
            }
            turn: list[dict[str, Any]] = [assistant_msg]

            retry_this_turn = False
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments

                # Validate FIRST — the error is fed back to the model once
                # (the documented single retry) before the run gives up.
                # Only valid calls enter the duplicate set, so a repeated
                # invalid call reports validation_failed, not duplicate.
                is_valid, error = validate_tool_call(tool_name, tool_args)
                if not is_valid:
                    if validation_retried:
                        return done(None, "validation_failed")
                    validation_retried = True
                    turn.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"validation error: {error}. Fix the arguments.",
                        }
                    )
                    retry_this_turn = True
                    continue

                # Duplicate check
                call_key = (tool_name, tool_args)
                if call_key in seen_calls:
                    return done(None, "duplicate")
                seen_calls.add(call_key)

                # Execute
                try:
                    result = execute_tool(tool_name, tool_args)
                except Exception:
                    return done(None, "tool_failed")
                tool_calls_log.append(
                    {
                        "tool": tool_name,
                        "args": tool_args,
                        "result": result,
                    }
                )
                turn.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result),
                    }
                )

            messages.extend(turn)
            if retry_this_turn:
                # The retry round replaces this step, not adds to it.
                continue

            step += 1

            # Step budget check
            if step >= self.max_steps:
                return done(None, "step_budget")

        # Hard cap reached
        return done(None, "step_budget")

    def run_streaming(self, messages: list[dict[str, Any]]) -> Any:
        """Run the interpreter with streaming, yielding events.

        Yields dicts with event types:
        - {"type": "token", "content": "..."} — incremental text
        - {"type": "tool_call", "tool": "...", "args": "..."} — tool invocation
        - {"type": "tool_result", "tool": "...", "result": "..."} — tool output
        - {"type": "done", "answer": "...", "stop_reason": "...", "steps": N}
        """
        from collections.abc import Generator

        # Never mutate the caller's list (mirrors run()).
        messages = list(messages)
        seen_calls: set[tuple[str, str]] = set()
        tool_calls_log: list[dict[str, Any]] = []
        step = 0
        validation_retried = False
        set_store(self.state_store)

        def generate() -> Generator[dict[str, Any], None, None]:
            nonlocal step, validation_retried

            while step < self.hard_cap:
                # Context manager closes the underlying httpx stream on every
                # exit path; a bare iterator leaks the socket when the
                # consumer abandons this generator (disconnect, exception).
                with self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=ALL_TOOLS,
                    temperature=0.0,
                    stream=True,
                ) as stream:
                    # Collect streamed content
                    content_parts: list[str] = []
                    tool_calls_stream: dict[int, dict[str, str]] = {}

                    for chunk in stream:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta is None:
                            continue

                        # Stream text content
                        if delta.content:
                            content_parts.append(delta.content)
                            yield {"type": "token", "content": delta.content}

                        # Collect tool call deltas
                        if delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index
                                if idx not in tool_calls_stream:
                                    tool_calls_stream[idx] = {
                                        "id": "",
                                        "name": "",
                                        "arguments": "",
                                    }
                                if tc_delta.id:
                                    tool_calls_stream[idx]["id"] = tc_delta.id
                                if tc_delta.function and tc_delta.function.name:
                                    tool_calls_stream[idx]["name"] += tc_delta.function.name
                                if tc_delta.function and tc_delta.function.arguments:
                                    tool_calls_stream[idx]["arguments"] += (
                                        tc_delta.function.arguments
                                    )

                full_content = "".join(content_parts)

                # No tool calls → final answer
                if not tool_calls_stream:
                    yield {
                        "type": "done",
                        "answer": full_content,
                        "stop_reason": "answer",
                        "steps": step,
                    }
                    return

                # ONE assistant message carrying the whole tool_calls array
                # (mirrors run(): strict chat templates reject N consecutive
                # assistant messages, and full_content must not duplicate
                # into every one). Staged; appended only when the turn holds.
                assistant_msg = {
                    "role": "assistant",
                    "content": full_content or None,
                    "tool_calls": [
                        {
                            "id": tc_data["id"],
                            "type": "function",
                            "function": {
                                "name": tc_data["name"],
                                "arguments": tc_data["arguments"],
                            },
                        }
                        for _idx, tc_data in sorted(tool_calls_stream.items())
                    ],
                }
                turn: list[dict[str, Any]] = [assistant_msg]
                retry_this_turn = False

                # Process collected tool calls
                for _idx, tc_data in sorted(tool_calls_stream.items()):
                    tool_name = tc_data["name"]
                    tool_args = tc_data["arguments"]
                    tool_id = tc_data["id"]

                    # Validate FIRST — error fed back once (documented
                    # retry); only valid calls enter the duplicate set.
                    is_valid, error = validate_tool_call(tool_name, tool_args)
                    if not is_valid:
                        if validation_retried:
                            yield {
                                "type": "done",
                                "answer": None,
                                "stop_reason": "validation_failed",
                                "steps": step,
                            }
                            return
                        validation_retried = True
                        turn.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "content": f"validation error: {error}. Fix the arguments.",
                            }
                        )
                        retry_this_turn = True
                        continue

                    # Duplicate check
                    call_key = (tool_name, tool_args)
                    if call_key in seen_calls:
                        yield {
                            "type": "done",
                            "answer": None,
                            "stop_reason": "duplicate",
                            "steps": step,
                        }
                        return
                    seen_calls.add(call_key)

                    yield {
                        "type": "tool_call",
                        "tool": tool_name,
                        "args": tool_args,
                    }

                    # Execute
                    try:
                        result = execute_tool(tool_name, tool_args)
                    except Exception:
                        yield {
                            "type": "done",
                            "answer": None,
                            "stop_reason": "tool_failed",
                            "steps": step,
                        }
                        return
                    tool_calls_log.append({"tool": tool_name, "args": tool_args, "result": result})
                    yield {
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": result,
                    }
                    turn.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": str(result),
                        }
                    )

                messages.extend(turn)
                if retry_this_turn:
                    # The retry round replaces this step, not adds to it.
                    continue

                step += 1

                if step >= self.max_steps:
                    yield {
                        "type": "done",
                        "answer": None,
                        "stop_reason": "step_budget",
                        "steps": step,
                    }
                    return

            # Hard cap
            yield {
                "type": "done",
                "answer": None,
                "stop_reason": "step_budget",
                "steps": step,
            }

        return generate()
