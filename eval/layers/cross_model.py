"""Layer 3: Cross-model robustness.

Runs the same composed prompts through multiple agent models to assess
whether the quality improvement generalizes across model sizes.

Hypothesis: smaller models benefit more from just-in-time composition
because they have less inherent knowledge and rely more on injected
skills.
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

# Pre-defined model lineup. Each is an OpenAI-compatible endpoint + model
# name, all env-overridable. The four slots span architecture x scale:
# tiny hybrid-sparse, small dense, medium dense, medium MoE.
DEFAULT_MODELS = [
    {
        "name": "tiny",
        "url": os.environ.get("MODEL_TINY_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_TINY_NAME", "lfm2.5-8b-a1b"),
    },
    {
        "name": "small",
        "url": os.environ.get("MODEL_SMALL_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_SMALL_NAME", "gemma-4-12b"),
    },
    {
        "name": "medium",
        "url": os.environ.get("MODEL_MEDIUM_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_MEDIUM_NAME", "qwen3.6-27b"),
    },
    {
        "name": "large",
        "url": os.environ.get("MODEL_LARGE_URL", "http://localhost:1234"),
        "model": os.environ.get("MODEL_LARGE_NAME", "qwen3.6-35B-A3B"),
    },
]


@dataclass(frozen=True)
class ModelResult:
    model_name: str
    task_id: str
    score: float
    input_tokens: int
    output_tokens: int
    wall_latency_ms: float


def run(
    models: list[dict[str, Any]] | None = None,
    k: int = 4,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run composed prompts through multiple agent models.

    For each task, compose the prompt once (deterministic), then send
    it to each model and grade the output.
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
                # Compose the prompt (once, deterministic)
                compose_resp = client.post(
                    f"{AGENTALLOY_URL}/compose",
                    json={"task": task.spec, "phase": task.phase, "k": k},
                )
                compose_resp.raise_for_status()
                compose_body = compose_resp.json()
                composed_prompt = compose_body.get("output", "")

                # Send to agent model
                agent_resp = client.post(
                    f"{model_url}/v1/chat/completions",
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": composed_prompt},
                            {"role": "user", "content": task.spec},
                        ],
                        "temperature": 0.2,
                        "max_tokens": 4096,
                    },
                )
                agent_resp.raise_for_status()
                agent_body = agent_resp.json()
                choices = agent_body.get("choices")
                if not choices:
                    output = ""
                    usage = {}
                else:
                    output = choices[0]["message"]["content"]
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
                        score=score,
                        input_tokens=usage.get("prompt_tokens", 0),
                        output_tokens=usage.get("completion_tokens", 0),
                        wall_latency_ms=agent_body.get("response_ms", 0),
                    )
                )

                print(
                    f"  {task.task_id:35s} score={score:.2f} "
                    f"in={usage.get('prompt_tokens', 0)} "
                    f"out={usage.get('completion_tokens', 0)}"
                )

    # Aggregate: mean score per model
    model_scores: dict[str, list[float]] = {}
    for r in results:
        model_scores.setdefault(r.model_name, []).append(r.score)

    summary = {
        "label": "cross_model",
        "n_models": len(models),
        "n_tasks": len(TASKS),
        "k": k,
        "per_model": {
            name: {
                "mean_score": sum(scores) / len(scores),
                "n_tasks": len(scores),
            }
            for name, scores in model_scores.items()
        },
        "per_task": [
            {
                "model_name": r.model_name,
                "task_id": r.task_id,
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
    per_model: dict[str, dict[str, float]] = summary.get("per_model", {})  # type: ignore[assignment]
    for name, stats in per_model.items():
        print(f"  {name:10s} mean_score={stats['mean_score']:.3f}")
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
