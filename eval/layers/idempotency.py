"""Layer 4: Composition idempotency.

Verifies that the same task always produces the same composition —
proving the deterministic claim. Sends identical POST requests to
/compose N times and checks byte-identical output.

Also verifies that source_skills is identical across runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eval.tasks import TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")


@dataclass(frozen=True)
class IdempotencyResult:
    task_id: str
    phase: str
    output_hash: str
    source_hash: str
    output_consistent: bool
    source_consistent: bool
    compose_ms_values: list[float]


def run(
    n: int = 100,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run idempotency checks on /compose endpoint.

    Sends the same task N times and verifies output is byte-identical.
    """
    results: list[IdempotencyResult] = []
    total_pass = 0
    total_fail = 0

    with httpx.Client(timeout=30.0) as client:
        for task in TASKS:
            outputs: list[str] = []
            sources: list[list[str]] = []
            compose_times: list[float] = []

            for _ in range(n):
                resp = client.post(
                    f"{AGENTALLOY_URL}/compose",
                    json={"task": task.spec, "phase": task.phase, "k": 4},
                )
                resp.raise_for_status()
                body = resp.json()

                output = body.get("output", "")
                source = body.get("source_skills", []) or []
                compose_ms = body.get("compose_ms", 0)

                outputs.append(output)
                sources.append(source)
                compose_times.append(compose_ms)

            # Check consistency
            output_hash = hashlib.sha256(outputs[0].encode()).hexdigest()[:12]
            source_hash = hashlib.sha256(str(sources[0]).encode()).hexdigest()[:12]

            output_consistent = all(o == outputs[0] for o in outputs)
            source_consistent = all(s == sources[0] for s in sources)

            passed = output_consistent and source_consistent
            if passed:
                total_pass += 1
            else:
                total_fail += 1

            results.append(
                IdempotencyResult(
                    task_id=task.task_id,
                    phase=task.phase,
                    output_hash=output_hash,
                    source_hash=source_hash,
                    output_consistent=output_consistent,
                    source_consistent=source_consistent,
                    compose_ms_values=compose_times,
                )
            )

            status = "PASS" if passed else "FAIL"
            print(
                f"  {task.task_id:35s} [{status}] "
                f"out_hash={output_hash} src_hash={source_hash} "
                f"avg_ms={sum(compose_times) / len(compose_times):.0f}"
            )

    summary = {
        "label": "idempotency",
        "n_runs_per_task": n,
        "n_tasks": len(results),
        "total_pass": total_pass,
        "total_fail": total_fail,
        "all_pass": total_fail == 0,
        "per_task": [
            {
                "task_id": r.task_id,
                "phase": r.phase,
                "output_hash": r.output_hash,
                "source_hash": r.source_hash,
                "output_consistent": r.output_consistent,
                "source_consistent": r.source_consistent,
                "avg_compose_ms": sum(r.compose_ms_values) / len(r.compose_ms_values),
                "p95_compose_ms": sorted(r.compose_ms_values)[int(0.95 * n)] if n > 0 else 0,
            }
            for r in results
        ],
    }

    out_path = Path(out_dir or "eval/runs") / "layer4__idempotency.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))

    print()
    print(f"=== Idempotency | n={n} runs per task | {len(results)} tasks ===")
    print(f"PASS: {total_pass}/{len(results)}  FAIL: {total_fail}/{len(results)}")
    print(f"all deterministic: {summary['all_pass']}")
    print(f"wrote: {out_path}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 4: Composition idempotency")
    parser.add_argument("--n", type=int, default=100, help="Runs per task")
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args(argv)

    run(n=args.n, out_dir=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
