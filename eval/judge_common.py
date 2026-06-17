"""Shared pieces for the LLM-as-judge variants (cloud batch + local server).

The cloud judge (:mod:`eval.judge`, Anthropic Batches) and the local judge
(:mod:`eval.judge_local`, an OpenAI-compatible llama-server) score the same
``run-N.txt`` artifacts against the same rubric and aggregate with the same
report math. The only things that differ are *transport* (batch API vs
synchronous HTTP) and *prompt shape* (system+user with structured-output
enforcement vs a single user message with prompted JSON). Everything else —
rubric text, score schema, run discovery, and the report stats — lives here so
the two front-ends cannot drift.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parent

# --- Rubric -----------------------------------------------------------------
# The *content* of the rubric is identical across judges. The cloud judge puts
# the persona/length-control instruction in a system prompt; the local judge
# folds it into the user turn (a single-user-turn prompt kept stable across
# runs). Keep both halves here so the wording stays in lockstep.

JUDGE_PERSONA = (
    "You are an impartial evaluator of responses to software-engineering "
    "tasks. You will be given a task specification and one candidate "
    "response. Score the response on the rubric provided. Be strict and "
    "consistent.\n\n"
    "CRITICAL: response length must not influence any score in either "
    "direction. A short response that fully satisfies the task earns full "
    "marks; a long response earns nothing for its length. Judge content "
    "only."
)

RUBRIC = """Score the response on three dimensions, each an integer 0-5:

- correctness: technical accuracy. 5 = every claim, code fragment, and
  recommendation is sound; 0 = fundamentally wrong or misleading.
- coverage: how completely the response addresses every explicit
  requirement in the task. 5 = all requirements met; 0 = misses the point.
- precision: how specific and actionable the content is. 5 = concrete,
  task-specific guidance or code; 0 = generic filler that could answer any
  task. This measures specificity, NOT brevity — do not penalize or reward
  length.

Also write a one-or-two-sentence rationale."""

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "correctness": {"type": "integer", "minimum": 0, "maximum": 5},
        "coverage": {"type": "integer", "minimum": 0, "maximum": 5},
        "precision": {"type": "integer", "minimum": 0, "maximum": 5},
        "rationale": {"type": "string"},
    },
    "required": ["correctness", "coverage", "precision", "rationale"],
    "additionalProperties": False,
}

DIMENSIONS = ("correctness", "coverage", "precision")
MAX_POINTS = 15  # 3 dimensions x 5


def task_specs() -> dict[str, str]:
    from eval.domain_tasks import DOMAIN_TASKS
    from eval.tasks import TASKS

    return {t.task_id: t.spec for t in [*TASKS, *DOMAIN_TASKS]}


# --- Run discovery ----------------------------------------------------------


def iter_runs(run_dirs: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    """Yield ``(txt_path, meta)`` for every persisted run under the given dirs."""
    found: list[tuple[Path, dict[str, Any]]] = []
    for run_dir in run_dirs:
        for meta_path in sorted(run_dir.glob("*/*/run-*.meta.json")):
            txt_path = meta_path.with_name(meta_path.name.replace(".meta.json", ".txt"))
            if not txt_path.is_file():
                print(f"warning: meta without output, skipping: {meta_path}", file=sys.stderr)
                continue
            found.append((txt_path, json.loads(meta_path.read_text())))
    return found


# --- Robust JSON parsing of free-form model replies -------------------------


def strip_think(text: str) -> str:
    """Remove a leading R1-style ``<think>...</think>`` block from ``text``.

    The local server is launched with ``--reasoning-format deepseek``, which is
    *supposed* to route reasoning into ``reasoning_content`` and leave ``content``
    clean. We strip defensively in case a build leaks the tags into ``content``.
    Only a balanced opening/closing pair is removed; unmatched tags are left so
    the caller still sees something to debug.
    """
    out = text
    while True:
        start = out.find("<think>")
        if start == -1:
            break
        end = out.find("</think>", start)
        if end == -1:
            break
        out = out[:start] + out[end + len("</think>") :]
    return out


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract and parse the first *balanced* top-level JSON object in ``text``.

    Models often wrap JSON in prose or markdown fences. We scan for the first
    ``{`` and walk forward tracking brace depth (string-aware, so braces inside
    string literals don't count) until the matching ``}``. Returns ``None`` if no
    balanced object parses.
    """
    s = strip_think(text)
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try the next '{'
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = s.find("{", start + 1)
    return None


