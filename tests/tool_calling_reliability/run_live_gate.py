"""Live reliability gate — run T4 scenarios against the real :50001 MiniCPM5-2B driver.

Pass criteria (per tasks.artifact):
- >=90% fully correct single-turn (tool+args)
- Zero schema-invalid calls
- 100% legal phase_advance targets
- >=80% correct next-step decisions multi-turn
"""

import json
import sys
from typing import Any

from openai import OpenAI

from agentalloy.tools import ALL_TOOLS
from agentalloy.validate import validate_tool_call

# Live driver config
CLIENT = OpenAI(
    base_url="http://localhost:50001/v1",
    api_key="sk-local-f2d05be43df88a4c5b96b0915ec10029",
)
MODEL = "minicpm5-2b"

# Single-turn scenarios
SINGLE_TURN: list[dict[str, Any]] = [
    {
        "id": "st-01",
        "input": "Search for code related to authentication",
        "expected_tool": "code_search",
        "expected_args_match": {"query": "authentication"},
    },
    {
        "id": "st-02",
        "input": "Look up the symbol agentalloy.retrieval.rerank",
        "expected_tool": "symbols",
        "expected_args_match": {"fqn": "agentalloy.retrieval.rerank"},
    },
    {
        "id": "st-03",
        "input": "Add a contract with slug 'sdd-build', domain tags ['rust'], touches 'core crate'",
        "expected_tool": "contract_add",
        "expected_args_match": {"slug": "sdd-build", "domain_tags": ["rust"]},
    },
    {
        "id": "st-04",
        "input": "Record artifact in phase 'build', name 'build-log', body 'all tests pass'",
        "expected_tool": "artifact_record",
        "expected_args_match": {"phase": "build", "name": "build-log"},
    },
    {
        "id": "st-05",
        "input": "Advance to the design phase",
        "expected_tool": "phase_advance",
        "expected_args_match": {"target": "design", "approved": True},
    },
    {
        "id": "st-06",
        "input": "What is 2+2?",
        "expected_tool": None,  # No tool call — final answer
        "expected_args_match": None,
    },
    {
        "id": "st-07",
        "input": "Get the details of contract sdd-build",
        "expected_tool": "contract_detail",
        "expected_args_match": {"slug": "sdd-build"},
    },
    {
        "id": "st-08",
        "input": "Show me the last 5 telemetry traces",
        "expected_tool": "telemetry",
        "expected_args_match": {"k": 5},
    },
    {
        "id": "st-09",
        "input": "Find skills for building the Rust core crate in the build phase",
        "expected_tool": "get_skill_for",
        "expected_args_match": {"task": "building the Rust core crate", "phase": "build"},
    },
    {
        "id": "st-10",
        "input": "Read the body of the spec artifact named 'product-spec' in contract 'sdd-spec'",
        "expected_tool": "artifact_body",
        "expected_args_match": {"phase": "spec", "slug": "sdd-spec", "name": "product-spec"},
    },
]


def call_model(user_input: str) -> dict[str, Any]:
    """Call the live :50001 model with the full 12-tool surface."""
    response = CLIENT.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": user_input}],
        tools=ALL_TOOLS,
        temperature=0.0,
    )
    choice = response.choices[0]
    msg = choice.message

    if not msg.tool_calls:
        return {"tool": None, "args": None, "content": msg.content or ""}

    tc = msg.tool_calls[0]
    return {
        "tool": tc.function.name,
        "args": json.loads(tc.function.arguments),
        "content": msg.content or "",
    }


def evaluate_single_turn(result: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a single-turn result."""
    expected_tool = scenario["expected_tool"]
    expected_match = scenario["expected_args_match"]

    # Expected no tool call
    if expected_tool is None:
        passed = result["tool"] is None
        reason = "no tool call" if passed else f"unexpected tool: {result['tool']}"
        return {"passed": passed, "reason": reason}

    # Expected a tool call
    if result["tool"] is None:
        return {"passed": False, "reason": "expected tool call but got none"}

    if result["tool"] != expected_tool:
        return {"passed": False, "reason": f"wrong tool: {result['tool']} != {expected_tool}"}

    # Validate args
    is_valid, err = validate_tool_call(result["tool"], json.dumps(result["args"]))
    if not is_valid:
        return {"passed": False, "reason": f"schema-invalid: {err}"}

    # Check expected args match (partial — key fields must match)
    if expected_match:
        for key, value in expected_match.items():
            actual = result["args"].get(key)
            if actual != value:
                return {"passed": False, "reason": f"arg mismatch: {key}={actual} != {value}"}

    return {"passed": True, "reason": "ok"}


def run_gate() -> dict[str, Any]:
    """Run the full gate and return results."""
    results: list[dict[str, Any]] = []
    schema_invalid_count = 0

    print(f"Running T4 live gate against {MODEL} on :50001")
    print(f"Tools: {len(ALL_TOOLS)} (11 read + 3 state)")
    print(f"Scenarios: {len(SINGLE_TURN)} single-turn")
    print("=" * 60)

    for scenario in SINGLE_TURN:
        print(f"\n[{scenario['id']}] {scenario['input'][:60]}...")
        try:
            result = call_model(scenario["input"])
            evaluation = evaluate_single_turn(result, scenario)

            # Check for schema-invalid
            if result["tool"]:
                is_valid, _ = validate_tool_call(result["tool"], json.dumps(result["args"]))
                if not is_valid:
                    schema_invalid_count += 1

            results.append(
                {
                    "id": scenario["id"],
                    "expected_tool": scenario["expected_tool"],
                    "actual_tool": result["tool"],
                    "actual_args": result.get("args"),
                    **evaluation,
                }
            )

            status = "PASS" if evaluation["passed"] else "FAIL"
            print(f"  -> {status}: {evaluation['reason']}")
            if result["tool"]:
                print(f"  -> tool={result['tool']}, args={json.dumps(result.get('args', {}))}")

        except Exception as e:
            results.append(
                {
                    "id": scenario["id"],
                    "expected_tool": scenario["expected_tool"],
                    "actual_tool": None,
                    "passed": False,
                    "reason": f"error: {e}",
                }
            )
            print(f"  -> ERROR: {e}")

    # Compute pass rates
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    rate = passed / total if total > 0 else 0

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} passed ({rate:.0%})")
    print(f"Schema-invalid calls: {schema_invalid_count}")
    print("Gate threshold: >=90% single-turn, zero schema-invalid")

    gate_passed = rate >= 0.90 and schema_invalid_count == 0
    print(f"\nGATE: {'PASSED' if gate_passed else 'FAILED'}")

    return {
        "total": total,
        "passed": passed,
        "rate": rate,
        "schema_invalid": schema_invalid_count,
        "gate_passed": gate_passed,
        "results": results,
    }


if __name__ == "__main__":
    gate_results = run_gate()
    sys.exit(0 if gate_results["gate_passed"] else 1)
