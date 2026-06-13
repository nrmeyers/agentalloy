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
* **reranker** — the *production* signal-layer reranker backend. Builds the same
  ``lm_assist.FragmentScorer`` the runtime builds (via
  ``classifier._INTENT_INSTRUCT``), scores each utterance-as-query against each
  intent's ``classifier._INTENT_TASK_DESCRIPTIONS`` entry, and applies the
  production negation guard (``classifier._has_negation``) — a negated utterance
  has all intent scores zeroed so it predicts ``none``. Threshold is swept;
  best-F1 point + full sweep reported.

Two decision framings are reported:

* **argmax** — argmax over the three intent scores, ``none`` below threshold.
  Kept for continuity with the 2026-06-12 report.
* **per-intent (production)** — each intent decided independently
  (``score >= threshold``), since every runtime gate queries exactly one intent.
  This is the number that reflects what ships.

Both the un-guarded and guarded reranker are reported side by side, so the
guard's contribution is visible without a separate flag.

Usage
-----
    uv run python -m eval.intent_bench
    uv run python -m eval.intent_bench --limit 20        # quick smoke

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

import yaml

from agentalloy.lm_client import OpenAICompatClient
from agentalloy.retrieval.lm_assist import (
    FragmentScorer,
    LMAssistConfig,
    LMAssistMode,
    LMAssistOutcome,
)
from agentalloy.signals.classifier import (
    _DEFAULT_RERANK_THRESHOLD,
    _INTENT_INSTRUCT,
    _INTENT_REFERENCES,
    _INTENT_TASK_DESCRIPTIONS,
    _cosine,
    _has_negation,
)

INTENTS = ["completion", "approval", "redirection"]
LABELS = [*INTENTS, "none"]
DIFFICULTIES = ["clear", "paraphrase", "negation", "scoped"]

_EMBED_URL = "http://localhost:11434"
_EMBED_MODEL = "qwen3-embedding:0.6b"
_RERANK_URL = "http://127.0.0.1:60001"
_RERANK_MODEL = "qwen3-reranker-0.6b"
_COSINE_THRESHOLD = 0.75

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
    threshold: float,
    *,
    guard: bool = True,
) -> list[Scored]:
    """Score each utterance against the three intent descriptions, production-faithful.

    Builds the same ``FragmentScorer`` the runtime builds (intent instruct,
    utterance-as-query) and scores all three intent descriptions in one batch.
    When ``guard`` is set, a negated utterance (``classifier._has_negation``)
    has every intent score zeroed — exactly the runtime veto, which forces a
    ``none`` prediction at any positive threshold.
    """
    config = LMAssistConfig(
        mode=LMAssistMode.ARBITRATE,
        url=_RERANK_URL,
        timeout_ms=5000,  # generous; benchmark, not the 300ms runtime budget
        keep_threshold=0.0,
        model=_RERANK_MODEL,
        instruct=_INTENT_INSTRUCT,
    )
    scorer = FragmentScorer(config)
    descs = [_INTENT_TASK_DESCRIPTIONS[i] for i in INTENTS]
    out: list[Scored] = []
    try:
        for it in items:
            t0 = time.perf_counter()
            if guard and _has_negation(it.text):
                scores = {i: 0.0 for i in INTENTS}
            else:
                res = scorer.score(it.text, descs)
                if res.outcome is not LMAssistOutcome.HIT or len(res.scores) != len(INTENTS):
                    raise RuntimeError(f"reranker non-HIT outcome {res.outcome} for {it.text!r}")
                scores = dict(zip(INTENTS, res.scores, strict=True))
            dt = (time.perf_counter() - t0) * 1000.0
            out.append(Scored(it, scores, predict_from_scores(scores, threshold), dt))
    finally:
        scorer.close()
    return out


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


def per_intent_prf_independent(scored: list[Scored], intent: str, threshold: float) -> PRF:
    """P/R/F1 for one intent decided *independently* (``score >= threshold``).

    This is the production framing: every runtime gate queries exactly one
    intent, so the decision never sees the other intents' scores (no argmax).
    """
    tp = fp = fn = 0
    for s in scored:
        gold_pos = s.item.label == intent
        pred_pos = s.scores[intent] >= threshold
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


def macro_f1_independent(scored: list[Scored], threshold: float) -> float:
    return statistics.mean(per_intent_prf_independent(scored, i, threshold).f1 for i in INTENTS)


def sweep_independent(
    scored: list[Scored], lo: float = 0.0, hi: float = 1.0, step: float = 0.02
) -> list[tuple[float, float]]:
    """Return [(threshold, per-intent macro_f1)] across the sweep."""
    rows: list[tuple[float, float]] = []
    t = lo
    while t <= hi + 1e-9:
        rows.append((round(t, 3), macro_f1_independent(scored, t)))
        t += step
    return rows


