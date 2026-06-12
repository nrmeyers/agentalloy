"""Labeled benchmark for the signal-layer intent classifier — cosine vs reranker.

Measures two backends head-to-head over a hand-labeled dataset of ~130 utterances
spanning three transition intents (completion / approval / redirection) plus
none-of-the-above prompts, with difficulty tags (clear / paraphrase / negation /
scoped).

Backends
--------
* **cosine** — replicates ``classifier._intent_similarity`` exactly: embeds the
  utterance + each intent's reference phrases via the live Ollama embedder and
  takes max cosine per intent. Operating threshold 0.75 (the production value).
  Reuses ``classifier._INTENT_REFERENCES`` and ``classifier._cosine`` directly.
* **reranker** — qwen3-reranker-0.6b (Stage B model) yes/no pair-scoring via
  ``lm_assist.FragmentScorer``. Scores each utterance against each intent's
  ``classifier._INTENT_TASK_DESCRIPTIONS`` entry. Tries both orientations
  (utterance-as-query vs utterance-as-document) and reports which separates
  better. Threshold is swept; best-F1 point + full sweep reported.

Multi-class decision: argmax over the three intent scores; if the winning score
is below the operating threshold the prediction is ``none``.

Usage
-----
    uv run python -m eval.intent_bench
    uv run python -m eval.intent_bench --limit 20        # quick smoke
    uv run python -m eval.intent_bench --orientation doc # force one orientation

Requires the live embedder (Ollama :11434) and reranker (llama-server :60001).
"""

from __future__ import annotations

import argparse
import csv
import statistics
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import yaml

from agentalloy.lm_client import OpenAICompatClient
from agentalloy.retrieval.lm_assist import (
    FragmentScorer,
    LMAssistConfig,
    LMAssistMode,
)
from agentalloy.signals.classifier import (
    _INTENT_REFERENCES,
    _INTENT_TASK_DESCRIPTIONS,
    _cosine,
)

INTENTS = ["completion", "approval", "redirection"]
LABELS = [*INTENTS, "none"]
DIFFICULTIES = ["clear", "paraphrase", "negation", "scoped"]

_EMBED_URL = "http://localhost:11434"
_EMBED_MODEL = "qwen3-embedding:0.6b"
_RERANK_URL = "http://127.0.0.1:60001"
_RERANK_MODEL = "qwen3-reranker-0.6b"
_COSINE_THRESHOLD = 0.75

# Instruct line shown to the reranker. The Stage-B default instruct is about
# skill-fragment relevance and produces near-zero scores here; this one frames
# the yes/no question as intent classification.
_RERANK_INSTRUCT = (
    "Judge whether the user message expresses the intent described. "
    "Answer yes only if the message clearly signals that intent."
)

DATA_PATH = Path(__file__).parent / "intent_bench_data.yaml"


@dataclass
class Item:
    text: str
    label: str
    difficulty: str


@dataclass
class Scored:
    item: Item
    scores: dict[str, float]  # intent -> score
    pred: str  # argmax-with-threshold prediction
    latency_ms: float


def load_dataset(path: Path = DATA_PATH) -> list[Item]:
    raw = yaml.safe_load(path.read_text())
    items = [
        Item(text=str(d["text"]), label=str(d["label"]), difficulty=str(d["difficulty"]))
        for d in raw["items"]
    ]
    for it in items:
        if it.label not in LABELS:
            raise ValueError(f"bad label {it.label!r} in {it.text!r}")
        if it.difficulty not in DIFFICULTIES:
            raise ValueError(f"bad difficulty {it.difficulty!r} in {it.text!r}")
    return items


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def predict_from_scores(scores: dict[str, float], threshold: float) -> str:
    """argmax over intent scores; below threshold -> none."""
    best_intent = max(scores, key=lambda k: scores[k])
    return best_intent if scores[best_intent] >= threshold else "none"


