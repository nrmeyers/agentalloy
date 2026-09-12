"""Score a results.jsonl against the gate table.

Usage:
    python -m newagent.score [--results results/results.jsonl] [--config config.toml]
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_results(path: Path) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def is_fabricated(rec: dict, request: str) -> bool:
    """Args were valid JSON but an exact-expected value appears nowhere in
    the request — the model made it up rather than mishearing the task."""
    if not rec.get("a_ok") or rec.get("args_ok"):
        return False
    text = request.lower()
    for err in rec.get("args_errors", []):
        # error strings look like: "slug: expected 'sdd-build', got 'sdd-qa'"
        # (unquoted forms like "k: expected 5, got None" are skipped)
        if "expected '" not in err:
            continue
        expected = err.split("expected '", 1)[1].split("'", 1)[0].strip()
        if expected and expected.lower() not in text:
            return True
    return False


def score(recs: list[dict], gates: dict, requests: dict[str, str]) -> int:
    steps = len(recs)
    by_task: dict[str, list[dict]] = {}
    for r in recs:
        by_task.setdefault(r["task_id"], []).append(r)

    a_ok = sum(1 for r in recs if r.get("a_ok"))
    classified = [r for r in recs if r.get("a_ok")]
    fill_ok = sum(1 for r in classified if r["expected"] == "none" or r.get("args_ok"))
    loop_recs = [r for r in recs if r["suite"] == "loop"]
    loop_tasks = {tid: rs for tid, rs in by_task.items() if rs[0]["suite"] == "loop"}
    loop_full = sum(1 for rs in loop_tasks.values() if all(r.get("step_ok") for r in rs))
    false_actions = sum(1 for r in recs if r.get("false_action"))
    fabricated = sum(1 for r in recs if is_fabricated(r, requests.get(r["task_id"], "")))
    hallu = (false_actions + fabricated) / steps if steps else 0.0

    classification = a_ok / steps if steps else 0.0
    argument_fill = fill_ok / len(classified) if classified else 0.0
    loop_two_step = loop_full / len(loop_tasks) if loop_tasks else 0.0

    rows = [
        ("classification ≥ 0.85", classification, "floor", gates.get("classification", 0.85)),
        ("argument_fill  ≥ 0.95", argument_fill, "floor", gates.get("argument_fill", 0.95)),
        ("loop_two_step  ≥ 0.70", loop_two_step, "floor", gates.get("loop_two_step", 0.70)),
        ("hallucination  < 0.05", hallu, "ceil", gates.get("hallucination_max", 0.05)),
    ]
    print(f"steps={steps}  tasks={len(by_task)}  "
          f"loop_tasks={len(loop_tasks)}  false_actions={false_actions}  fabricated={fabricated}")
    print()
    all_pass = True
    for name, measured, kind, gate in rows:
        passed = measured >= gate if kind == "floor" else measured < gate
        all_pass &= passed
        print(f"  {'PASS' if passed else 'FAIL'}  {name}   measured={measured:.3f}")
    print()

    # Per-suite classification accuracy.
    by_suite: dict[str, list[dict]] = {}
    for r in recs:
        by_suite.setdefault(r["suite"], []).append(r)
    print("per-suite classification accuracy:")
    for suite in sorted(by_suite):
        rs = by_suite[suite]
        acc = sum(1 for r in rs if r.get("a_ok")) / len(rs)
        print(f"  {suite:<12} {acc:.3f}  ({sum(1 for r in rs if r.get('a_ok'))}/{len(rs)})")
    print()

    # Top confusions.
    conf = Counter(
        (r["expected"], r.get("a_final"))
        for r in recs
        if r.get("a_final") is not None and not r.get("a_ok")
    )
    if conf:
        print("top confusions (expected → got):")
        for (exp, got), n in conf.most_common(8):
            print(f"  {exp:<18} → {got:<18} ×{n}")
        print()

    # Failures with detail.
    fails = [r for r in recs if not r.get("step_ok")]
    if fails:
        print(f"step failures ({len(fails)}):")
        for r in fails:
            detail = r.get("error") or r.get("args_errors") or r.get("b_errors") or \
                f"got {r.get('a_final')!r}"
            print(f"  {r['task_id']} s{r['step']} exp={r['expected']} got={r.get('a_final')} "
                  f"args_ok={r.get('args_ok')} :: {str(detail)[:120]}")
    print()
    print("GATES:", "ALL PASS" if all_pass else "NOT MET")
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newagent.score")
    ap.add_argument("--results", default=str(ROOT / "results" / "results.jsonl"))
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--tasks", default=str(ROOT / "tasks" / "tasks.jsonl"))
    args = ap.parse_args(argv)

    cfg = tomllib.load(open(Path(args.config), "rb"))
    gates = cfg.get("gates", {})
    recs = load_results(Path(args.results))
    requests = {}
    for line in open(Path(args.tasks)):
        if line.strip():
            t = json.loads(line)
            requests[t["id"]] = t["request"]
    return score(recs, gates, requests)


if __name__ == "__main__":
    raise SystemExit(main())
