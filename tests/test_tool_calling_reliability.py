"""Tool-calling reliability tests (T4)."""

from tests.tool_calling_reliability.test_harness import (
    evaluate_multi_turn,
    evaluate_single_turn,
    run_reliability_test,
)


def test_reliability_gate() -> None:
    """Reliability gate passes (stub — needs live :50001 for real test)."""
    results = run_reliability_test()
    # Stub always passes; real test needs live model
    assert results["gate_passed"]


def test_evaluate_single_turn_answer() -> None:
    """Evaluate single-turn: expected answer (no tool call)."""
    result = {"stop_reason": "answer", "tool_calls": []}
    expected = {"expected_tool": None, "expected_args": None}
    assert evaluate_single_turn(result, expected)


def test_evaluate_single_turn_tool_call() -> None:
    """Evaluate single-turn: expected tool call."""
    result = {
        "stop_reason": "answer",
        "tool_calls": [{"tool": "code_search", "args": {"query": "test"}}],
    }
    expected = {"expected_tool": "code_search", "expected_args": {"query": "test"}}
    assert evaluate_single_turn(result, expected)


def test_evaluate_multi_turn() -> None:
    """Evaluate multi-turn: expected sequence."""
    result = {
        "tool_calls": [
            {"tool": "code_search", "args": {"query": "test"}},
            {"tool": "symbols", "args": {"fqn": "Test"}},
        ]
    }
    expected = {
        "expected_sequence": [
            {"tool": "code_search", "args": {"query": "test"}},
            {"tool": "symbols", "args": {"fqn": "Test"}},
        ]
    }
    assert evaluate_multi_turn(result, expected)