def run_cosine(items: list[Item], threshold: float) -> list[Scored]:
    """Replicate classifier._intent_similarity: max cosine vs reference phrases."""
    client = OpenAICompatClient(_EMBED_URL)
    out: list[Scored] = []
    # Pre-embed all reference phrases once (they're shared across items).
    ref_texts: dict[str, list[str]] = {i: _INTENT_REFERENCES[i] for i in INTENTS}
    flat_refs = [r for i in INTENTS for r in ref_texts[i]]
    ref_vecs_flat = client.embed(model=_EMBED_MODEL, texts=flat_refs)
    ref_vecs: dict[str, list[list[float]]] = {}
    pos = 0
    for i in INTENTS:
        n = len(ref_texts[i])
        ref_vecs[i] = ref_vecs_flat[pos : pos + n]
        pos += n
    try:
        for it in items:
            t0 = time.perf_counter()
            qvec = client.embed(model=_EMBED_MODEL, texts=[it.text])[0]
            scores = {i: max(_cosine(qvec, rv) for rv in ref_vecs[i]) for i in INTENTS}
            dt = (time.perf_counter() - t0) * 1000.0
            out.append(Scored(it, scores, predict_from_scores(scores, threshold), dt))
    finally:
        client.close()
    return out


def run_reranker(
    items: list[Item],
    orientation: Literal["query", "doc"],
    threshold: float,
) -> list[Scored]:
    """Score utterance against each intent's task description via the reranker.

    orientation="query": utterance is the Query, task description is the Document.
    orientation="doc":   task description is the Query, utterance is the Document.
    """
    config = LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url=_RERANK_URL,
        timeout_ms=5000,  # generous; benchmark, not the 300ms runtime budget
        keep_threshold=0.05,
        model=_RERANK_MODEL,
    )
    scorer = FragmentScorer(config)
    out: list[Scored] = []
    try:
        for it in items:
            t0 = time.perf_counter()
            scores: dict[str, float] = {}
            for intent in INTENTS:
                desc = _INTENT_TASK_DESCRIPTIONS[intent]
                if orientation == "query":
                    task, doc = it.text, desc
                else:
                    task, doc = desc, it.text
                # FragmentScorer.score builds the prompt with the module default
                # instruct; we want our intent-framed instruct, so call the
                # internal scorer via a one-doc batch using build_prompt override.
                res = _score_pair(scorer, task, doc)
                scores[intent] = res
            dt = (time.perf_counter() - t0) * 1000.0
            out.append(Scored(it, scores, predict_from_scores(scores, threshold), dt))
    finally:
        scorer.close()
    return out


def _score_pair(scorer: FragmentScorer, task: str, document: str) -> float:
    """Score a single (task, document) pair with the intent-framed instruct.

    Reuses FragmentScorer's HTTP client + logprob math but overrides the
    instruct line. We build the prompt explicitly and post via the scorer's
    private machinery to avoid duplicating the request/parse code.
    """
    from agentalloy.retrieval.lm_assist import (
        _parse_completion_logprobs,
        build_prompt,
        score_from_logprobs,
    )

    payload = {
        "model": _RERANK_MODEL,
        "prompt": build_prompt(task, document, instruct=_RERANK_INSTRUCT),
        "max_tokens": 1,
        "temperature": 0.0,
        "n_probs": 20,
        "logprobs": 20,
    }
    resp = scorer._client.post("/v1/completions", json=payload)  # pyright: ignore[reportPrivateUsage]
    resp.raise_for_status()
    return score_from_logprobs(_parse_completion_logprobs(resp.json()))


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


@dataclass
class PRF:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int


def per_intent_prf(scored: list[Scored], intent: str) -> PRF:
    tp = fp = fn = 0
    for s in scored:
        gold_pos = s.item.label == intent
        pred_pos = s.pred == intent
        if pred_pos and gold_pos:
            tp += 1
        elif pred_pos and not gold_pos:
            fp += 1
        elif not pred_pos and gold_pos:
            fn += 1
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return PRF(prec, rec, f1, tp, fp, fn)


def macro_f1(scored: list[Scored]) -> float:
    return statistics.mean(per_intent_prf(scored, i).f1 for i in INTENTS)


def overall_accuracy(scored: list[Scored]) -> float:
    return sum(1 for s in scored if s.pred == s.item.label) / len(scored)


