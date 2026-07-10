"""Offline LLM-as-judge running against a *local* OpenAI-compatible server.

This is the new default judge. It scores the same ``run-N.txt`` artifacts as the
cloud judge (:mod:`eval.judge`) against the same rubric (see
:mod:`eval.judge_common`), but instead of the Anthropic Batches API it calls a
local ``llama-server`` exposing the OpenAI ``/v1/chat/completions`` endpoint.

Target model: **Qwen3.6-27B** (alias ``qwen3.6-27b``,
``http://192.168.4.26:60000``). Constraints baked in here, from our serving
setup:

* **Single user turn.** Qwen3.6 supports a system role, but we fold the entire
  persona + rubric + task + candidate output into one user message — keeping the
  prompt identical to how prior runs were graded and to the cloud judge's content.
* **Sampling is pinned server-side** (temp 0.6, top_p 0.95). We do *not* send
  those params — the server owns them.
* **R1-style reasoner.** The server runs ``--reasoning-format deepseek``, so
  the chain-of-thought arrives in ``message.reasoning_content`` and the final
  answer in ``message.content``. We read ``content`` but *also* defensively
  strip any ``<think>...</think>`` block in case a build leaks the tags.
* **No structured-output enforcement.** We prompt for "reply with ONLY a JSON
  object" and parse robustly: extract the first balanced JSON object; on parse
  failure retry **once** with a corrective turn; if that still fails, record a
  null verdict with ``parse_error`` rather than crashing.
* **Sequential only** (single GPU slot). Each judgment runs 30-60s because of
  the thinking trace, so a full pass is multi-hour — hence mandatory
  checkpointing.

Usage::

    # score outputs one by one (resumable):
    uv run python -m eval.judge_local run eval/runs/<leg> [...] \\
        [--conditions composed,none] [--models qwen3-4b,...] \\
        [--limit N] [--sample K]

    # aggregate whatever has been judged so far (no server needed):
    uv run python -m eval.judge_local report eval/runs/<leg> [...] \\
        [--verdicts eval/runs/judge-local/<ts>/verdicts.jsonl]

Checkpointing: every verdict is appended to
``eval/runs/judge-local/<timestamp>/verdicts.jsonl`` as it completes. On
restart, point ``run`` at the same ``--verdicts`` file (or let it pick the
latest) and already-judged ``(run_dir, task, condition, run)`` keys are skipped.
A multi-hour pass survives Ctrl-C, an OOM, or a server bounce.

Env overrides: ``JUDGE_URL`` (default ``http://192.168.4.26:60000``),
``JUDGE_MODEL`` (default ``qwen3.6-27b``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.judge_common import (
    DIMENSIONS,
    EVAL_ROOT,
    JUDGE_PERSONA,
    RUBRIC,
    ReportRow,
    coerce_verdict,
    extract_json_object,
    iter_runs,
    render_report,
    strip_think,
    task_specs,
)

LOCAL_DIR = EVAL_ROOT / "runs" / "judge-local"
DEFAULT_JUDGE_URL = "http://192.168.4.26:60000"
DEFAULT_JUDGE_MODEL = "qwen3.6-27b"
DEFAULT_TIMEOUT_S = 180.0

# Rough per-judgment wall-time (thinking trace dominates). Used only for the
# up-front projection, not for any control flow.
EST_SECONDS_PER_JUDGMENT = 45.0

RETRY_NUDGE = (
    "Your previous reply was not valid JSON. Reply with only the JSON object, nothing else."
)


# --- Prompt -----------------------------------------------------------------


def build_user_prompt(spec: str, output: str) -> str:
    """The whole judge prompt as a single user turn (no system message).

    Persona + length-control instruction (shared with the cloud judge's system
    prompt) lead, then the task, the candidate, the rubric, and an explicit
    JSON-only output contract since the server enforces no schema.
    """
    return (
        f"{JUDGE_PERSONA}\n\n"
        f"## Task specification\n\n{spec}\n\n"
        f"## Candidate response\n\n{output}\n\n"
        f"## Rubric\n\n{RUBRIC}\n\n"
        "## Output format\n\n"
        "Reply with ONLY a JSON object matching this shape, and nothing else "
        "(no markdown fence, no commentary):\n"
        '{"correctness": <int 0-5>, "coverage": <int 0-5>, '
        '"precision": <int 0-5>, "rationale": "<one or two sentences>"}'
    )


# --- HTTP client (httpx; injectable transport for tests) --------------------


class LocalJudgeClient:
    """Thin synchronous client for the OpenAI-compatible chat endpoint.

    Deliberately tiny: one ``chat`` method that returns the raw
    ``(content, reasoning_content)`` pair so the caller owns all parsing. A
    custom ``httpx`` transport can be injected for offline unit tests.
    """

    def __init__(
        self,
        url: str = DEFAULT_JUDGE_URL,
        model: str = DEFAULT_JUDGE_MODEL,
        timeout: float = DEFAULT_TIMEOUT_S,
        *,
        transport: Any = None,
    ) -> None:
        import httpx

        self.model = model
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    def chat(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """POST ``messages`` and return ``(content, reasoning_content)``.

        We do NOT send temperature/top_p — the server pins them. ``max_tokens``
        is generous because the thinking trace can be long.
        """
        resp = self._client.post(
            "/v1/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        return content, reasoning

    def close(self) -> None:
        self._client.close()


# --- Single judgment with one retry -----------------------------------------


def judge_once(
    client: LocalJudgeClient, spec: str, output: str
) -> tuple[dict[str, Any] | None, str]:
    """Score one candidate. Returns ``(verdict_or_None, raw_reply)``.

    Tries the primary prompt, then one corrective retry on a parse/validation
    failure. ``verdict`` is ``None`` only if both attempts fail to yield a
    schema-valid JSON object; the caller turns that into a ``parse_error``
    record. ``raw_reply`` is the last model reply (post think-strip) for
    debugging.
    """
    messages: list[dict[str, str]] = [{"role": "user", "content": build_user_prompt(spec, output)}]
    last_raw = ""
    for attempt in range(2):
        content, reasoning = client.chat(messages)
        # Prefer cleaned content; if the server (mis)routed the answer into
        # reasoning_content and left content empty, fall back to it.
        raw = strip_think(content).strip()
        if not raw and reasoning:
            raw = strip_think(reasoning).strip()
        last_raw = raw

        obj = extract_json_object(raw)
        if obj is not None:
            verdict = coerce_verdict(obj)
            if verdict is not None:
                return verdict, raw

        if attempt == 0:
            # Feed the model its own (bad) reply, then nudge it to JSON-only.
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": RETRY_NUDGE})

    return None, last_raw


# --- Checkpoint store -------------------------------------------------------


def _verdict_key(run_dir: Path, task_id: str, condition: str, run_index: int) -> str:
    return f"{run_dir}|{task_id}|{condition}|{run_index}"


def load_done_keys(verdicts_path: Path) -> set[str]:
    """Read an existing verdicts.jsonl and return the set of completed keys.

    Tolerant of a truncated final line (an interrupted append) — a malformed
    last record is skipped rather than fatal.
    """
    done: set[str] = set()
    if not verdicts_path.is_file():
        return done
    for line in verdicts_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = rec.get("key")
        if isinstance(key, str):
            done.add(key)
    return done


# --- Sampling / filtering ---------------------------------------------------


def _stratified_sample(
    work: list[tuple[Path, dict[str, Any]]], k: int
) -> list[tuple[Path, dict[str, Any]]]:
    """Keep at most ``k`` runs per (task_id, condition) stratum.

    Deterministic: within a stratum, runs are ordered by ``run_index`` and the
    first ``k`` are kept, so a resumed pass samples the *same* runs.
    """
    by_stratum: dict[tuple[str, str], list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for item in work:
        meta = item[1]
        by_stratum[(meta["task_id"], meta["condition"])].append(item)
    kept: list[tuple[Path, dict[str, Any]]] = []
    for items in by_stratum.values():
        items.sort(key=lambda it: it[1].get("run_index", 0))
        kept.extend(items[:k])
    return kept


def _select_work(args: argparse.Namespace) -> list[tuple[Path, dict[str, Any]]]:
    run_dirs = [Path(d) for d in args.run_dirs]
    conditions = set(args.conditions.split(",")) if args.conditions else None
    models = set(args.models.split(",")) if args.models else None

    work: list[tuple[Path, dict[str, Any]]] = []
    for txt_path, meta in iter_runs(run_dirs):
        if conditions is not None and meta.get("condition") not in conditions:
            continue
        if models is not None and meta.get("model") not in models:
            continue
        work.append((txt_path, meta))

    if args.sample is not None:
        work = _stratified_sample(work, args.sample)
    if args.limit is not None:
        work = work[: args.limit]
    return work


# --- run command ------------------------------------------------------------


def _fmt_eta(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def cmd_run(args: argparse.Namespace) -> int:
    specs = task_specs()
    work = _select_work(args)
    if not work:
        print("nothing to judge (no runs matched the filters)")
        return 0

    if args.verdicts:
        verdicts_path = Path(args.verdicts)
    else:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        verdicts_path = LOCAL_DIR / ts / "verdicts.jsonl"

    # Resume from an existing checkpoint if present; only create the directory
    # once we're actually going to write (so --dry-run touches nothing).
    done = load_done_keys(verdicts_path)
    pending = [
        (txt, meta)
        for txt, meta in work
        if _verdict_key(txt.parents[2], meta["task_id"], meta["condition"], meta["run_index"])
        not in done
    ]

    url = os.environ.get("JUDGE_URL", DEFAULT_JUDGE_URL)
    model = os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    est_total = len(pending) * EST_SECONDS_PER_JUDGMENT
    print(f"judge-local: {len(work)} matched, {len(done)} already done, {len(pending)} to judge")
    print(f"  endpoint: {url}  model: {model}")
    print(f"  verdicts: {verdicts_path}")
    print(
        f"  projected wall-time: ~{_fmt_eta(est_total)} "
        f"(@ ~{EST_SECONDS_PER_JUDGMENT:.0f}s/judgment, sequential)"
    )
    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    if not pending:
        print("all matched runs already judged — nothing to do")
        return 0

    verdicts_path.parent.mkdir(parents=True, exist_ok=True)
    import httpx

    client = LocalJudgeClient(url=url, model=model, timeout=args.timeout)
    started = time.monotonic()
    parse_errors = 0
    transport_errors = 0
    consecutive_transport = 0
    try:
        with verdicts_path.open("a") as fh:
            for i, (txt_path, meta) in enumerate(pending, start=1):
                task_id = meta["task_id"]
                spec = specs.get(task_id)
                run_dir = txt_path.parents[2]
                key = _verdict_key(run_dir, task_id, meta["condition"], meta["run_index"])
                t0 = time.monotonic()

                if spec is None:
                    print(
                        f"warning: unknown task_id {task_id}, skipping {txt_path}", file=sys.stderr
                    )
                    record = _record(key, meta, run_dir, None, error="unknown_task_id")
                else:
                    # A single runaway thinking trace (ReadTimeout) or server
                    # hiccup must not kill a multi-hour checkpointed pass. No
                    # verdict row is written, so a later resume retries the
                    # item. Only sustained failure (server down) aborts.
                    try:
                        verdict, raw = judge_once(client, spec, txt_path.read_text())
                        consecutive_transport = 0
                    except httpx.HTTPError as exc:
                        transport_errors += 1
                        consecutive_transport += 1
                        print(
                            f"warning: transport error on {key}: {exc!r} "
                            f"({consecutive_transport} consecutive, "
                            f"{transport_errors} total — item left for a future resume)",
                            file=sys.stderr,
                        )
                        if consecutive_transport >= 5:
                            raise RuntimeError(
                                "5 consecutive transport errors — judge endpoint down?"
                            ) from exc
                        continue
                    if verdict is None:
                        parse_errors += 1
                        record = _record(
                            key, meta, run_dir, None, error="parse_error", raw=raw[:500]
                        )
                    else:
                        record = _record(key, meta, run_dir, verdict)

                fh.write(json.dumps(record) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

                elapsed = time.monotonic() - t0
                _log_verdict(i, len(pending), meta, record, elapsed)
                if i % 10 == 0:
                    rate = (time.monotonic() - started) / i
                    remaining = (len(pending) - i) * rate
                    print(
                        f"  [{i}/{len(pending)}] ETA ~{_fmt_eta(remaining)} "
                        f"(avg {rate:.0f}s/judgment, {parse_errors} parse errors)"
                    )
    finally:
        client.close()

    print(f"done: {len(pending)} judged, {parse_errors} parse errors. verdicts: {verdicts_path}")
    _report_from_verdicts([verdicts_path])
    return 0


def _record(
    key: str,
    meta: dict[str, Any],
    run_dir: Path,
    verdict: dict[str, Any] | None,
    *,
    error: str | None = None,
    raw: str | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "key": key,
        "run_dir": str(run_dir),
        "task_id": meta["task_id"],
        "condition": meta["condition"],
        "run_index": meta["run_index"],
        "heuristic": meta.get("score"),
        "output_tokens": meta.get("output_tokens"),
        "model": meta.get("model"),
        "judged_at": datetime.now(UTC).isoformat(),
    }
    if verdict is not None:
        rec.update(verdict)
    if error is not None:
        rec["parse_error"] = error
    if raw is not None:
        rec["raw"] = raw
    return rec


def _log_verdict(
    i: int, total: int, meta: dict[str, Any], record: dict[str, Any], elapsed: float
) -> None:
    if "judge_score" in record:
        scores = " ".join(f"{d[:4]}={record[d]}" for d in DIMENSIONS)
        verdict = f"score={record['judge_score']:.3f} {scores}"
    else:
        verdict = f"PARSE_ERROR ({record.get('parse_error')})"
    print(
        f"[{i}/{total}] {meta['task_id']}/{meta['condition']}/"
        f"run-{meta['run_index']} {verdict} ({elapsed:.0f}s)"
    )


# --- report command ---------------------------------------------------------


def _rows_from_verdicts(verdicts_paths: list[Path]) -> list[ReportRow]:
    """Build report rows from one or more verdicts.jsonl files.

    Later files win on duplicate keys (so a re-judged record supersedes an
    earlier one). Parse-error / null records are excluded from the report.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for path in verdicts_paths:
        if not path.is_file():
            print(f"warning: no verdicts file at {path}", file=sys.stderr)
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec.get("key"), str):
                by_key[rec["key"]] = rec

    rows: list[ReportRow] = []
    for rec in by_key.values():
        if "judge_score" not in rec or rec.get("heuristic") is None:
            continue
        rows.append(
            (
                Path(rec["run_dir"]),
                rec["task_id"],
                rec["condition"],
                rec["run_index"],
                rec["judge_score"],
                rec["heuristic"],
                rec.get("output_tokens"),
            )
        )
    return rows


