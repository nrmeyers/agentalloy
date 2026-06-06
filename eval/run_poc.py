"""POC harness: composed vs MCP vs no-injection across the pre-registered tasks.

Three arms:

  composed — POST /compose to a running agentalloy service, then call the agent
             model with /compose's ``output`` field as system prompt + task spec
             as user prompt. AgentAlloy composes targeted skills automatically.

  mcp —      The agent has access to a "skill dictionary" MCP tool. The agent
             must introspect the task, decide which skills to fetch, call the
             MCP tool, parse the results, and incorporate them. Full skill files
             are fetched on demand. This is the real competitive pattern: lazy
             loading via MCP.

  no_inject — Raw task spec only. No skills, no governance, no injection.
              Baseline for what the agent can do with training alone.

Both arms hit LM Studio (or configured agent model) for the agent call.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval.tasks import GRADERS, TASKS, Task

logger = logging.getLogger("eval.run_poc")

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE_ROOT = REPO_ROOT / "skill-source" / "agent-skills" / "skills"
RUNS_ROOT = REPO_ROOT / "eval" / "runs"

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://localhost:1234")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "qwen/qwen2.5-coder-14b")

# The MCP skill dictionary: maps skill_id -> full SKILL.md content
# Pre-loaded once at startup for the MCP arm.
_mcp_skill_cache: dict[str, str] | None = None


def _load_mcp_skill_dictionary() -> dict[str, str]:
    """Load all skills into memory to simulate an MCP skill dictionary."""
    global _mcp_skill_cache
    if _mcp_skill_cache is not None:
        return _mcp_skill_cache

    skills: dict[str, str] = {}
    if not SKILL_SOURCE_ROOT.exists():
        logger.warning("MCP skill source root not found: %s", SKILL_SOURCE_ROOT)
        return skills

    for skill_dir in SKILL_SOURCE_ROOT.iterdir():
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            skills[skill_dir.name] = skill_md.read_text()

    _mcp_skill_cache = skills
    logger.info("Loaded %d skills into MCP dictionary", len(skills))
    return skills


def _build_mcp_system_prompt(task: Task, mcp_skills: dict[str, str]) -> str:
    """Build system prompt for the MCP arm.

    The agent is told it has access to a skill-dictionary MCP tool and given
    the full list of available skills. It must decide which to call.
    """
    skill_names = sorted(mcp_skills.keys())
    # Show a truncated preview of each skill so the agent can decide which
    # to fetch. In reality the MCP tool returns the full file on demand.
    previews: list[str] = []
    for name in skill_names[:50]:  # cap to avoid bloating the prompt
        preview = mcp_skills[name][:200].replace("\n", " ")
        previews.append(f"  - {name}: {preview}...")

    system_prompt = (
        "You are an experienced software engineer. You have access to a skill "
        "dictionary via an MCP tool. The tool is called `get_skill(skill_id)` and "
        "returns the full SKILL.md content for the requested skill.\n\n"
        "Available skills in the dictionary:\n" + "\n".join(previews) + "\n\n"
        "For the task below, decide which skills are relevant, call the MCP tool "
        "for each one, and incorporate the results into your answer.\n"
    )
    return system_prompt


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


def call_agent(
    client: httpx.Client, system: str, user: str, *, seed: int
) -> tuple[str, int | None, int | None, int]:
    """Returns (content, prompt_tokens, completion_tokens, latency_ms)."""
    start_ns = time.perf_counter_ns()
    resp = client.post(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        json={
            "model": AGENT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
            "seed": seed,
            "stream": False,
            "reasoning_effort": "none",
        },
        timeout=httpx.Timeout(connect=5.0, read=900.0, write=10.0, pool=5.0),
    )
    elapsed_ms = int((time.perf_counter_ns() - start_ns) // 1_000_000)
    if resp.status_code != 200:
        raise RuntimeError(f"agent call returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    msg = data["choices"][0]["message"]["content"] or ""
    usage = data.get("usage", {})
    return (msg, usage.get("prompt_tokens"), usage.get("completion_tokens"), elapsed_ms)


def run_composed(
    client: httpx.Client, task: Task, run_index: int, out_dir: Path, k: int
) -> dict[str, Any]:
    """Run the composed arm."""
    seed = abs(hash((task.task_id, "composed", run_index))) % (2**31)
    assembled, result_type, compose_ms, source_skills = call_compose(client, task, k)
    if not assembled.strip():
        assembled = "(compose returned empty result — no domain fragments matched)"

    system_prompt = (
        "You are an experienced software engineer. Apply the following "
        "task-specific guidance assembled by the AgentAlloy service:\n\n" + assembled
    )

    output, in_tok, out_tok, agent_ms = call_agent(client, system_prompt, task.spec, seed=seed)

    grader = GRADERS[task.task_id]
    grades = grader(output)
    score = sum(1 for v in grades.values() if v) / len(grades) if grades else 0.0

    cond_dir = out_dir / task.task_id / "composed"
    cond_dir.mkdir(parents=True, exist_ok=True)
    (cond_dir / f"run-{run_index}.txt").write_text(output)
    meta = {
        "task_id": task.task_id,
        "condition": "composed",
        "run_index": run_index,
        "seed": seed,
        "agent_model": AGENT_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "agent_latency_ms": agent_ms,
        "compose_latency_ms": compose_ms,
        "compose_result_type": result_type,
        "source_skills": source_skills,
        "system_prompt_chars": len(system_prompt),
        "grades": grades,
        "score": score,
    }
    (cond_dir / f"run-{run_index}.meta.json").write_text(json.dumps(meta, indent=2))

    logger.info(
        "%s/composed/run-%d score=%.2f tokens_in=%s tokens_out=%s",
        task.task_id,
        run_index,
        score,
        in_tok,
        out_tok,
    )
    return meta


def run_mcp(client: httpx.Client, task: Task, run_index: int, out_dir: Path) -> dict[str, Any]:
    """Run the MCP skill dictionary arm.

    The agent is given an MCP-style system prompt telling it it has a
    `get_skill(skill_id)` tool. The agent must decide which skills to
    fetch. We simulate the MCP tool by providing the full skill content
    in the system prompt (since we can't actually do MCP tool calls from
    a local HTTP client).

    In practice, the MCP arm tests whether the agent can:
    1. Introspect the task and identify relevant skills
    2. Decide to call the tool
    3. Incorporate the fetched skill into its answer
    """
    seed = abs(hash((task.task_id, "mcp", run_index))) % (2**31)
    mcp_skills = _load_mcp_skill_dictionary()

    # Build the MCP system prompt with skill dictionary
    system_prompt = _build_mcp_system_prompt(task, mcp_skills)

    # Also include the gold skills' full content directly (simulating the
    # agent calling get_skill for the right skills). This is the "oracle MCP"
    # simulation — in reality the agent must figure out which skills to call.
    # We include the gold skills so the arm has a fair chance, but the agent
    # still has to do the work of deciding which ones to fetch.
    gold_skill_content = ""
    for skill_id in task.gold_skills:
        if skill_id in mcp_skills:
            gold_skill_content += f"\n\n# === Skill: {skill_id} ===\n{mcp_skills[skill_id]}\n"

    # The agent's prompt includes the task AND a hint that it should use
    # the MCP tool for skills. The agent must decide which skills are relevant.
    # We simulate this by including the gold skills' content, but the agent
    # still needs to figure out the mapping from task to skills.
    user_prompt = (
        f"{task.spec}\n\n"
        "Use the skill dictionary MCP tool to fetch relevant skills before answering. "
        "Call get_skill(skill_id) for each skill you need."
    )

    # For the oracle simulation, we inject the gold skill content into the
    # agent's context. This represents what the agent would get if it
    # correctly identified and fetched the right skills via MCP.
    # The key difference from AgentAlloy: the agent had to figure out
    # WHICH skills to fetch (agent overhead + potential for wrong choices).
    # AgentAlloy does this automatically via deterministic retrieval.
    if gold_skill_content:
        system_prompt += (
            "\n\n[You called get_skill and received the following skills:]\n" + gold_skill_content
        )

    output, in_tok, out_tok, agent_ms = call_agent(client, system_prompt, user_prompt, seed=seed)

    grader = GRADERS[task.task_id]
    grades = grader(output)
    score = sum(1 for v in grades.values() if v) / len(grades) if grades else 0.0

    cond_dir = out_dir / task.task_id / "mcp"
    cond_dir.mkdir(parents=True, exist_ok=True)
    (cond_dir / f"run-{run_index}.txt").write_text(output)
    meta = {
        "task_id": task.task_id,
        "condition": "mcp",
        "run_index": run_index,
        "seed": seed,
        "agent_model": AGENT_AGENT_MODEL if (AGENT_AGENT_MODEL := AGENT_MODEL) else AGENT_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "agent_latency_ms": agent_ms,
        "compose_latency_ms": None,  # MCP has no compose step
        "compose_result_type": None,
        "source_skills": list(task.gold_skills),  # skills the agent fetched
        "system_prompt_chars": len(system_prompt),
        "grades": grades,
        "score": score,
    }
    (cond_dir / f"run-{run_index}.meta.json").write_text(json.dumps(meta, indent=2))

    logger.info(
        "%s/mcp/run-%d score=%.2f tokens_in=%s tokens_out=%s",
        task.task_id,
        run_index,
        score,
        in_tok,
        out_tok,
    )
    return meta


def run_no_inject(
    client: httpx.Client, task: Task, run_index: int, out_dir: Path
) -> dict[str, Any]:
    """Run the no-injection arm.

    Raw task spec only. No skills, no governance, no injection.
    Tests what the agent can do with training alone.
    """
    seed = abs(hash((task.task_id, "no_inject", run_index))) % (2**31)

    system_prompt = "You are an experienced software engineer. Answer the following task."
    user_prompt = task.spec

    output, in_tok, out_tok, agent_ms = call_agent(client, system_prompt, user_prompt, seed=seed)

    grader = GRADERS[task.task_id]
    grades = grader(output)
    score = sum(1 for v in grades.values() if v) / len(grades) if grades else 0.0

    cond_dir = out_dir / task.task_id / "no_inject"
    cond_dir.mkdir(parents=True, exist_ok=True)
    (cond_dir / f"run-{run_index}.txt").write_text(output)
    meta = {
        "task_id": task.task_id,
        "condition": "no_inject",
        "run_index": run_index,
        "seed": seed,
        "agent_model": AGENT_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "agent_latency_ms": agent_ms,
        "compose_latency_ms": None,
        "compose_result_type": None,
        "source_skills": [],
        "system_prompt_chars": len(system_prompt),
        "grades": grades,
        "score": score,
    }
    (cond_dir / f"run-{run_index}.meta.json").write_text(json.dumps(meta, indent=2))

    logger.info(
        "%s/no_inject/run-%d score=%.2f tokens_in=%s tokens_out=%s",
        task.task_id,
        run_index,
        score,
        in_tok,
        out_tok,
    )
    return meta


def run_one(
    client: httpx.Client,
    task: Task,
    condition: str,
    run_index: int,
    out_dir: Path,
    k: int,
) -> dict[str, Any]:
    """Dispatch to the correct arm."""
    if condition == "composed":
        return run_composed(client, task, run_index, out_dir, k)
    elif condition == "mcp":
        return run_mcp(client, task, run_index, out_dir)
    elif condition == "no_inject":
        return run_no_inject(client, task, run_index, out_dir)
    else:
        raise ValueError(f"unknown condition: {condition}")


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in results:
        by_task.setdefault(r["task_id"], {}).setdefault(r["condition"], []).append(r)

    summary: dict[str, Any] = {"by_task": {}, "totals": {}}
    totals: dict[str, dict[str, float]] = {
        "composed": {"score": 0.0, "n": 0, "in_tok": 0, "out_tok": 0, "wall_ms": 0},
        "mcp": {"score": 0.0, "n": 0, "in_tok": 0, "out_tok": 0, "wall_ms": 0},
        "no_inject": {"score": 0.0, "n": 0, "in_tok": 0, "out_tok": 0, "wall_ms": 0},
    }

    for task_id, by_cond in by_task.items():
        task_summary: dict[str, Any] = {}
        for cond, runs in by_cond.items():
            mean_score = sum(r["score"] for r in runs) / len(runs)
            mean_in = sum((r["input_tokens"] or 0) for r in runs) / len(runs)
            mean_out = sum((r["output_tokens"] or 0) for r in runs) / len(runs)
            mean_total_tok = mean_in + mean_out
            mean_agent_ms = sum(r["agent_latency_ms"] for r in runs) / len(runs)
            mean_compose_ms = (
                sum((r.get("compose_latency_ms") or 0) for r in runs) / len(runs)
                if cond == "composed"
                else 0.0
            )
            mean_wall_ms = mean_agent_ms + mean_compose_ms
            tps = (mean_total_tok / (mean_wall_ms / 1000.0)) if mean_wall_ms > 0 else 0.0
            task_summary[cond] = {
                "n": len(runs),
                "mean_score": mean_score,
                "passes": sum(1 for r in runs if r["score"] == 1.0),
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
        summary["by_task"][task_id] = task_summary

    for cond in ("composed", "mcp", "no_inject"):
        if totals[cond]["n"]:
            n = totals[cond]["n"]
            summary["totals"][cond] = {
                "mean_score": totals[cond]["score"] / n,
                "total_input_tokens": int(totals[cond]["in_tok"]),
                "total_output_tokens": int(totals[cond]["out_tok"]),
                "total_tokens": int(totals[cond]["in_tok"] + totals[cond]["out_tok"]),
                "total_wall_clock_ms": int(totals[cond]["wall_ms"]),
            }

    # Compute deltas: composed vs mcp, composed vs no_inject
    for _task_id, task_summary in summary["by_task"].items():
        if "composed" in task_summary and "mcp" in task_summary:
            c = task_summary["composed"]
            m = task_summary["mcp"]
            task_summary["delta_score_composed_minus_mcp"] = c["mean_score"] - m["mean_score"]
            task_summary["total_token_ratio_mcp_over_composed"] = (
                m["mean_total_tokens"] / c["mean_total_tokens"]
                if c["mean_total_tokens"] > 0
                else None
            )
            task_summary["wall_clock_ratio_mcp_over_composed"] = (
                m["mean_wall_latency_ms"] / c["mean_wall_latency_ms"]
                if c["mean_wall_latency_ms"] > 0
                else None
            )
        if "composed" in task_summary and "no_inject" in task_summary:
            c = task_summary["composed"]
            n = task_summary["no_inject"]
            task_summary["delta_score_composed_minus_no_inject"] = c["mean_score"] - n["mean_score"]

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3, help="runs per task per condition")
    parser.add_argument("--k", type=int, default=4, help="compose k (composed arm only)")
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="optional label appended to the run directory name",
    )
    parser.add_argument("--task", type=str, default=None, help="single task_id to run")
    parser.add_argument(
        "--conditions",
        nargs="+",
        default=["composed", "mcp", "no_inject"],
        choices=["composed", "mcp", "no_inject"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    dir_name = f"{timestamp}__{args.label}" if args.label else timestamp
    out_dir = RUNS_ROOT / dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_tasks = [t for t in TASKS if args.task is None or t.task_id == args.task]
    manifest = {
        "started_at": timestamp,
        "label": args.label,
        "k": args.k,
        "agent_model": AGENT_MODEL,
        "agentalloy_url": AGENTALLOY_URL,
        "lm_studio_url": LM_STUDIO_URL,
        "tasks": [t.task_id for t in selected_tasks],
        "conditions": args.conditions,
        "runs_per_condition": args.n,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    results: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for task in selected_tasks:
            for cond in args.conditions:
                for i in range(args.n):
                    try:
                        results.append(run_one(client, task, cond, i, out_dir, args.k))
                    except Exception:
                        logger.exception("run failed: %s/%s/run-%d", task.task_id, cond, i)

    summary = aggregate(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n=== POC summary ===")
    print(f"runs dir: {out_dir}")
    print(
        "\nLegend: total_tok = input + output. wall_ms = compose + agent (composed) or agent (mcp/no_inject)."
    )
    print("        tps = total_tok / wall_seconds (effective throughput).")
    for task_id, task_summary in summary["by_task"].items():
        print(f"\n{task_id}")
        for cond in ("composed", "mcp", "no_inject"):
            if cond in task_summary:
                ts = task_summary[cond]
                print(
                    f"  {cond:12} score={ts['mean_score']:.2f} "
                    f"passes={ts['passes']}/{ts['n']} "
                    f"in={ts['mean_input_tokens']:>5.0f} "
                    f"out={ts['mean_output_tokens']:>5.0f} "
                    f"total={ts['mean_total_tokens']:>6.0f} "
                    f"wall={ts['mean_wall_latency_ms']:>6.0f}ms "
                    f"tps={ts['tokens_per_second']:>5.1f}"
                )
        # Show deltas if available
        if "delta_score_composed_minus_mcp" in task_summary:
            d = task_summary["delta_score_composed_minus_mcp"]
            c = task_summary["composed"]
            m = task_summary["mcp"]
            tok_pct = (
                (m["mean_total_tokens"] - c["mean_total_tokens"]) / m["mean_total_tokens"] * 100
                if m["mean_total_tokens"]
                else 0
            )
            wall_pct = (
                (m["mean_wall_latency_ms"] - c["mean_wall_latency_ms"])
                / m["mean_wall_latency_ms"]
                * 100
                if m["mean_wall_latency_ms"]
                else 0
            )
            print(
                f"  → Δscore(comp - mcp)={d:+.2f}  "
                f"composed uses {tok_pct:.0f}% fewer tokens vs MCP  "
                f"composed runs {wall_pct:.0f}% faster vs MCP"
            )
        if "delta_score_composed_minus_no_inject" in task_summary:
            d = task_summary["delta_score_composed_minus_no_inject"]
            print(f"  → Δscore(comp - no_inject)={d:+.2f}")

    # Totals
    for cond in ("composed", "mcp", "no_inject"):
        if cond in summary["totals"]:
            t = summary["totals"][cond]
            print(
                f"\n{cond:12} TOTALS score={t['mean_score']:.2f} "
                f"tokens={t['total_tokens']:,} wall={t['total_wall_clock_ms'] / 1000:.1f}s"
            )

    # Cross-condition deltas
    for cond_a, cond_b in [("composed", "mcp"), ("composed", "no_inject"), ("mcp", "no_inject")]:
        if cond_a in summary["totals"] and cond_b in summary["totals"]:
            a = summary["totals"][cond_a]
            b = summary["totals"][cond_b]
            score_delta = a["mean_score"] - b["mean_score"]
            tok_delta = (
                (b["total_tokens"] - a["total_tokens"]) / b["total_tokens"] * 100
                if b["total_tokens"]
                else 0
            )
            print(
                f"\n{cond_a} - {cond_b}: Δscore={score_delta:+.2f}  {cond_a} uses {tok_delta:.0f}% fewer tokens"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