def accuracy_by_difficulty(scored: list[Scored]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d in DIFFICULTIES:
        subset = [s for s in scored if s.item.difficulty == d]
        out[d] = sum(1 for s in subset if s.pred == s.item.label) / len(subset) if subset else 0.0
    return out


def confusion(scored: list[Scored]) -> dict[str, dict[str, int]]:
    m = {g: {p: 0 for p in LABELS} for g in LABELS}
    for s in scored:
        m[s.item.label][s.pred] += 1
    return m


def rescore_predictions(scored: list[Scored], threshold: float) -> list[Scored]:
    """Re-derive predictions at a new threshold without re-querying the backend."""
    return [
        Scored(s.item, s.scores, predict_from_scores(s.scores, threshold), s.latency_ms)
        for s in scored
    ]


def sweep_thresholds(
    scored: list[Scored], lo: float = 0.0, hi: float = 1.0, step: float = 0.02
) -> list[tuple[float, float, float]]:
    """Return [(threshold, macro_f1, accuracy)] across the sweep."""
    rows: list[tuple[float, float, float]] = []
    t = lo
    while t <= hi + 1e-9:
        rs = rescore_predictions(scored, t)
        rows.append((round(t, 3), macro_f1(rs), overall_accuracy(rs)))
        t += step
    return rows


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


@dataclass
class BackendReport:
    name: str
    scored: list[Scored]
    threshold: float
    sweep: list[tuple[float, float, float]] | None = field(default=None)


def _fmt_prf_table(scored: list[Scored]) -> list[str]:
    lines = [f"  {'intent':<12} {'P':>6} {'R':>6} {'F1':>6}  (tp/fp/fn)"]
    for i in INTENTS:
        p = per_intent_prf(scored, i)
        lines.append(
            f"  {i:<12} {p.precision:>6.3f} {p.recall:>6.3f} {p.f1:>6.3f}  ({p.tp}/{p.fp}/{p.fn})"
        )
    lines.append(f"  {'macro-F1':<12} {'':>6} {'':>6} {macro_f1(scored):>6.3f}")
    return lines


def _fmt_confusion(scored: list[Scored]) -> list[str]:
    m = confusion(scored)
    hdr = "  gold\\pred  " + "".join(f"{p[:5]:>8}" for p in LABELS)
    lines = [hdr]
    for g in LABELS:
        lines.append(f"  {g:<10} " + "".join(f"{m[g][p]:>8}" for p in LABELS))
    return lines


def _latency_stats(scored: list[Scored]) -> tuple[float, float]:
    lats = sorted(s.latency_ms for s in scored)
    median = statistics.median(lats)
    p95 = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
    return median, p95


def render_report(reports: list[BackendReport], items_n: int) -> str:
    lines: list[str] = []
    lines.append(f"# Intent-classifier benchmark — {date.today().isoformat()}")
    lines.append("")
    counts = {lbl: sum(1 for s in reports[0].scored if s.item.label == lbl) for lbl in LABELS}
    breakdown = ", ".join(f"{lbl}={counts[lbl]}" for lbl in LABELS)
    lines.append(f"Dataset: {items_n} labeled utterances ({breakdown})")
    lines.append("")

    # Headline: accuracy by difficulty, side by side.
    lines.append("## Headline — accuracy by difficulty tag")
    lines.append("")
    diff_tables = {r.name: accuracy_by_difficulty(r.scored) for r in reports}
    hdr = "| difficulty | " + " | ".join(r.name for r in reports) + " |"
    sep = "|" + "---|" * (len(reports) + 1)
    lines.append(hdr)
    lines.append(sep)
    for d in DIFFICULTIES:
        row = f"| {d} | " + " | ".join(f"{diff_tables[r.name][d]:.3f}" for r in reports) + " |"
        lines.append(row)
    overall_row = (
        "| **overall** | "
        + " | ".join(f"**{overall_accuracy(r.scored):.3f}**" for r in reports)
        + " |"
    )
    lines.append(overall_row)
    macro_row = "| macro-F1 | " + " | ".join(f"{macro_f1(r.scored):.3f}" for r in reports) + " |"
    lines.append(macro_row)
    lat_row = (
        "| latency p50 (ms) | "
        + " | ".join(f"{_latency_stats(r.scored)[0]:.0f}" for r in reports)
        + " |"
    )
    lines.append(lat_row)
    lines.append("")

    for r in reports:
        lines.append(f"## {r.name} (threshold={r.threshold:.3f})")
        lines.append("")
        lines.append("### per-intent P/R/F1")
        lines.append("```")
        lines.extend(_fmt_prf_table(r.scored))
        lines.append("```")
        lines.append("### confusion (gold rows / predicted cols)")
        lines.append("```")
        lines.extend(_fmt_confusion(r.scored))
        lines.append("```")
        med, p95 = _latency_stats(r.scored)
        lines.append(
            f"latency: p50={med:.0f}ms p95={p95:.0f}ms "
            f"(total {sum(s.latency_ms for s in r.scored) / 1000:.1f}s)"
        )
        lines.append("")
        if r.sweep is not None:
            best = max(r.sweep, key=lambda x: x[1])
            lines.append(f"### threshold sweep (best macro-F1 @ {best[0]:.2f} = {best[1]:.3f})")
            lines.append("```")
            lines.append("  thresh  macroF1  accuracy")
            for thr, f1, acc in r.sweep:
                mark = "  <== best" if thr == best[0] else ""
                lines.append(f"  {thr:>5.2f}  {f1:>7.3f}  {acc:>7.3f}{mark}")
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, reports: list[BackendReport]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "backend",
                "text",
                "gold",
                "difficulty",
                "pred",
                "score_completion",
                "score_approval",
                "score_redirection",
                "latency_ms",
            ]
        )
        for r in reports:
            for s in r.scored:
                w.writerow(
                    [
                        r.name,
                        s.item.text,
                        s.item.label,
                        s.item.difficulty,
                        s.pred,
                        f"{s.scores['completion']:.4f}",
                        f"{s.scores['approval']:.4f}",
                        f"{s.scores['redirection']:.4f}",
                        f"{s.latency_ms:.1f}",
                    ]
                )


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="cap items (smoke test)")
    parser.add_argument(
        "--orientation",
        choices=["query", "doc", "both"],
        default="both",
        help="reranker utterance orientation; 'both' picks the better-separating one",
    )
    parser.add_argument("--no-write", action="store_true", help="skip writing run artifacts")
    args = parser.parse_args(argv)

    items = load_dataset()
    if args.limit:
        items = items[: args.limit]
    print(f"loaded {len(items)} items")

    # --- cosine backend ---
    print("running cosine backend...")
    cosine_scored = run_cosine(items, _COSINE_THRESHOLD)
    cosine_report = BackendReport("cosine", cosine_scored, _COSINE_THRESHOLD)

    # --- reranker backend: choose orientation ---
    rerank_orientations: list[Literal["query", "doc"]]
    rerank_orientations = ["query", "doc"] if args.orientation == "both" else [args.orientation]

    best_orient = None
    best_rerank_scored: list[Scored] | None = None
    best_sep = -1.0
    for orient in rerank_orientations:
        print(f"running reranker backend (orientation={orient})...")
        # Score once at a neutral threshold (predictions re-derived in the sweep).
        rs = run_reranker(items, orient, threshold=0.5)
        # Separation metric: mean(gold-intent score) - mean(off-intent score)
        # over positive items — higher = cleaner separation.
        pos = [s for s in rs if s.item.label in INTENTS]
        sep = statistics.mean(
            s.scores[s.item.label]
            - statistics.mean(s.scores[i] for i in INTENTS if i != s.item.label)
            for s in pos
        )
        print(f"  orientation={orient} separation={sep:.4f}")
        if sep > best_sep:
            best_sep, best_orient, best_rerank_scored = sep, orient, rs

    assert best_rerank_scored is not None and best_orient is not None
    print(f"chosen reranker orientation: {best_orient} (separation={best_sep:.4f})")

    sweep = sweep_thresholds(best_rerank_scored)
    best_thr = max(sweep, key=lambda x: x[1])[0]
    rerank_scored = rescore_predictions(best_rerank_scored, best_thr)
    rerank_report = BackendReport(f"reranker[{best_orient}]", rerank_scored, best_thr, sweep=sweep)
    # Also include a cosine sweep for completeness.
    cosine_report.sweep = sweep_thresholds(cosine_scored)

    reports = [cosine_report, rerank_report]
    report_md = render_report(reports, len(items))
    print("\n" + report_md)

    if not args.no_write:
        run_dir = Path(__file__).parent / "runs" / f"intent-bench-{date.today().isoformat()}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.md").write_text(report_md)
        write_csv(run_dir / "scores.csv", reports)
        print(f"\nwrote {run_dir}/report.md and scores.csv")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
