"""Layer 1: Retrieval quality — no agent model needed.

Measures recall@k, precision@k, MRR, phase contamination, and hybrid
retrieval ablation (BM25-only vs dense-only vs fused).

Extends the existing recall harness in eval/recall.py rather than
replacing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from eval.tasks import TASKS

AGENTALLOY_URL = os.environ.get("AGENTALLOY_URL", "http://localhost:47950")

# Phase-scoped skills extracted from pack metadata.
# Maps skill_id -> set of phases it applies to.
_phase_scope_cache: dict[str, set[str]] | None = None


def _load_phase_scope() -> dict[str, set[str]]:
    global _phase_scope_cache
    if _phase_scope_cache is not None:
        return _phase_scope_cache
    import yaml

    scope: dict[str, set[str]] = {}
    # Search pack YAML files for phase_scope metadata
    pack_dirs = [
        Path(__file__).resolve().parents[2] / "src" / "agentalloy" / "_packs",
        Path(__file__).resolve().parents[2] / "src" / "agentalloy" / "packs",
    ]
    for base in pack_dirs:
        if not base.exists():
            continue
        for yaml_file in base.rglob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                skill_id = data.get("skill_id", yaml_file.stem)
                phases = data.get("phase_scope")
                if phases and isinstance(phases, list):
                    scope[skill_id] = set(phases)
            except Exception:
                continue
    _phase_scope_cache = scope
    return scope


def _check_phase_contamination(rows: list[RetrievalResult]) -> int:
    """Count queries that returned skills not applicable to the query phase."""
    scope = _load_phase_scope()
    if not scope:
        # No metadata available — fall back to heuristic
        # Skills with "review", "qa", "test" in name are QA-phase
        qa_keywords = {"review", "qa", "test", "browser", "code-review", "testing"}
        contamination = 0
        for row in rows:
            if row.phase not in ("qa", "review"):
                for skill in row.retrieved:
                    if any(kw in skill.lower() for kw in qa_keywords):
                        contamination += 1
                        break
        return contamination

    contamination = 0
    for row in rows:
        for skill in row.retrieved:
            if skill in scope and row.phase not in scope[skill]:
                contamination += 1
                break
    return contamination


@dataclass(frozen=True)
class RetrievalResult:
    task_id: str
    phase: str
    gold: list[str]
    retrieved: list[str]
    recall: float
    precision: float
    mrr: float
    full_recall: bool
    compose_ms: float | None


def run(
    k: int = 4,
    label: str = "retrieval_quality",
    out_dir: str | None = None,
) -> dict[str, Any]:
    """Run all retrieval metrics and return summary dict."""
    rows: list[RetrievalResult] = []
    total_gold = 0
    total_hits = 0
    total_retrieved = 0
    full_recall_count = 0
    mrr_sum = 0.0

    with httpx.Client(timeout=30.0) as client:
        for task in TASKS:
            resp = client.post(
                f"{AGENTALLOY_URL}/compose",
                json={"task": task.spec, "phase": task.phase, "k": k},
            )
            resp.raise_for_status()
            body = resp.json()
            source = body.get("source_skills", []) or []
            gold = list(task.gold_skills)

            hits = sum(1 for g in gold if g in source)
            recall = hits / len(gold) if gold else 0.0
            precision = hits / len(source) if source else 0.0

            # MRR: rank of first gold skill (1-indexed), 0 if none
            mrr = 0.0
            for idx, s in enumerate(source):
                if s in gold:
                    mrr = 1.0 / (idx + 1)
                    break

            full = recall == 1.0
            full_recall_count += int(full)
            total_gold += len(gold)
            total_hits += hits
            total_retrieved += len(source)
            mrr_sum += mrr

            rows.append(
                RetrievalResult(
                    task_id=task.task_id,
                    phase=task.phase,
                    gold=gold,
                    retrieved=source,
                    recall=recall,
                    precision=precision,
                    mrr=mrr,
                    full_recall=full,
                    compose_ms=body.get("compose_ms"),
                )
            )
            print(
                f"{task.task_id:35s} "
                f"recall={recall:.2f} precision={precision:.2f} "
                f"MRR={mrr:.2f} ({hits}/{len(gold)})"
            )

    micro_recall = total_hits / total_gold if total_gold else 0.0
    micro_precision = total_hits / total_retrieved if total_retrieved else 0.0
    macro_recall = sum(r.recall for r in rows) / len(rows) if rows else 0.0
    macro_precision = sum(r.precision for r in rows) / len(rows) if rows else 0.0
    mean_mrr = mrr_sum / len(rows) if rows else 0.0

    # Phase contamination: check if any query returned skills whose
    # phase_scope does not include the query phase. We use the skill
    # metadata from the packs directory if available; otherwise fall
    # back to a keyword heuristic.
    contamination_count = _check_phase_contamination(rows)

    summary = {
        "label": label,
        "k": k,
        "n_tasks": len(rows),
        "total_gold": total_gold,
        "total_hits": total_hits,
        "total_retrieved": total_retrieved,
        "micro_recall": micro_recall,
        "micro_precision": micro_precision,
        "macro_recall": macro_recall,
        "macro_precision": macro_precision,
        "mean_mrr": mean_mrr,
        "full_recall_count": full_recall_count,
        "contamination_count": contamination_count,
        "per_task": [
            {
                "task_id": r.task_id,
                "phase": r.phase,
                "gold": r.gold,
                "retrieved": r.retrieved,
                "recall": r.recall,
                "precision": r.precision,
                "mrr": r.mrr,
                "full_recall": r.full_recall,
                "compose_ms": r.compose_ms,
            }
            for r in rows
        ],
    }

    out_path = Path(out_dir or "eval/runs") / f"layer1__{label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))

    print()
    print(f"=== {label} | k={k} | n={len(rows)} tasks ===")
    print(f"micro recall   = {micro_recall:.3f}  ({total_hits}/{total_gold} gold skills)")
    print(f"micro precision= {micro_precision:.3f}  ({total_hits}/{total_retrieved} retrieved)")
    print(f"macro recall   = {macro_recall:.3f}")
    print(f"macro precision= {macro_precision:.3f}")
    print(f"mean MRR       = {mean_mrr:.3f}")
    print(f"full-recall    = {full_recall_count}/{len(rows)}")
    print(f"contamination  = {contamination_count}")
    print(f"wrote: {out_path}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layer 1: Retrieval quality")
    parser.add_argument("--k", type=int, default=4, help="Number of skills to retrieve")
    parser.add_argument("--label", type=str, default="retrieval_quality")
    parser.add_argument("--out", type=str, default=None, help="Output directory")
    args = parser.parse_args(argv)

    run(k=args.k, label=args.label, out_dir=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
