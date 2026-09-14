"""Run the classify → fill → validate → retry loop over the task set.

Usage:
    python -m newagent.run [--config config.toml] [--tasks tasks/tasks.jsonl]
                           [--suite SUITE] [--ids a,b] [--limit N]
                           [--out results.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from . import protocol
from .client import Client, LMError
from .validate import check_args, validate

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_tasks(path: Path) -> list[dict]:
    tasks = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def run_step(client: Client, task: dict, step_idx: int, step: dict, max_retries: int) -> dict:
    """Classify (with optional retry), then fill (with optional retry)."""
    expected = step["expect"]
    check = step.get("args_check", {})
    rec: dict = {
        "task_id": task["id"],
        "suite": task["suite"],
        "step": step_idx,
        "expected": expected,
    }

    # ---- Stage A: classify -------------------------------------------------
    if step_idx == 0:
        msgs = protocol.stage_a_messages(task["request"])
    else:
        msgs = protocol.loop_step_messages(
            task["request"], step.get("_prev_call") or "{}", task["observation"]
        )

    raw_a = None
    action = None
    first_action = None
    for attempt in range(1 + max_retries):
        m = msgs if attempt == 0 else protocol.stage_a_retry_messages(msgs, raw_a or "(empty)")
        try:
            resp = client.chat(m, protocol.ACTION_SCHEMA)
        except LMError as exc:
            rec["error"] = f"classify: {exc}"
            action = None
        else:
            raw_a = resp["content"]
            if attempt == 0:
                rec["a_raw"] = raw_a
                rec["a_latency_ms"] = resp["latency_ms"]
                rec["a_tokens"] = resp["completion_tokens"]
            else:
                rec["a_retried"] = True
                rec["a_retry_raw"] = raw_a
                rec["a_latency_ms"] = rec.get("a_latency_ms", 0) + resp["latency_ms"]
                rec["a_tokens"] = rec.get("a_tokens", 0) + resp["completion_tokens"]
            parsed = protocol.parse_json(raw_a)
            action = parsed.get("action") if isinstance(parsed, dict) else None
            if first_action is None:
                first_action = action
            if action in protocol.ACTIONS:
                break
    if first_action is not None:
        rec["a_first"] = first_action
        rec["a_first_ok"] = first_action == expected
    if rec.get("a_retried"):
        rec["a_retry"] = action

    rec["a_final"] = action
    rec["a_ok"] = action == expected
    if expected == "none" and action in protocol.ACTIONS and action != "none":
        rec["false_action"] = True

    # ---- Stage B: fill arguments (skipped for "none") -----------------------
    rec["b_valid"] = None
    rec["args_ok"] = None
    if action in protocol.ACTIONS and action != "none":
        schema = protocol.ARG_SCHEMAS[action]
        msgs_b = protocol.stage_b_messages(msgs, raw_a, action)
        b_parsed, b_errs = None, []
        raw_b = None
        for attempt in range(1 + max_retries):
            m_b = (
                msgs_b
                if attempt == 0
                else protocol.stage_b_retry_messages(
                    msgs_b, raw_b or "(empty)", action, b_errs[0] if b_errs else "(previous output unusable)"
                )
            )
            try:
                resp_b = client.chat(m_b, schema)
            except LMError as exc:
                rec["error"] = f"fill: {exc}"
                b_parsed, b_errs = None, [str(exc)]
                raw_b = None
            else:
                raw_b = resp_b["content"]
                if attempt == 0:
                    rec["b_raw"] = raw_b
                    rec["b_latency_ms"] = resp_b["latency_ms"]
                    rec["b_tokens"] = resp_b["completion_tokens"]
                else:
                    rec["b_retried"] = True
                    rec["b_retry_raw"] = raw_b
                    rec["b_latency_ms"] = rec.get("b_latency_ms", 0) + resp_b["latency_ms"]
                    rec["b_tokens"] = rec.get("b_tokens", 0) + resp_b["completion_tokens"]
                b_parsed = protocol.parse_json(raw_b)
                b_errs = validate(b_parsed, schema)
            if not b_errs:
                break

        rec["b_valid"] = not b_errs
        rec["args"] = b_parsed if (isinstance(b_parsed, dict) and not b_errs) else None
        if b_errs:
            rec["b_errors"] = b_errs[:5]
        if action == expected:
            a_errs = check_args(rec["args"] or {}, check)
            rec["args_ok"] = not a_errs
            if a_errs:
                rec["args_errors"] = a_errs
    elif action == "none":
        pass
    # unparseable action after retry: a_ok already False, no fill possible

    rec["step_ok"] = rec["a_ok"] and (rec["args_ok"] if expected != "none" else True)
    call = {"action": action}
    if rec.get("args"):
        call["args"] = rec["args"]
    rec["_prev_call"] = json.dumps(call)
    return rec


def run_task(client: Client, task: dict, max_retries: int) -> list[dict]:
    """Run all steps of a task; thread the previous call into loop steps."""
    prev_call = "{}"
    records = []
    for i, step in enumerate(task["steps"]):
        s = dict(step)
        s["_prev_call"] = prev_call
        rec = run_step(client, task, i, s, max_retries)
        if rec.get("_prev_call"):
            prev_call = rec["_prev_call"]
        rec.pop("_prev_call", None)
        records.append(rec)
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newagent.run")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--tasks", default=str(ROOT / "tasks" / "tasks.jsonl"))
    ap.add_argument("--suite", help="only run this suite (clean|fill|confusable|negative|loop|phase_class)")
    ap.add_argument("--ids", help="comma-separated task ids")
    ap.add_argument("--limit", type=int, help="stop after N tasks")
    ap.add_argument("--out", default=str(ROOT / "results" / "results.jsonl"))
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    tasks = load_tasks(Path(args.tasks))
    if args.suite:
        tasks = [t for t in tasks if t["suite"] == args.suite]
    if args.ids:
        want = {s.strip() for s in args.ids.split(",")}
        tasks = [t for t in tasks if t["id"] in want]
    if args.limit:
        tasks = tasks[: args.limit]

    api_key = cfg.get("api_key", "not-needed")
    if api_key == "env":
        api_key = os.environ.get("OPENAI_API_KEY", "")

    client = Client(
        endpoint=cfg["endpoint"],
        model=cfg["model"],
        api_key=api_key,
        temperature=cfg.get("temperature", 0.0),
        max_tokens=cfg.get("max_tokens", 512),
        timeout_s=cfg.get("timeout_s", 120),
    )
    max_retries = int(cfg.get("max_retries", 1))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_steps = sum(len(t["steps"]) for t in tasks)
    print(f"running {len(tasks)} tasks / {total_steps} steps against {cfg['endpoint']} "
          f"({cfg['model']})", file=sys.stderr)

    n = 0
    with open(out_path, "w") as fh:
        for task in tasks:
            for rec in run_task(client, task, max_retries):
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                n += 1
                mark = "OK " if rec.get("step_ok") else "FAIL"
                extra = rec.get("error") or ""
                print(f"[{n}/{total_steps}] {mark} {rec['task_id']} s{rec['step']} "
                      f"exp={rec['expected']} got={rec.get('a_final')} {extra}",
                      file=sys.stderr)
    print(f"done: {n} steps → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
