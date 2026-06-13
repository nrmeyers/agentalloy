"""Pairwise LLM-as-judge for code tasks, running against the *local* server.

This is a sibling to the scalar judge (:mod:`eval.judge_local`). It scores the
same ``run-N.txt`` artifacts with the same local server (AceReason-Nemotron-14B,
``JUDGE_URL`` default ``http://127.0.0.1:60002``) and reuses every transport,
checkpointing, and JSON-extraction primitive from
:mod:`eval.judge_local` / :mod:`eval.judge_common`. Only the *unit of judgment*
and the *aggregation* differ.

Why pairwise. CodeJudgeBench finds that for code-task judging, asking the model
to *compare two responses* is materially more accurate than asking it to assign
a scalar rubric score to each in isolation. It also finds **position bias** is
large enough to flip verdicts, so every pair is judged twice with the A/B order
swapped and the two verdicts reconciled:

* **Agreement** (the same condition wins both orders, once un-swapped) -> that
  condition is the winner.
* **Disagreement** (the *positional* "A" wins both times, i.e. swapping the
  order flipped the verdict) -> recorded as a ``position_flip``. Flips count as
  a tie in win-rates but their rate is tracked separately: it is the headline
  judge-quality diagnostic.
* Either side declaring an explicit ``tie`` reconciles to a tie.

The judge is **blind to condition names**: prompts only ever say "RESPONSE A" /
"RESPONSE B". The mapping from positional A/B to condition is kept out of the
model's sight and applied only during reconciliation.

Usage::

    # judge every task x run present in BOTH legs of each pair (resumable):
    uv run python -m eval.judge_local pairwise eval/runs/<leg> [...] \\
        --pairs composed:none,composed:flat [--sample K] [--limit N]

    # aggregate whatever has been judged so far (no server needed):
    uv run python -m eval.judge_local pairwise-report eval/runs/<leg> [...] \\
        --pairs composed:none [--verdicts .../pairwise.jsonl]

Checkpointing mirrors the scalar judge: every judged *order* is appended (then
``fsync``'d) to ``eval/runs/judge-pairwise/<ts>/pairwise.jsonl`` as its own
record, keyed ``(run_dir, task, pair, run, order)``. A kill mid-pair resumes
cleanly — the surviving order is reused and only the missing one is re-judged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from eval.judge_common import (
    EVAL_ROOT,
    JUDGE_PERSONA,
    extract_json_object,
    strip_think,
    task_specs,
)
from eval.judge_local import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_JUDGE_URL,
    DEFAULT_TIMEOUT_S,
    LocalJudgeClient,
    _fmt_eta,
)

PAIRWISE_DIR = EVAL_ROOT / "runs" / "judge-pairwise"

# Two model calls per comparison unit (A/B then B/A).
CALLS_PER_UNIT = 2
EST_SECONDS_PER_CALL = 45.0

Order = Literal["AB", "BA"]
PosWinner = Literal["A", "B", "tie"]

RETRY_NUDGE = (
    "Your previous reply was not valid JSON. Reply with only the JSON object, nothing else."
)


# --- Prompt (condition-blind) -----------------------------------------------


PAIRWISE_PERSONA = (
    "You are an impartial evaluator comparing two candidate responses to a "
    "software-engineering task. You will be given the task specification and "
    "two responses, RESPONSE A and RESPONSE B. Decide which response better "
    "satisfies the task — judging correctness, how completely it covers the "
    "task's explicit requirements, and how specific and actionable it is.\n\n"
    "CRITICAL: response length must not influence your verdict in either "
    "direction. A short response that fully satisfies the task beats a long one "
    "that does not. Judge content only. If the two are genuinely "
    'indistinguishable in quality, answer "tie".'
)


def build_pairwise_prompt(spec: str, response_a: str, response_b: str) -> str:
    """The whole comparison prompt as a single user turn (no system message).

    Persona + length-control lead (sharing wording with the scalar judge), then
    the task, the two *anonymous* candidates, and an explicit JSON-only output
    contract. Condition names never appear — only "A" / "B".
    """
    # JUDGE_PERSONA's length-control sentence is echoed via PAIRWISE_PERSONA; we
    # keep a reference to the scalar persona only so the two stay discoverable
    # together when wording is revised.
    _ = JUDGE_PERSONA
    return (
        f"{PAIRWISE_PERSONA}\n\n"
        f"## Task specification\n\n{spec}\n\n"
        f"## RESPONSE A\n\n{response_a}\n\n"
        f"## RESPONSE B\n\n{response_b}\n\n"
        "## Output format\n\n"
        "Reply with ONLY a JSON object matching this shape, and nothing else "
        "(no markdown fence, no commentary):\n"
        '{"winner": "A" | "B" | "tie", "reason": "<one line>"}'
    )


def coerce_pairwise(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a parsed comparison object. Returns ``{winner, reason}`` or None.

    ``winner`` must be exactly ``"A"``, ``"B"``, or ``"tie"`` (case/space
    tolerant). Anything else is treated as a parse failure so the caller retries
    or records a ``parse_error`` rather than silently mis-scoring.
    """
    winner = obj.get("winner")
    if not isinstance(winner, str):
        return None
    w = winner.strip().lower()
    if w in ("a", "b"):
        norm: PosWinner = w.upper()  # type: ignore[assignment]
    elif w == "tie":
        norm = "tie"
    else:
        return None
    return {"winner": norm, "reason": str(obj.get("reason", ""))}


