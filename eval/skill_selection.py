"""v2 skill-selection eval — does the interpreter pick the gold skill?

v2 has no embedding retrieval of skills: on ``POST /compose`` the interpreter
(MiniCPM5-2B) reads the pack catalog, picks packs, then skills, and calls
``assemble_skill``. This eval drives that real path against a live
``agentalloy serve`` and scores, for each task in
``eval.domain_tasks.DOMAIN_TASKS``, whether a gold skill ends up in the
response's ``source_skills``.

Per task, in its own project scope (so state from one task cannot steer the
next):

1. Walk the project from ``intake`` to the task's phase through ``/tool``
   (record the ``{phase}-exit`` artifact, then ``phase_advance`` with the
   approver header). Every step must return ``ok``.
2. ``POST /compose`` with the task spec (``new_session``, per-task
   ``session_key``) and assert the response's ``phase`` equals the task's
   phase — the eval must measure selection at the phase it claims to.
3. Record hit, ``source_skills``, ``stop_reason``, tool-call count, seconds.

Some tasks' gold skills are scoped to phases that exclude the task's phase
(pinned in ``KNOWN_PHASE_MISMATCH``, guarded by
``tests/test_corpus_integrity.py``). ``assemble_skill`` drops skills outside
the phase the interpreter passes it, so those tasks hit only when the model
passes another phase or none — the report carries both the overall score and
the score over phase-matched tasks. Eval projects are not
registered repos, so the catalog is not stack-filtered.

Exit codes: 0 = ran (the comparator judges quality), 2 = infrastructure or
setup failure (service down, a phase walk rejected, a phase mismatch, an
HTTP error). Writes ``eval/runs/skill-selection-<ts>.json``.

Env: ``AGENTALLOY_URL`` (default http://127.0.0.1:48961, the pre-PR port — never
the live :48950 service),
``AGENTALLOY_APPROVER_TOKEN`` (must match the service's).

Normally run through ``scripts/pre-pr-eval.sh`` on the dev server, which
starts an isolated service against the GPU interpreter and judges the report
against ``eval/selection_baselines.json``.

Usage::

    uv run python -m eval.skill_selection [--only domain_1_webhook_signature ...]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from eval.domain_tasks import DOMAIN_TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://127.0.0.1:48961")
APPROVER = os.environ.get("AGENTALLOY_APPROVER_TOKEN", "")
PHASE_ORDER = ("intake", "spec", "design", "plan", "build", "qa", "ship")
REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "eval" / "runs"

# Tasks whose gold skills are scoped to phases that exclude the task's phase.
# assemble_skill drops skills outside the phase the interpreter passes, so
# these hit only when the model passes another phase (or none), until the
# skills' phase_scope is widened. tests/test_corpus_integrity.py pins this
# set, so it can only change deliberately.
KNOWN_PHASE_MISMATCH: frozenset[str] = frozenset(
    {
        "domain_3_webhook_dlq",
        "domain_4_webhook_versioning",
        "domain_5_temporal_workflow_determinism",
        "domain_8_scd_type2",
        "domain_15_snowflake_warehouse_cost",
        "domain_18_redshift_table_design",
    }
)


class SetupError(RuntimeError):
    """The eval could not measure (as opposed to measuring a low score)."""


def _tool(cli: httpx.Client, project: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    headers = {"X-AgentAlloy-Approver": APPROVER} if APPROVER else {}
    resp = cli.post(
        f"{AGENTALLOY_URL}/tool",
        json={"name": name, "args": json.dumps(args), "project": project},
        headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise SetupError(f"{project}: /tool {name} failed: {body.get('error')}")
    result = (
        json.loads(body["result"]) if isinstance(body.get("result"), str) else body.get("result")
    )
    return result if isinstance(result, dict) else {"result": result}


def walk_to(cli: httpx.Client, project: str, phase: str) -> None:
    """Advance a fresh project from intake to ``phase`` one gate at a time."""
    for current, target in zip(PHASE_ORDER, PHASE_ORDER[1:], strict=False):
        if current == phase:
            return
        rec = _tool(
            cli,
            project,
            "artifact_record",
            {
                "phase": current,
                "name": f"{current}-exit",
                "body": f"Skill-selection eval scaffold: {current} complete; walking to {phase}.",
            },
        )
        if rec.get("status") not in (None, "ok", "recorded"):
            raise SetupError(f"{project}: artifact_record({current}) → {rec}")
        adv = _tool(cli, project, "phase_advance", {"target": target, "approved": True})
        if adv.get("status") != "ok":
            raise SetupError(f"{project}: phase_advance {current}→{target} → {adv}")
    if phase != PHASE_ORDER[-1]:
        raise SetupError(f"{project}: unknown phase {phase!r}")


def run(only: list[str] | None, run_id: str) -> dict[str, Any]:
    tasks = [t for t in DOMAIN_TASKS if not only or t.task_id in only]
    # One compose = up to 6 model steps + a brief follow-up; on CPU each step
    # can take minutes.
    timeout = httpx.Timeout(connect=10.0, read=3600.0, write=30.0, pool=10.0)
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout) as cli:
        health = cli.get(f"{AGENTALLOY_URL}/health")
        if health.status_code != 200:
            raise SetupError(f"service unhealthy: {health.status_code} {health.text[:200]}")
        for task in tasks:
            project = f"eval-{run_id}-{task.task_id}"
            walk_to(cli, project, task.phase)
            started = time.monotonic()
            resp = cli.post(
                f"{AGENTALLOY_URL}/compose",
                json={
                    "prompt": task.spec,
                    "project": project,
                    "session_key": project,
                    "new_session": True,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            seconds = round(time.monotonic() - started, 1)
            if body.get("phase") != task.phase:
                raise SetupError(
                    f"{task.task_id}: compose ran in phase {body.get('phase')!r}, expected {task.phase!r}"
                )
            got = list(body.get("source_skills") or [])
            gold = list(task.gold_skills)
            hit = any(g in got for g in gold)
            matched = task.task_id not in KNOWN_PHASE_MISMATCH
            print(
                f"  {'HIT ' if hit else 'MISS'}  {task.task_id:<40} {task.phase:<6} "
                f"{'' if matched else '(phase-mismatch) '}{seconds:>6}s  "
                f"stop={body.get('stop_reason')} tools={body.get('tool_calls')} got={got}",
                flush=True,
            )
            results.append(
                {
                    "task_id": task.task_id,
                    "phase": task.phase,
                    "gold": gold,
                    "source_skills": got,
                    "hit": hit,
                    "phase_matched": matched,
                    "stop_reason": body.get("stop_reason"),
                    "tool_calls": body.get("tool_calls"),
                    "seconds": seconds,
                }
            )
    matched = [r for r in results if r["phase_matched"]]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "url": AGENTALLOY_URL,
        "total": len(results),
        "hits": sum(r["hit"] for r in results),
        "matched_total": len(matched),
        "matched_hits": sum(r["hit"] for r in matched),
        "errors": sum(r["stop_reason"] == "error" for r in results),
        "seconds": round(sum(r["seconds"] for r in results), 1),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--only", nargs="*", help="task ids to run (default: all)")
    parser.add_argument("--out", type=Path, default=None, help="report path")
    args = parser.parse_args(argv)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        report = run(args.only, run_id)
    except (SetupError, httpx.HTTPError) as exc:
        print(f"skill-selection: SETUP FAILURE — {exc}", file=sys.stderr)
        return 2
    out = args.out or RUNS_ROOT / f"skill-selection-{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"skill-selection: {report['hits']}/{report['total']} "
        f"(phase-matched {report['matched_hits']}/{report['matched_total']}), "
        f"{report['errors']} interpreter errors, {report['seconds']}s"
    )
    print(f"wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
