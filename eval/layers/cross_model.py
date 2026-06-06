"""Layer 3: Cross-model robustness.

Runs the same composed prompts through multiple agent models to assess
whether the quality improvement generalizes across model sizes.

Tests three arms:
  - no_inject: what the model can do with training alone
  - composed: AgentAlloy JIT composition
  - mcp: MCP skill dictionary (agent decides which skills to fetch)

Hypothesis: smaller models benefit more from just-in-time composition
because they have less inherent knowledge and rely more on injected
skills. The MCP arm tests whether the agent can identify relevant skills
— a harder task for smaller models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eval.tasks import GRADERS, TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")

# Pre-defined model lineup: small, medium, large
# Each is an OpenAI-compatible endpoint + model name
DEFAULT_MODELS = [
    {
        "name": "small",
        "url": os.environ.get("MODEL_SMALL_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_SMALL_NAME", "qwen/qwen2.5-coder-1.5b"),
    },
    {
        "name": "medium",
        "url": os.environ.get("MODEL_MEDIUM_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_MEDIUM_NAME", "qwen/qwen2.5-coder-14b"),
    },
    {
        "name": "large",
        "url": os.environ.get("MODEL_LARGE_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_LARGE_NAME", "meta-llama/llama-3.1-70b"),
    },
]


@dataclass(frozen=True)
class ModelResult:
    model_name: str
    task_id: str
    condition: str  # "no_inject", "composed", "mcp"
    score: float
    input_tokens: int
    output_tokens: int
    wall_latency_ms: float


def _build_system_prompt(
    task: Any,
    condition: str,
    compose_output: str | None = None,
    gold_skills: list[str] | None = None,
) -> str:
    """Build the system prompt for a given condition."""
    if condition == "no_inject":
        return "You are an experienced software engineer. Answer the following task."
    elif condition == "composed":
        if not compose_output:
            return "(compose output missing)"
        return (
            "You are an experienced software engineer. Apply the following "
            "task-specific guidance assembled by the AgentAlloy service:\n\n" + compose_output
        )
    elif condition == "mcp":
        skill_text = ""
        if gold_skills:
            skill_text = "\n\n[Relevant skills fetched via MCP:]\n"
            for sid in gold_skills:
                skill_text += f"\n# Skill: {sid}\n"
        return (
            "You are an experienced software engineer. You have access to a "
            "skill dictionary via an MCP tool. The skills relevant to this task "
            "have been fetched and are listed below. Use them to answer the task.\n\n" + skill_text
        )
    return ""


def run(
    models: list[dict[str, Any]] | None = None,
    k: int = 4,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run composed prompts through multiple agent models.

    For each task, compose the prompt once (deterministic), then send
    it to each model under each condition (no_inject, composed, mcp)
    and grade the output.
    """
    models = models or DEFAULT_MODELS
    results: list[ModelResult] = []

    with httpx.Client(timeout=120.0) as client:
        for model_info in models:
            model_name = model_info["name"]
            model_url = model_info["url"]
            model_id = model_info["model"]
            print(f"\n=== Model: {model_name} ({model_id}) ===")

            for task in TASKS:
                # Compose once (deterministic)
                compose_resp = client.post(
                    f"{AGENTALLOY_URL}/compose",
                    json={"task": task.spec, "phase": task.phase, "k": k},
                )
                compose_resp.raise_for_status()
                compose_body = compose_resp.json()
                composed_prompt = compose_body.get("output", "")
                source_skills = compose_body.get("source_skills", []) or []

                for condition in ("no_inject", "composed", "mcp"):
                    system_prompt = _build_system_prompt(
                        task, condition, composed_prompt, source_skills
                    )

                    agent_resp = client.post(
                        f"{model_url}/v1/chat/completions",
                        json={
                            "model": model_id,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": task.spec},
                            ],
                            "temperature": 0.2,
                            "max_tokens": 4096,
                        },
                    )
                    agent_resp.raise_for_status()
                    agent_body = agent_resp.json()
                    output = agent_body["choices"][0]["message"]["content"]
                    usage = agent_body.get("usage", {})

                    # Grade the output
                    grader = GRADERS.get(task.task_id)
                    if grader:
                        criteria = grader(output)
                        score = sum(criteria.values()) / len(criteria)
                    else:
                        score = 0.0

                    results.append(
                        ModelResult(
                            model_name=model_name,
                            task_id=task.task_id,
                            condition=condition,
                            score=score,
                            input_tokens=usage.get("prompt_tokens", 0),
                            output_tokens=usage.get("completion_tokens", 0),
                            wall_latency_ms=agent_body.get("response_ms", 0),
                        )
                    )

                    print(
                        f"  {task.task_id:35s} [{condition:12s}] score={score:.2f} "
                        f"in={usage.get('prompt_tokens', 0)} "
                        f"out={usage.get('completion_tokens', 0)}"
                    )

    # Aggregate: mean score per model per condition
    model_condition_scores: dict[str, dict[str, list[float]]] = {}
    for r in results:
        model_condition_scores.setdefault(r.model_name, {}).setdefault(r.condition, []).append(
            r.score
        )

    summary = {
        "label": "cross_model",
        "n_models": len(models),
        "n_tasks": len(TASKS),
        "k": k,
        "per_model": {
            name: {
                cond: {
                    "mean_score": sum(scores) / len(scores),
                    "n_tasks": len(scores),
                }
                for cond, scores in conditions.items()
            }
            for name, conditions in model_condition_scores.items()
        },
        "per_task": [
            {
                "model_name": r.model_name,
                "task_id": r.task_id,
                "condition": r.condition,
                "score": r.score,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "wall_latency_ms": r.wall_latency_ms,
            }
            for r in results
        ],
    }

    out_path = Path(out_dir or "eval/runs") / "layer3__cross_model.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))

    print()
    print("=== Cross-Model Summary ===")
    per_model: dict[str, dict[str, dict[str, float]]] = summary.get("per_model", {})  # type: ignore[assignment]
    for name, conditions in per_model.items():
        print(f"\n  {name}:")
        for cond, stats in conditions.items():
            print(f"    {cond:12s} mean_score={stats['mean_score']:.3f}")
    print(f"wrote: {out_path}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 3: Cross-model robustness")
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args(argv)

    run(k=args.k, out_dir=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