# --- A single one-order judgment (with one retry) ---------------------------


def judge_pair_once(
    client: LocalJudgeClient, spec: str, response_a: str, response_b: str
) -> tuple[dict[str, Any] | None, str]:
    """Compare two responses in a *fixed* A/B order. Returns ``(verdict, raw)``.

    ``verdict`` is ``{"winner": "A"|"B"|"tie", "reason": str}`` or ``None`` after
    two failed attempts. Mirrors :func:`eval.judge_local.judge_once`: one
    corrective retry, then give up. The positional A/B here is *literal* — the
    caller is responsible for mapping it back to conditions.
    """
    messages: list[dict[str, str]] = [
        {"role": "user", "content": build_pairwise_prompt(spec, response_a, response_b)}
    ]
    last_raw = ""
    for attempt in range(2):
        content, reasoning = client.chat(messages)
        raw = strip_think(content).strip()
        if not raw and reasoning:
            raw = strip_think(reasoning).strip()
        last_raw = raw

        obj = extract_json_object(raw)
        if obj is not None:
            verdict = coerce_pairwise(obj)
            if verdict is not None:
                return verdict, raw

        if attempt == 0:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": RETRY_NUDGE})

    return None, last_raw


# --- Reconciliation ---------------------------------------------------------

# Reconciled outcome of a pair, in *condition* terms (not positional):
#   "left"  -> the pair's left condition won
#   "right" -> the pair's right condition won
#   "tie"   -> genuine tie (either order said tie, or the two orders disagreed
#              on a non-flip basis)
#   "position_flip" -> the positional A won both orders: swapping flipped the
#              verdict. Counted as a tie in win-rates; tracked separately.
Reconciled = Literal["left", "right", "tie", "position_flip"]


def _pos_to_condition(order: Order, pos: PosWinner) -> Literal["left", "right", "tie"]:
    """Map a positional winner under a given order back to left/right condition.

    Order ``"AB"``: position A = left condition, B = right.
    Order ``"BA"``: position A = right condition, B = left (swapped).
    """
    if pos == "tie":
        return "tie"
    if order == "AB":
        return "left" if pos == "A" else "right"
    # order == "BA": A is the right condition, B is the left
    return "right" if pos == "A" else "left"


def reconcile(ab_winner: PosWinner, ba_winner: PosWinner) -> Reconciled:
    """Reconcile the two orders' positional winners into a condition verdict.

    * Either order says ``tie`` -> ``"tie"``.
    * Both orders pick the same *positional* slot (both "A" or both "B") -> the
      order flipped the verdict (position bias) -> ``"position_flip"``.
    * Both orders agree on the same *condition* once un-swapped -> that side.
    * Anything else is unreachable here (a non-flip disagreement requires a tie,
      already handled); guarded as ``"tie"`` for total coverage.
    """
    if ab_winner == "tie" or ba_winner == "tie":
        return "tie"
    # Both are A/B here. A clean positional flip is "both said the same slot".
    if ab_winner == ba_winner:
        return "position_flip"
    left_ab = _pos_to_condition("AB", ab_winner)
    left_ba = _pos_to_condition("BA", ba_winner)
    if left_ab == left_ba:
        # both un-swapped verdicts agree on the same condition
        return left_ab  # type: ignore[return-value]
    return "tie"


# --- Work construction (units present in BOTH legs of a pair) ---------------