def _report_from_verdicts(verdicts_paths: list[Path]) -> None:
    render_report(_rows_from_verdicts(verdicts_paths), title="local LLM-judge report")


def _default_verdicts() -> Path | None:
    candidates = sorted(
        LOCAL_DIR.glob("*/verdicts.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def cmd_report(args: argparse.Namespace) -> int:
    if args.verdicts:
        paths = [Path(p) for p in args.verdicts]
    else:
        latest = _default_verdicts()
        if latest is None:
            print("no verdicts files under eval/runs/judge-local/", file=sys.stderr)
            return 1
        print(f"(using latest verdicts: {latest})")
        paths = [latest]
    _report_from_verdicts(paths)
    return 0


# --- CLI --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="score outputs one by one against the local server")
    p_run.add_argument("run_dirs", nargs="+", help="run dirs under eval/runs/")
    p_run.add_argument("--conditions", help="comma-separated condition filter, e.g. composed,none")
    p_run.add_argument("--models", help="comma-separated model filter")
    p_run.add_argument("--limit", type=int, help="hard cap on judgments after filtering/sampling")
    p_run.add_argument(
        "--sample", type=int, metavar="K", help="keep K runs per task×condition (stratified)"
    )
    p_run.add_argument(
        "--verdicts", help="checkpoint path (default: eval/runs/judge-local/<ts>/verdicts.jsonl)"
    )
    p_run.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-call timeout s"
    )
    p_run.add_argument("--dry-run", action="store_true", help="print plan + ETA, send nothing")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="aggregate a verdicts.jsonl (no server)")
    p_report.add_argument("run_dirs", nargs="*", help="(unused; for symmetry with judge.py)")
    p_report.add_argument(
        "--verdicts", action="append", help="verdicts.jsonl path(s); default: latest"
    )
    p_report.set_defaults(func=cmd_report)

    # Pairwise A/B comparison judging lives in a sibling module for cohesion; it
    # registers its own `pairwise` / `pairwise-report` subcommands here so the
    # whole local judge presents a single CLI entry point.
    from eval.judge_pairwise import add_subcommands as _add_pairwise

    _add_pairwise(sub)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