def apply_negation_guard(scored: list[Scored], threshold: float) -> list[Scored]:
    """Derive guarded predictions from un-guarded scores (no re-query).

    A negated utterance has every intent score zeroed, mirroring the runtime
    veto. Deterministic, so this reproduces ``run_reranker(guard=True)`` exactly
    without a second pass over the live reranker.
    """
    out: list[Scored] = []
    for s in scored:
        scores = {i: 0.0 for i in INTENTS} if _has_negation(s.item.text) else s.scores
        out.append(Scored(s.item, scores, predict_from_scores(scores, threshold), s.latency_ms))
    return out


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


def _operating_threshold(name: str) -> float:
    """The production operating threshold for a backend (used by the per-intent
    framing): cosine runs at 0.75, the reranker variants at the calibrated default."""
    return _COSINE_THRESHOLD if name.startswith("cosine") else _DEFAULT_RERANK_THRESHOLD


def _fmt_prf_table(scored: list[Scored]) -> list[str]:
    lines = [f"  {'intent':<12} {'P':>6} {'R':>6} {'F1':>6}  (tp/fp/fn)"]
    for i in INTENTS:
        p = per_intent_prf(scored, i)
        lines.append(
            f"  {i:<12} {p.precision:>6.3f} {p.recall:>6.3f} {p.f1:>6.3f}  ({p.tp}/{p.fp}/{p.fn})"
        )
    lines.append(f"  {'macro-F1':<12} {'':>6} {'':>6} {macro_f1(scored):>6.3f}")
    return lines


def _fmt_prf_table_independent(scored: list[Scored], threshold: float) -> list[str]:
    lines = [f"  {'intent':<12} {'P':>6} {'R':>6} {'F1':>6}  (tp/fp/fn)  [indep @ {threshold:.2f}]"]
    for i in INTENTS:
        p = per_intent_prf_independent(scored, i, threshold)
        lines.append(
            f"  {i:<12} {p.precision:>6.3f} {p.recall:>6.3f} {p.f1:>6.3f}  ({p.tp}/{p.fp}/{p.fn})"
        )
    lines.append(
        f"  {'macro-F1':<12} {'':>6} {'':>6} {macro_f1_independent(scored, threshold):>6.3f}"
    )
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
    macro_row = (
        "| macro-F1 (argmax) | " + " | ".join(f"{macro_f1(r.scored):.3f}" for r in reports) + " |"
    )
    lines.append(macro_row)
    indep_row = (
        "| **macro-F1 (per-intent, prod)** | "
        + " | ".join(
            f"**{macro_f1_independent(r.scored, _operating_threshold(r.name)):.3f}**"
            for r in reports
        )
        + " |"
    )
    lines.append(indep_row)
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
        lines.append("### per-intent P/R/F1 (argmax)")
        lines.append("```")
        lines.extend(_fmt_prf_table(r.scored))
        lines.append("```")
        op_thr = _operating_threshold(r.name)
        lines.append(f"### per-intent P/R/F1 (production framing, indep @ {op_thr:.2f})")
        lines.append("```")
        lines.extend(_fmt_prf_table_independent(r.scored, op_thr))
        ind_best_thr, ind_best_f1 = max(sweep_independent(r.scored), key=lambda x: x[1])
        lines.append(
            f"  (per-intent macro-F1 peaks @ {ind_best_thr:.2f} = {ind_best_f1:.3f}; "
            f"operating @ {op_thr:.2f})"
        )
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
    cosine_report.sweep = sweep_thresholds(cosine_scored)

    # --- reranker backend (production) ---
    # Score once without the guard (one campaign against :60001); the guard is a
    # deterministic in-process veto, so the guarded variant is derived offline.
    print("running reranker backend (production: query orientation)...")
    rerank_noguard = run_reranker(items, threshold=_DEFAULT_RERANK_THRESHOLD, guard=False)
    rerank_guard = apply_negation_guard(rerank_noguard, _DEFAULT_RERANK_THRESHOLD)

    # Display the argmax tables/confusion at the production operating threshold so
    # the whole report reads at one operating point; the argmax sweep subsection
    # still surfaces where argmax peaks (typically much lower) for diagnostics.
    op_thr = _DEFAULT_RERANK_THRESHOLD
    noguard_report = BackendReport(
        "reranker[no-guard]",
        rescore_predictions(rerank_noguard, op_thr),
        op_thr,
        sweep=sweep_thresholds(rerank_noguard),
    )
    rerank_report = BackendReport(
        "reranker",
        rescore_predictions(rerank_guard, op_thr),
        op_thr,
        sweep=sweep_thresholds(rerank_guard),
    )

    reports = [cosine_report, noguard_report, rerank_report]
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