def parse_pairs(spec: str) -> list[tuple[str, str]]:
    """Parse ``"composed:none,composed:flat"`` into ``[(left, right), ...]``."""
    pairs: list[tuple[str, str]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.count(":") != 1:
            raise ValueError(f"bad pair {chunk!r}: expected 'left:right'")
        left, right = (s.strip() for s in chunk.split(":"))
        if not left or not right or left == right:
            raise ValueError(f"bad pair {chunk!r}: left/right must differ and be non-empty")
        pairs.append((left, right))
    return pairs


class Unit:
    """One comparison unit: a (run_dir, task, pair, run_index) with both files."""

    __slots__ = ("run_dir", "task_id", "pair", "run_index", "left_txt", "right_txt", "model")

    def __init__(
        self,
        run_dir: Path,
        task_id: str,
        pair: tuple[str, str],
        run_index: int,
        left_txt: Path,
        right_txt: Path,
        model: str | None,
    ) -> None:
        self.run_dir = run_dir
        self.task_id = task_id
        self.pair = pair
        self.run_index = run_index
        self.left_txt = left_txt
        self.right_txt = right_txt
        self.model = model


def _meta_model(meta_path: Path) -> str | None:
    """Best-effort model leg from a meta file (``agent_model`` on real runs)."""
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return meta.get("agent_model") or meta.get("model")


def build_units(run_dirs: list[Path], pairs: list[tuple[str, str]]) -> list[Unit]:
    """Build one :class:`Unit` per task x run_index present in BOTH legs of a pair.

    A unit exists only when the ``run-N.txt`` artifact is present for *both* the
    left and right condition of the pair, for the same task and run_index, under
    the same run_dir.
    """
    units: list[Unit] = []
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            print(f"warning: run dir not found: {run_dir}", file=sys.stderr)
            continue
        for task_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            task_id = task_dir.name
            for left, right in pairs:
                left_dir = task_dir / left
                right_dir = task_dir / right
                if not (left_dir.is_dir() and right_dir.is_dir()):
                    continue
                for left_txt in sorted(left_dir.glob("run-*.txt")):
                    run_index = int(left_txt.stem.split("-", 1)[1])
                    right_txt = right_dir / left_txt.name
                    if not right_txt.is_file():
                        continue
                    model = _meta_model(left_txt.with_suffix(".meta.json"))
                    units.append(
                        Unit(run_dir, task_id, (left, right), run_index, left_txt, right_txt, model)
                    )
    return units


def _stratified_units(units: list[Unit], k: int) -> list[Unit]:
    """Keep at most ``k`` units per (task_id, pair) stratum, by run_index.

    Deterministic so a resumed pass samples the *same* units.
    """
    by_stratum: dict[tuple[str, tuple[str, str]], list[Unit]] = defaultdict(list)
    for u in units:
        by_stratum[(u.task_id, u.pair)].append(u)
    kept: list[Unit] = []
    for items in by_stratum.values():
        items.sort(key=lambda u: u.run_index)
        kept.extend(items[:k])
    return kept


# --- Checkpoint keys --------------------------------------------------------


def _pair_str(pair: tuple[str, str]) -> str:
    return f"{pair[0]}:{pair[1]}"


def order_key(
    run_dir: Path, task_id: str, pair: tuple[str, str], run_index: int, order: Order
) -> str:
    """Checkpoint key for one judged ORDER (so a kill mid-pair resumes cleanly)."""
    return f"{run_dir}|{task_id}|{_pair_str(pair)}|{run_index}|{order}"


def load_done_orders(path: Path) -> dict[str, dict[str, Any]]:
    """Read a pairwise.jsonl and return ``{order_key: record}`` for completed orders.

    Tolerant of a truncated final line. Later records win on duplicate keys.
    """
    done: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return done
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = rec.get("key")
        if isinstance(key, str):
            done[key] = rec
    return done


def _order_record(
    key: str,
    unit: Unit,
    order: Order,
    verdict: dict[str, Any] | None,
    *,
    error: str | None = None,
    raw: str | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "key": key,
        "run_dir": str(unit.run_dir),
        "task_id": unit.task_id,
        "pair": _pair_str(unit.pair),
        "run_index": unit.run_index,
        "order": order,
        "model": unit.model,
        "judged_at": datetime.now(UTC).isoformat(),
    }
    if verdict is not None:
        rec["winner"] = verdict["winner"]
        rec["reason"] = verdict["reason"]
    if error is not None:
        rec["parse_error"] = error
    if raw is not None:
        rec["raw"] = raw
    return rec


# --- run command ------------------------------------------------------------


def _select_units(args: argparse.Namespace) -> list[Unit]:
    run_dirs = [Path(d) for d in args.run_dirs]
    pairs = parse_pairs(args.pairs)
    units = build_units(run_dirs, pairs)
    if args.sample is not None:
        units = _stratified_units(units, args.sample)
    if args.limit is not None:
        units = units[: args.limit]
    return units


def _judge_order(
    client: LocalJudgeClient,
    spec: str,
    unit: Unit,
    order: Order,
) -> tuple[dict[str, Any] | None, str]:
    """Judge one order, mapping the unit's left/right files onto positional A/B."""
    if order == "AB":
        a_txt, b_txt = unit.left_txt, unit.right_txt
    else:
        a_txt, b_txt = unit.right_txt, unit.left_txt
    return judge_pair_once(client, spec, a_txt.read_text(), b_txt.read_text())


def cmd_pairwise(args: argparse.Namespace) -> int:
    specs = task_specs()
    units = _select_units(args)
    if not units:
        print("nothing to judge (no task x run present in both legs of any pair)")
        return 0

    if args.verdicts:
        path = Path(args.verdicts)
    else:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = PAIRWISE_DIR / ts / "pairwise.jsonl"

    done = load_done_orders(path)
    # Expand each unit into its two orders; skip orders already checkpointed.
    pending: list[tuple[Unit, Order]] = []
    for unit in units:
        for order in ("AB", "BA"):
            key = order_key(unit.run_dir, unit.task_id, unit.pair, unit.run_index, order)
            if key not in done:
                pending.append((unit, order))  # type: ignore[arg-type]

    url = os.environ.get("JUDGE_URL", DEFAULT_JUDGE_URL)
    model = os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    est_total = len(pending) * EST_SECONDS_PER_CALL
    print(
        f"judge-pairwise: {len(units)} units ({len(units) * CALLS_PER_UNIT} calls), "
        f"{len(done)} orders done, {len(pending)} to judge"
    )
    print(f"  endpoint: {url}  model: {model}")
    print(f"  verdicts: {path}")
    print(
        f"  projected wall-time: ~{_fmt_eta(est_total)} "
        f"(@ ~{EST_SECONDS_PER_CALL:.0f}s/call, 2 calls/unit, sequential)"
    )
    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    if not pending:
        print("all orders already judged — nothing to do")
        args.verdicts_resolved = [path]
        return _report(args)

    path.parent.mkdir(parents=True, exist_ok=True)
    client = LocalJudgeClient(url=url, model=model, timeout=args.timeout)
    started = time.monotonic()
    parse_errors = 0
    try:
        with path.open("a") as fh:
            for i, (unit, order) in enumerate(pending, start=1):
                key = order_key(unit.run_dir, unit.task_id, unit.pair, unit.run_index, order)
                spec = specs.get(unit.task_id)
                t0 = time.monotonic()
                if spec is None:
                    print(f"warning: unknown task_id {unit.task_id}, skipping", file=sys.stderr)
                    rec = _order_record(key, unit, order, None, error="unknown_task_id")
                else:
                    verdict, raw = _judge_order(client, spec, unit, order)
                    if verdict is None:
                        parse_errors += 1
                        rec = _order_record(
                            key, unit, order, None, error="parse_error", raw=raw[:500]
                        )
                    else:
                        rec = _order_record(key, unit, order, verdict)

                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

                elapsed = time.monotonic() - t0
                outcome = rec.get("winner", f"PARSE_ERROR({rec.get('parse_error')})")
                print(
                    f"[{i}/{len(pending)}] {unit.task_id}/{_pair_str(unit.pair)}/"
                    f"run-{unit.run_index}/{order} winner={outcome} ({elapsed:.0f}s)"
                )
                if i % 10 == 0:
                    rate = (time.monotonic() - started) / i
                    remaining = (len(pending) - i) * rate
                    print(
                        f"  [{i}/{len(pending)}] ETA ~{_fmt_eta(remaining)} "
                        f"(avg {rate:.0f}s/call, {parse_errors} parse errors)"
                    )
    finally:
        client.close()

    print(f"done: {len(pending)} orders judged, {parse_errors} parse errors. verdicts: {path}")
    args.verdicts_resolved = [path]
    return _report(args)


# --- Reconciliation + report ------------------------------------------------


class PairOutcome:
    """A fully reconciled comparison unit, ready for aggregation."""

    __slots__ = ("run_dir", "task_id", "pair", "run_index", "model", "outcome")

    run_dir: str
    task_id: str
    pair: str
    run_index: int
    model: str | None
    outcome: Reconciled

    def __init__(
        self,
        run_dir: str,
        task_id: str,
        pair: str,
        run_index: int,
        model: str | None,
        outcome: Reconciled,
    ) -> None:
        self.run_dir = run_dir
        self.task_id = task_id
        self.pair = pair
        self.run_index = run_index
        self.model = model
        self.outcome = outcome


def reconcile_records(records: list[dict[str, Any]]) -> list[PairOutcome]:
    """Group order-records into units and reconcile each unit with both orders.

    A unit is only reconciled when BOTH orders have a non-error verdict. Units
    missing an order (mid-pair kill) or with a parse error in either order are
    skipped. Later records win on duplicate (key) collisions.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for rec in records:
        if isinstance(rec.get("key"), str):
            by_key[rec["key"]] = rec

    # Group the two orders back into their unit.
    units: dict[tuple[str, str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for rec in by_key.values():
        if "winner" not in rec:
            continue
        ukey = (rec["run_dir"], rec["task_id"], rec["pair"], rec["run_index"])
        units[ukey][rec["order"]] = rec

    outcomes: list[PairOutcome] = []
    for (run_dir, task_id, pair, run_index), orders in units.items():
        if "AB" not in orders or "BA" not in orders:
            continue
        ab = orders["AB"]["winner"]
        ba = orders["BA"]["winner"]
        outcome = reconcile(ab, ba)
        model = orders["AB"].get("model") or orders["BA"].get("model")
        outcomes.append(PairOutcome(run_dir, task_id, pair, run_index, model, outcome))
    return outcomes


def _sign_test_p(wins: int, losses: int) -> float | None:
    """Two-sided binomial sign-test p-value for wins vs losses (ties dropped)."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    # P(X <= k) under Binomial(n, 0.5), two-sided.
    tail = sum(math.comb(n, j) for j in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _heuristic_delta_signs(
    run_dirs: list[Path], pair: tuple[str, str]
) -> dict[tuple[str, str], int]:
    """``{(run_dir, task_id): sign}`` of (left mean_score − right mean_score).

    Reads each run_dir's ``summary.json`` (``by_task[task][cond].mean_score``).
    Sign is +1 (left better), -1 (right better), or 0. Missing data is omitted.
    """
    left, right = pair
    signs: dict[tuple[str, str], int] = {}
    for run_dir in run_dirs:
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        by_task = summary.get("by_task")
        if not isinstance(by_task, dict):
            continue
        for task_id, conds in by_task.items():
            if not isinstance(conds, dict):
                continue
            lc, rc = conds.get(left), conds.get(right)
            if not (isinstance(lc, dict) and isinstance(rc, dict)):
                continue
            ls, rs = lc.get("mean_score"), rc.get("mean_score")
            if ls is None or rs is None:
                continue
            delta = ls - rs
            signs[(str(run_dir), task_id)] = (delta > 0) - (delta < 0)
    return signs


def _outcome_sign(outcome: Reconciled) -> int:
    """Win-rate sign of a reconciled outcome: +1 left, -1 right, 0 tie/flip."""
    if outcome == "left":
        return 1
    if outcome == "right":
        return -1
    return 0  # tie and position_flip both count as ties in win-rates


def _tally(outcomes: list[PairOutcome]) -> dict[str, int]:
    t = {"left": 0, "right": 0, "tie": 0, "position_flip": 0}
    for o in outcomes:
        t[o.outcome] += 1
    return t


def _fmt_rates(pair: tuple[str, str], t: dict[str, int]) -> str:
    n = sum(t.values())
    if n == 0:
        return "  (no reconciled units)"
    left, right = pair
    wins = t["left"]
    losses = t["right"]
    ties = t["tie"] + t["position_flip"]
    flips = t["position_flip"]
    p = _sign_test_p(wins, losses)
    p_str = f"p={p:.4f}" if p is not None else "p=n/a"
    return (
        f"  n={n}  {left} win {wins / n:.2%}  tie {ties / n:.2%}  "
        f"{right} win {losses / n:.2%}\n"
        f"  position-flip rate {flips / n:.2%} ({flips}/{n})  sign-test {p_str} "
        f"(wins {wins} vs losses {losses})"
    )


def render_pairwise_report(
    run_dirs: list[Path],
    pair: tuple[str, str],
    outcomes: list[PairOutcome],
) -> None:
    left, right = pair
    print(f"\n=== pairwise judge: {left} vs {right} ({len(outcomes)} units) ===")
    print(_fmt_rates(pair, _tally(outcomes)))

    # Per task-set (run_dir) breakdown.
    by_dir: dict[str, list[PairOutcome]] = defaultdict(list)
    for o in outcomes:
        by_dir[o.run_dir].append(o)
    if len(by_dir) > 1:
        print("\nby task-set:")
        for rd in sorted(by_dir):
            print(f" {Path(rd).name}")
            print(_fmt_rates(pair, _tally(by_dir[rd])))

    # Per model leg.
    by_model: dict[str, list[PairOutcome]] = defaultdict(list)
    for o in outcomes:
        by_model[o.model or "<unknown>"].append(o)
    if len(by_model) > 1 or "<unknown>" not in by_model:
        print("\nby model leg:")
        for m in sorted(by_model):
            print(f" {m}")
            print(_fmt_rates(pair, _tally(by_model[m])))

    # Agreement with the heuristic graders' delta sign.
    signs = _heuristic_delta_signs(run_dirs, pair)
    agree = total = 0
    for o in outcomes:
        hsign = signs.get((o.run_dir, o.task_id))
        if hsign is None or hsign == 0:
            continue
        jsign = _outcome_sign(o.outcome)
        if jsign == 0:
            continue
        total += 1
        if jsign == hsign:
            agree += 1
    if total:
        print(
            f"\nheuristic sign-agreement: {agree}/{total} = {agree / total:.2%} "
            f"(judge winner matches heuristic {left}−{right} delta sign; "
            f"ties on either side excluded)"
        )
    else:
        print("\nheuristic sign-agreement: n/a (no decisive overlap with summary.json)")
    print()


def _resolve_verdicts(args: argparse.Namespace) -> list[Path]:
    if getattr(args, "verdicts_resolved", None):
        return args.verdicts_resolved
    if args.verdicts:
        raw = args.verdicts if isinstance(args.verdicts, list) else [args.verdicts]
        return [Path(p) for p in raw]
    candidates = sorted(
        PAIRWISE_DIR.glob("*/pairwise.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        print("no pairwise.jsonl files under eval/runs/judge-pairwise/", file=sys.stderr)
        return []
    print(f"(using latest verdicts: {candidates[0]})")
    return [candidates[0]]


def _report(args: argparse.Namespace) -> int:
    paths = _resolve_verdicts(args)
    if not paths:
        return 1
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            print(f"warning: no verdicts file at {path}", file=sys.stderr)
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    outcomes = reconcile_records(records)
    run_dirs = [Path(d) for d in args.run_dirs] if args.run_dirs else []
    pairs = parse_pairs(args.pairs)
    for pair in pairs:
        pstr = _pair_str(pair)
        sub = [o for o in outcomes if o.pair == pstr]
        render_pairwise_report(run_dirs, pair, sub)
    return 0


def cmd_pairwise_report(args: argparse.Namespace) -> int:
    return _report(args)


# --- argparse wiring (registered by judge_local's main) ---------------------


def add_subcommands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("pairwise", help="pairwise A/B comparison judging (CodeJudgeBench-style)")
    p.add_argument("run_dirs", nargs="+", help="run dirs under eval/runs/")
    p.add_argument(
        "--pairs",
        required=True,
        help="comma-separated left:right pairs, e.g. composed:none,composed:flat",
    )
    p.add_argument(
        "--sample", type=int, metavar="K", help="keep K units per task×pair (stratified)"
    )
    p.add_argument("--limit", type=int, help="hard cap on units after sampling")
    p.add_argument(
        "--verdicts", help="checkpoint path (default: judge-pairwise/<ts>/pairwise.jsonl)"
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-call timeout s")
    p.add_argument("--dry-run", action="store_true", help="print plan + ETA, send nothing")
    p.set_defaults(func=cmd_pairwise)

    pr = sub.add_parser("pairwise-report", help="aggregate a pairwise.jsonl (no server)")
    pr.add_argument("run_dirs", nargs="*", help="run dirs (for summary.json sign-agreement)")
    pr.add_argument("--pairs", required=True, help="comma-separated left:right pairs to report")
    pr.add_argument("--verdicts", action="append", help="pairwise.jsonl path(s); default: latest")
    pr.set_defaults(func=cmd_pairwise_report)
