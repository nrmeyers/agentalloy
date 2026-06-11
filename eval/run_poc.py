"""POC harness: composed vs flat across the 5 pre-registered tasks.

Composed arm: POST /compose to a running agentalloy (uvicorn at $AGENTALLOY_URL,
default http://localhost:47950), then call the agent model with /compose's
``output`` field as a system prompt + the task spec as user prompt.

Flat arm: concatenate the gold skills' ``raw_prose`` from the pack corpus
(``src/agentalloy/_packs/``) as the system prompt + task spec as user prompt.

External arm: third-party skill prose (``eval/external_skills.py`` registry,
files under ``eval/external/``) injected verbatim — the incumbent practice
of wiring an off-the-shelf pack into the system prompt.

All arms hit an OpenAI-compatible local server ($LM_STUDIO_URL, default
LM Studio on :1234) with $AGENT_MODEL for the agent call.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval import external_skills
from eval.tasks import GRADERS, TASKS, Task

logger = logging.getLogger("eval.run_poc")

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_ROOT = REPO_ROOT / "src" / "agentalloy" / "_packs"
RUNS_ROOT = REPO_ROOT / "eval" / "runs"

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "qwen/qwen3.6-35b-a3b")


@functools.cache
def _pack_skill_index() -> dict[str, Path]:
    """Map skill_id -> pack YAML path across the bundled pack corpus."""
    index: dict[str, Path] = {}
    for yaml_path in PACKS_ROOT.glob("*/*.yaml"):
        if yaml_path.name == "pack.yaml":
            continue
        doc: Any = yaml.safe_load(yaml_path.read_text())
        if isinstance(doc, dict):
            skill_id = doc.get("skill_id")
            if isinstance(skill_id, str):
                index[skill_id] = yaml_path
    return index


@dataclass
class RunResult:
    task_id: str
    condition: str  # "composed" | "flat"
    run_index: int
    output: str
    input_tokens: int | None
    output_tokens: int | None
    agent_latency_ms: int
    compose_latency_ms: int | None
    compose_result_type: str | None
    grades: dict[str, bool]
    score: float


def load_flat_prompt(task: Task) -> str:
    parts: list[str] = [
        "You are an experienced software engineer. Use the following skill "
        "guidance to answer the task that follows.\n"
    ]
    for skill_id in task.gold_skills:
        yaml_path = _pack_skill_index().get(skill_id)
        if yaml_path is None:
            raise FileNotFoundError(f"flat skill source missing from packs: {skill_id}")
        doc: Any = yaml.safe_load(yaml_path.read_text())
        prose = doc.get("raw_prose") if isinstance(doc, dict) else None
        parts.append(f"\n# Skill: {skill_id}\n\n{prose or ''}\n")
    return "\n".join(parts)


def call_compose(client: httpx.Client, task: Task, k: int) -> tuple[str, str, int, list[str]]:
    """Returns (assembled_text, result_type, compose_latency_ms, source_skills)."""
    start_ns = time.perf_counter_ns()
    resp = client.post(
        f"{AGENTALLOY_URL}/compose",
        json={"task": task.spec, "phase": task.phase, "k": k},
        timeout=httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0),
    )
    elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
    if resp.status_code != 200:
        raise RuntimeError(f"/compose returned {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    return (
        body.get("output", ""),
        body.get("result_type", "unknown"),
        elapsed_ms,
        body.get("source_skills", []),
    )


_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# Transient network failures (Tailscale blips, server restarts) shouldn't
# kill an overnight run. Retry connect-class errors only — a read timeout
# means the model is genuinely stuck and retrying would double the damage.
_RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 10.0


def call_agent(
    client: httpx.Client, system: str, user: str, *, seed: int
) -> tuple[str, int | None, int | None, int]:
    """Returns (content, prompt_tokens, completion_tokens, latency_ms)."""
    start_ns = time.perf_counter_ns()
    payload: dict[str, Any] = {
        "model": AGENT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
        "seed": seed,
        "stream": False,
    }
    # Reasoning-effort hint. Historically hardcoded to "none" to curb
    # Qwen3.6-A3B's CoT loop — but measured token counts show the Qwen/LFM
    # templates ignore it while Gemma honors it, silently disabling Gemma's
    # thinking. Default stays "none" so earlier runs reproduce; set
    # AGENT_REASONING_EFFORT="" to omit the field (model default), or any
    # other value to pass through.
    effort = os.environ.get("AGENT_REASONING_EFFORT", "none")
    if effort:
        payload["reasoning_effort"] = effort
    resp = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json=payload,
                timeout=httpx.Timeout(connect=5.0, read=900.0, write=10.0, pool=5.0),
            )
            break
        except _RETRYABLE as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            logger.warning("agent call attempt %d failed (%s), retrying", attempt, exc)
            time.sleep(_RETRY_BACKOFF_S * attempt)
    assert resp is not None
    elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
    if resp.status_code != 200:
        raise RuntimeError(f"agent call returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    msg = data["choices"][0]["message"]["content"] or ""
    # Reasoning models that inline CoT (rather than using a separate
    # reasoning_content field) would otherwise leak it into the graders.
    msg = _THINK_BLOCK.sub("", msg)
    usage = data.get("usage", {})
    return (msg, usage.get("prompt_tokens"), usage.get("completion_tokens"), elapsed_ms)


def run_one(
    client: httpx.Client,
    task: Task,
    condition: str,
    run_index: int,
    out_dir: Path,
    k: int,
    graders: dict[str, Callable[[str], dict[str, bool]]] | None = None,
    task_set: str = "generic",
) -> RunResult:
    seed = int(
        hashlib.sha256(f"{task.task_id}:{condition}:{run_index}".encode()).hexdigest(), 16
    ) % (2**31)
    compose_result_type: str | None = None
    compose_latency_ms: int | None = None
    if condition == "composed":
        assembled, compose_result_type, compose_latency_ms, _ = call_compose(client, task, k)
        if not assembled.strip():
            assembled = "(compose returned empty result — no domain fragments matched)"
        system_prompt = (
            "You are an experienced software engineer. Apply the following "
            "task-specific guidance assembled by the AgentAlloy service:\n\n" + assembled
        )
    elif condition == "flat":
        system_prompt = load_flat_prompt(task)
    elif condition == "external":
        # Third-party skill prose injected verbatim. Same framing as flat;
        # content differs (changes both content and format vs composed —
        # this arm measures "composed vs installing a popular pack").
        system_prompt = external_skills.load_external_prompt(task.task_id, task_set)
    elif condition == "none":
        # Control arm: no skill injection at all — measures whether either
        # injection method beats the bare model.
        system_prompt = "You are an experienced software engineer."
    else:
        raise ValueError(f"unknown condition: {condition}")

    output, in_tok, out_tok, agent_ms = call_agent(client, system_prompt, task.spec, seed=seed)

    active_graders = graders if graders is not None else GRADERS
    grader = active_graders[task.task_id]
    grades = grader(output)
    score = sum(1 for v in grades.values() if v) / len(grades) if grades else 0.0

    cond_dir = out_dir / task.task_id / condition
    cond_dir.mkdir(parents=True, exist_ok=True)
    (cond_dir / f"run-{run_index}.txt").write_text(output)
    meta = {
        "task_id": task.task_id,
        "condition": condition,
        "run_index": run_index,
        "seed": seed,
        "agent_model": AGENT_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "agent_latency_ms": agent_ms,
        "compose_latency_ms": compose_latency_ms,
        "compose_result_type": compose_result_type,
        "system_prompt_chars": len(system_prompt),
        "grades": grades,
        "score": score,
    }
    (cond_dir / f"run-{run_index}.meta.json").write_text(json.dumps(meta, indent=2))

    logger.info(
        "%s/%s/run-%d score=%.2f tokens_in=%s tokens_out=%s",
        task.task_id,
        condition,
        run_index,
        score,
        in_tok,
        out_tok,
    )
    return RunResult(
        task_id=task.task_id,
        condition=condition,
        run_index=run_index,
        output=output,
        input_tokens=in_tok,
        output_tokens=out_tok,
        agent_latency_ms=agent_ms,
        compose_latency_ms=compose_latency_ms,
        compose_result_type=compose_result_type,
        grades=grades,
        score=score,
    )


def aggregate(results: list[RunResult]) -> dict[str, Any]:
    by_task: dict[str, dict[str, list[RunResult]]] = {}
    for r in results:
        by_task.setdefault(r.task_id, {}).setdefault(r.condition, []).append(r)

    summary: dict[str, Any] = {"by_task": {}, "totals": {}}
    totals: dict[str, dict[str, float]] = {
        cond: {"score": 0.0, "n": 0, "in_tok": 0, "out_tok": 0, "wall_ms": 0}
        for cond in ("composed", "flat", "external", "none")
    }

    for task_id, by_cond in by_task.items():
        task_summary: dict[str, Any] = {}
        for cond, runs in by_cond.items():
            mean_score = sum(r.score for r in runs) / len(runs)
            mean_in = sum((r.input_tokens or 0) for r in runs) / len(runs)
            mean_out = sum((r.output_tokens or 0) for r in runs) / len(runs)
            mean_total_tok = mean_in + mean_out
            mean_agent_ms = sum(r.agent_latency_ms for r in runs) / len(runs)
            mean_compose_ms = (
                sum((r.compose_latency_ms or 0) for r in runs) / len(runs)
                if cond == "composed"
                else 0.0
            )
            mean_wall_ms = mean_agent_ms + mean_compose_ms
            # Tokens-per-second over the whole call (compose + agent for composed,
            # just agent for flat). Both arms include input prefill + output decode
            # in the wall clock.
            tps = (mean_total_tok / (mean_wall_ms / 1000.0)) if mean_wall_ms > 0 else 0.0
            task_summary[cond] = {
                "n": len(runs),
                "mean_score": mean_score,
                "passes": sum(1 for r in runs if r.score == 1.0),
                "mean_input_tokens": mean_in,
                "mean_output_tokens": mean_out,
                "mean_total_tokens": mean_total_tok,
                "mean_agent_latency_ms": mean_agent_ms,
                "mean_compose_latency_ms": mean_compose_ms,
                "mean_wall_latency_ms": mean_wall_ms,
                "tokens_per_second": tps,
            }
            totals[cond]["score"] += mean_score
            totals[cond]["n"] += 1
            totals[cond]["in_tok"] += int(mean_in)
            totals[cond]["out_tok"] += int(mean_out)
            totals[cond]["wall_ms"] += mean_wall_ms
        if "composed" in task_summary and "flat" in task_summary:
            c = task_summary["composed"]
            f = task_summary["flat"]
            task_summary["delta_score_composed_minus_flat"] = c["mean_score"] - f["mean_score"]
            task_summary["total_token_ratio_flat_over_composed"] = (
                f["mean_total_tokens"] / c["mean_total_tokens"]
                if c["mean_total_tokens"] > 0
                else None
            )
            task_summary["wall_clock_ratio_flat_over_composed"] = (
                f["mean_wall_latency_ms"] / c["mean_wall_latency_ms"]
                if c["mean_wall_latency_ms"] > 0
                else None
            )
        summary["by_task"][task_id] = task_summary

    for cond in ("composed", "flat", "external", "none"):
        if totals[cond]["n"]:
            n = totals[cond]["n"]
            summary["totals"][cond] = {
                "mean_score": totals[cond]["score"] / n,
                "total_input_tokens": int(totals[cond]["in_tok"]),
                "total_output_tokens": int(totals[cond]["out_tok"]),
                "total_tokens": int(totals[cond]["in_tok"] + totals[cond]["out_tok"]),
                "total_wall_clock_ms": int(totals[cond]["wall_ms"]),
            }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3, help="runs per task per condition")
    parser.add_argument("--k", type=int, default=4, help="compose k (composed arm only)")
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="optional label appended to the run directory name (e.g. 'k2', 'no-diversity')",
    )
    parser.add_argument("--task", type=str, default=None, help="single task_id to run")
    parser.add_argument(
        "--conditions",
        nargs="+",
        default=["composed", "flat"],
        choices=["composed", "flat", "external", "none"],
    )
    parser.add_argument(
        "--task-set",
        dest="task_set",
        default="generic",
        choices=["generic", "domain"],
        help="which task set to run: 'generic' (default) or 'domain' (domain_tasks.py)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.task_set == "domain":
        from eval.domain_tasks import DOMAIN_GRADERS, DOMAIN_TASKS

        task_pool = DOMAIN_TASKS
        active_graders: dict[str, Callable[[str], dict[str, bool]]] = DOMAIN_GRADERS  # type: ignore[assignment]
    else:
        task_pool = TASKS
        active_graders = GRADERS

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    dir_name = f"{timestamp}__{args.label}" if args.label else timestamp
    out_dir = RUNS_ROOT / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_tasks = [t for t in task_pool if args.task is None or t.task_id == args.task]

    # Tasks with no genuine third-party skill stay unmapped by design; the
    # external arm skips them (recorded in the manifest) rather than blocking
    # the leg. Broken mappings (missing files/registry entries) still block.
    external_skipped: set[str] = set()
    if "external" in args.conditions:
        if args.task_set != "generic":
            external_skipped = {
                t.task_id for t in selected_tasks if t.task_id not in external_skills.TASK_MAPPING
            }
            for task_id in sorted(external_skipped):
                logger.warning("external arm: no third-party skill mapped, skipping %s", task_id)
        external_task_ids = [t.task_id for t in selected_tasks if t.task_id not in external_skipped]
        problems = external_skills.validate(external_task_ids, args.task_set)
        if problems:
            for p in problems:
                print(f"external arm blocked: {p}", file=sys.stderr)
            return 1

    manifest = {
        "started_at": timestamp,
        "label": args.label,
        "task_set": args.task_set,
        "k": args.k,
        "agent_model": AGENT_MODEL,
        "agentalloy_url": AGENTALLOY_URL,
        "lm_studio_url": LM_STUDIO_URL,
        "diversity_selection": os.environ.get("RUNTIME_DIVERSITY_SELECTION", "on"),
        "tasks": [t.task_id for t in selected_tasks],
        "conditions": args.conditions,
        "runs_per_cell": args.n,
    }
    if "external" in args.conditions:
        # Freeze the task→skill mapping + provenance so the arm is auditable.
        manifest["external_skills"] = external_skills.manifest_entry(
            [t.task_id for t in selected_tasks if t.task_id not in external_skipped],
            args.task_set,
        )
        manifest["external_skipped_tasks"] = sorted(external_skipped)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    results: list[RunResult] = []
    with httpx.Client() as client:
        for task in selected_tasks:
            for cond in args.conditions:
                if cond == "external" and task.task_id in external_skipped:
                    continue
                for i in range(args.n):
                    try:
                        results.append(
                            run_one(
                                client,
                                task,
                                cond,
                                i,
                                out_dir,
                                args.k,
                                active_graders,
                                task_set=args.task_set,
                            )
                        )
                    except Exception:
                        logger.exception("run failed: %s/%s/run-%d", task.task_id, cond, i)

    summary = aggregate(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n=== POC summary ===")
    print(f"runs dir: {out_dir}")
    print(
        "\nLegend: total_tok = input + output. wall_ms = compose + agent (composed) or agent (flat)."
    )
    print("        tps = total_tok / wall_seconds (effective throughput).")
    for task_id, task_summary in summary["by_task"].items():
        print(f"\n{task_id}")
        for cond in ("composed", "flat", "external", "none"):
            if cond in task_summary:
                ts = task_summary[cond]
                print(
                    f"  {cond:8} score={ts['mean_score']:.2f} "
                    f"passes={ts['passes']}/{ts['n']} "
                    f"in={ts['mean_input_tokens']:>5.0f} "
                    f"out={ts['mean_output_tokens']:>5.0f} "
                    f"total={ts['mean_total_tokens']:>6.0f} "
                    f"wall={ts['mean_wall_latency_ms']:>6.0f}ms "
                    f"tps={ts['tokens_per_second']:>5.1f}"
                )
        if "delta_score_composed_minus_flat" in task_summary:
            d = task_summary["delta_score_composed_minus_flat"]
            c = task_summary["composed"]
            f = task_summary["flat"]
            tok_pct = (
                (f["mean_total_tokens"] - c["mean_total_tokens"]) / f["mean_total_tokens"] * 100
                if f["mean_total_tokens"]
                else 0
            )
            wall_pct = (
                (f["mean_wall_latency_ms"] - c["mean_wall_latency_ms"])
                / f["mean_wall_latency_ms"]
                * 100
                if f["mean_wall_latency_ms"]
                else 0
            )
            print(
                f"  → Δscore={d:+.2f}  "
                f"composed uses {tok_pct:.0f}% fewer tokens  "
                f"composed runs {wall_pct:.0f}% faster"
            )
    if "composed" in summary["totals"] and "flat" in summary["totals"]:
        c = summary["totals"]["composed"]
        f_ = summary["totals"]["flat"]
        c_tok = c["total_tokens"]
        f_tok = f_["total_tokens"]
        c_ms = c["total_wall_clock_ms"]
        f_ms = f_["total_wall_clock_ms"]
        tok_pct = (f_tok - c_tok) / f_tok * 100 if f_tok else 0
        wall_pct = (f_ms - c_ms) / f_ms * 100 if f_ms else 0
        print(f"\nTOTALS  composed score={c['mean_score']:.2f}  flat score={f_['mean_score']:.2f}")
        print(
            f"        tokens: composed={c_tok}  flat={f_tok}  ({tok_pct:.0f}% fewer with composed)"
        )
        print(
            f"        wall:   composed={c_ms / 1000:.1f}s  flat={f_ms / 1000:.1f}s  "
            f"({wall_pct:.0f}% faster with composed)"
        )
    if "none" in summary["totals"]:
        b = summary["totals"]["none"]
        print(
            f"BASELINE (no skills)  score={b['mean_score']:.2f}  "
            f"tokens={b['total_tokens']}  wall={b['total_wall_clock_ms'] / 1000:.1f}s"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())


_ = asdict  # silence unused (used elsewhere if extended)