def coerce_verdict(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a parsed object against the rubric schema and compute the score.

    Returns the verdict dict (dimensions + rationale + ``judge_score``) or
    ``None`` if any dimension is missing or out of the 0-5 integer range.
    """
    scores: dict[str, int] = {}
    for dim in DIMENSIONS:
        val = obj.get(dim)
        if isinstance(val, bool) or not isinstance(val, int):
            # tolerate "4" / 4.0 from a loose model, reject anything else
            try:
                val = int(val)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        if not 0 <= val <= 5:
            return None
        scores[dim] = val
    rationale = obj.get("rationale", "")
    return {
        **scores,
        "rationale": str(rationale),
        "judge_score": sum(scores.values()) / MAX_POINTS,
    }


# --- Report math (shared by both judges) ------------------------------------

# A report row, condition-agnostic:
#   (run_dir, task_id, condition, run_index, judge_score, heuristic, out_tokens)
ReportRow = tuple[Path, str, str, int, float, float, "int | None"]
"""Alias for one row consumed by :func:`render_report`."""


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def bootstrap_ci(deltas: list[float], n_resamples: int = 10_000) -> tuple[float, float]:
    rng = random.Random(0)
    means = sorted(statistics.fmean(rng.choices(deltas, k=len(deltas))) for _ in range(n_resamples))
    return means[int(0.025 * n_resamples)], means[int(0.975 * n_resamples)]


def render_report(rows: list[ReportRow], *, title: str = "LLM-judge report") -> None:
    """Print per-condition means, paired bootstrap CIs, and the two diagnostics."""
    if not rows:
        print("no judged runs found")
        return

    conditions = sorted({r[2] for r in rows})
    print(f"\n=== {title} ({len(rows)} judged runs) ===\n")
    print(f"{'condition':<12} {'n':>4} {'judge':>7} {'heuristic':>10}")
    for cond in conditions:
        sub = [r for r in rows if r[2] == cond]
        print(
            f"{cond:<12} {len(sub):>4} "
            f"{statistics.fmean(r[4] for r in sub):>7.3f} "
            f"{statistics.fmean(r[5] for r in sub):>10.3f}"
        )

    # Paired per-run deltas: composed vs each other arm, same run_dir+task+run.
    by_key = {(r[0], r[1], r[3], r[2]): r[4] for r in rows}
    if "composed" in conditions:
        print("\npaired deltas (composed − other, same task+seed; bootstrap 95% CI):")
        for cond in conditions:
            if cond == "composed":
                continue
            deltas = [
                score - by_key[(d, t, i, cond)]
                for (d, t, i, c), score in by_key.items()
                if c == "composed" and (d, t, i, cond) in by_key
            ]
            if not deltas:
                continue
            lo, hi = bootstrap_ci(deltas)
            print(
                f"  vs {cond:<10} mean {statistics.fmean(deltas):+.3f} "
                f"[{lo:+.3f}, {hi:+.3f}]  (n={len(deltas)} pairs)"
            )

    agreement = pearson([r[4] for r in rows], [r[5] for r in rows])
    if agreement is not None:
        print(f"\njudge–heuristic agreement: Pearson r = {agreement:.3f}")
    token_rows = [(r[4], float(r[6])) for r in rows if r[6] is not None]
    length_bias = pearson([t[1] for t in token_rows], [t[0] for t in token_rows])
    if length_bias is not None:
        print(f"length-bias diagnostic:    Pearson r(judge, output_tokens) = {length_bias:.3f}")
        if abs(length_bias) > 0.4:
            print("  ^ judge scores correlate strongly with length — inspect before trusting")
    print()
