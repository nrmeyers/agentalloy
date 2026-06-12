"""Offline LLM-as-judge over persisted POC run outputs.

Judges the ``run-N.txt`` artifacts that ``run_poc`` writes under
``eval/runs/<label>__<ts>/<task_id>/<condition>/`` using Claude via the
Message Batches API (50% off interactive pricing). The judge is **blind**:
it sees only the task spec and the candidate response — never the
condition, the model that produced it, or any mention of skill injection.

Methodology (pre-registered in eval/campaign-2026-06.md):

* Length-controlled rubric — the judge is explicitly instructed that
  response length must not move scores in either direction (LLM judges
  systematically reward verbosity; composed outputs are shorter).
* Structured outputs (``output_config.format``) so every judgment parses.
* Judge model defaults to Claude Opus 4.8 — outside all four test-model
  families (Qwen/Gemma/LFM), so no self-preference bias.
* The report includes a judge–heuristic-grader agreement stat (Pearson r)
  and a length-bias diagnostic (judge score vs output tokens).

Usage (the ``anthropic`` SDK is not a project dependency — inject it):

    export ANTHROPIC_API_KEY=...
    uv run --with anthropic python -m eval.judge submit eval/runs/<leg> [...]
    uv run --with anthropic python -m eval.judge collect            # latest batch
    uv run python -m eval.judge report eval/runs/<leg> [...]        # no SDK needed

``submit`` writes a mapping file under ``eval/runs/judge-batches/`` keyed by
batch id; ``collect`` polls that batch, writes ``run-N.judge.json`` next to
each output, and prints the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.judge_common import (
    DIMENSIONS,
    EVAL_ROOT,
    JUDGE_PERSONA,
    JUDGE_SCHEMA,
    MAX_POINTS,
    RUBRIC,
    iter_runs,
    render_report,
)
from eval.judge_common import (
    task_specs as _task_specs,
)

BATCHES_DIR = EVAL_ROOT / "runs" / "judge-batches"
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"

# Cloud judge keeps the persona in a system prompt (the local judge folds it
# into the user turn — see eval/judge_local.py). Rubric/schema are shared.
JUDGE_SYSTEM = JUDGE_PERSONA

_iter_runs = iter_runs


def _judge_path(txt_path: Path) -> Path:
    return txt_path.with_name(txt_path.name.replace(".txt", ".judge.json"))


def _build_request(model: str, spec: str, output: str) -> dict[str, Any]:
    prompt = (
        f"## Task specification\n\n{spec}\n\n"
        f"## Candidate response\n\n{output}\n\n"
        f"## Rubric\n\n{RUBRIC}"
    )
    return {
        "model": model,
        "max_tokens": 1500,
        "thinking": {"type": "adaptive"},
        "system": JUDGE_SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    }


def cmd_submit(args: argparse.Namespace) -> int:
    specs = _task_specs()
    run_dirs = [Path(d) for d in args.run_dirs]
    requests: list[dict[str, Any]] = []
    items: dict[str, dict[str, str]] = {}
    skipped_judged = 0
    for txt_path, meta in _iter_runs(run_dirs):
        if not args.force and _judge_path(txt_path).is_file():
            skipped_judged += 1
            continue
        task_id = meta["task_id"]
        if task_id not in specs:
            print(f"warning: unknown task_id {task_id}, skipping {txt_path}", file=sys.stderr)
            continue
        custom_id = f"j{len(requests):05d}"
        requests.append(
            {
                "custom_id": custom_id,
                "params": _build_request(args.model, specs[task_id], txt_path.read_text()),
            }
        )
        items[custom_id] = {"txt": str(txt_path)}
    if not requests:
        print("nothing to judge (all runs already have judge.json? use --force)")
        return 0
    print(f"{len(requests)} judgments to submit ({skipped_judged} already judged, skipped)")

    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        out = BATCHES_DIR / "dry-run.requests.json"
        out.write_text(json.dumps(requests, indent=2))
        print(f"dry run: wrote {out}, nothing submitted")
        return 0

    import anthropic

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)  # type: ignore[arg-type]
    mapping = {
        "batch_id": batch.id,
        "model": args.model,
        "created": datetime.now(UTC).isoformat(),
        "items": items,
    }
    mapping_path = BATCHES_DIR / f"{batch.id}.json"
    mapping_path.write_text(json.dumps(mapping, indent=2))
    print(f"submitted batch {batch.id} ({len(requests)} requests)")
    print(f"mapping: {mapping_path}")
    print("collect with: uv run --with anthropic python -m eval.judge collect")
    return 0


def _latest_mapping() -> Path | None:
    candidates = sorted(
        BATCHES_DIR.glob("msgbatch_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def cmd_collect(args: argparse.Namespace) -> int:
    if args.batch_id:
        mapping_path = BATCHES_DIR / f"{args.batch_id}.json"
    else:
        found = _latest_mapping()
        if found is None:
            print("no submitted batches found under eval/runs/judge-batches/", file=sys.stderr)
            return 1
        mapping_path = found
    mapping = json.loads(mapping_path.read_text())
    batch_id: str = mapping["batch_id"]

    import anthropic

    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        counts = batch.request_counts
        print(
            f"batch {batch_id} still {batch.processing_status}: "
            f"{counts.succeeded} succeeded, {counts.processing} processing, "
            f"{counts.errored} errored — try again later"
        )
        return 2

    written = 0
    failed: list[str] = []
    touched_dirs: set[Path] = set()
    for entry in client.messages.batches.results(batch_id):
        item = mapping["items"].get(entry.custom_id)
        if item is None:
            continue
        txt_path = Path(item["txt"])
        if entry.result.type != "succeeded":
            failed.append(f"{entry.custom_id} ({entry.result.type}): {txt_path}")
            continue
        message = entry.result.message
        text = next(b.text for b in message.content if b.type == "text")
        scores = json.loads(text)
        judgment = {
            **scores,
            "judge_score": sum(scores[d] for d in DIMENSIONS) / MAX_POINTS,
            "judge_model": mapping["model"],
            "batch_id": batch_id,
        }
        _judge_path(txt_path).write_text(json.dumps(judgment, indent=2))
        touched_dirs.add(txt_path.parents[2])
        written += 1
    print(f"wrote {written} judgments")
    if failed:
        print(f"{len(failed)} requests did not succeed:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
    if touched_dirs:
        _report(sorted(touched_dirs))
    return 1 if failed else 0


def _report(run_dirs: list[Path]) -> None:
    # rows: (run_dir, task_id, condition, run_index, judge_score, heuristic, out_tokens)
    rows: list[tuple[Path, str, str, int, float, float, int | None]] = []
    for txt_path, meta in _iter_runs(run_dirs):
        judge_path = _judge_path(txt_path)
        if not judge_path.is_file():
            continue
        judgment = json.loads(judge_path.read_text())
        rows.append(
            (
                txt_path.parents[2],
                meta["task_id"],
                meta["condition"],
                meta["run_index"],
                judgment["judge_score"],
                meta["score"],
                meta.get("output_tokens"),
            )
        )
    render_report(rows)


def cmd_report(args: argparse.Namespace) -> int:
    _report([Path(d) for d in args.run_dirs])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="build and submit a judging batch")
    p_submit.add_argument("run_dirs", nargs="+", help="run dirs under eval/runs/")
    p_submit.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    p_submit.add_argument("--force", action="store_true", help="re-judge already-judged runs")
    p_submit.add_argument("--dry-run", action="store_true", help="write requests, don't submit")
    p_submit.set_defaults(func=cmd_submit)

    p_collect = sub.add_parser("collect", help="fetch results, write judge files, report")
    p_collect.add_argument("--batch-id", help="default: most recently submitted batch")
    p_collect.set_defaults(func=cmd_collect)

    p_report = sub.add_parser("report", help="aggregate existing judge files (no API)")
    p_report.add_argument("run_dirs", nargs="+", help="run dirs under eval/runs/")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
