"""Calibrate the Stage B keep-threshold (LM_ASSIST_KEEP_THRESHOLD) on real fragments.

Stage B keeps fused fragments whose yes-probability clears
``LM_ASSIST_KEEP_THRESHOLD`` (default 0.05). That default is a placeholder: the
synthetic score landscape from the model bake-off was bimodal (relevant >= 0.51,
noise <= 0.001), but real fused fragments — overview paragraphs, sibling-skill
near-misses, partially-relevant setup steps — land in between. Pick the
threshold on *real* compose-candidate fragments, not synthetic pairs.

What it does
------------
For each probe query it runs the live retrieval pipeline (the same dense + BM25
+ RRF + card-boost path /compose uses, via ``retrieve_domain_candidates`` with
``raw_scores=True`` so no deterministic selection collapses the pool), takes the
top ``--top`` fused fragments, and scores each against the task with the Stage B
scorer. It prints, per query, every fragment's (skill_id, fragment_type, score)
sorted descending, plus the global score distribution (min / quartiles / max and
a coarse histogram). A human reads the gap between the relevant cluster and the
noise floor and sets LM_ASSIST_KEEP_THRESHOLD just below the relevant cluster.

Requirements
------------
* A running embedding server (the usual EMBEDDING_* / LM env the service uses)
  and a built corpus (DuckDB + LadybugDB) — same prerequisites as ``/compose``.
* The Stage B reranker reachable at LM_ASSIST_RERANK_URL (default
  http://127.0.0.1:60001). LM_ASSIST does NOT need to be ``arbitrate`` for this
  script — it builds the scorer directly, bypassing the mode gate.

Usage
-----
    uv run python -m eval.calibrate_lm_assist
    uv run python -m eval.calibrate_lm_assist --phase build --top 12

The default probes are the three this design was shaped around: a webhook
signature task (expects a tight relevant cluster), an underspecified blog-site
task (expects framework cards to surface), and a Redshift table task (the
"inject nothing" canary — expect a low, flat distribution).
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass

from agentalloy.config import get_settings
from agentalloy.embed_provider import get_embed_client
from agentalloy.retrieval.domain import retrieve_domain_candidates
from agentalloy.retrieval.embedding_errors import EmbeddingErrorResult
from agentalloy.retrieval.lm_assist import FragmentScorer, load_config, max_candidates
from agentalloy.runtime_state import load_runtime_cache
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import open_or_create

_DEFAULT_PROBES: list[tuple[str, str]] = [
    ("webhook", "set up webhook signature verification for my API"),
    ("blog", "I want to build a website that is a blog"),
    ("redshift", "design a redshift table with proper dist and sort keys"),
]


@dataclass
class _Scored:
    probe: str
    skill_id: str
    fragment_type: str
    score: float


def _histogram(values: list[float], bins: int = 10) -> str:
    if not values:
        return "(no scores)"
    lines: list[str] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        # Last bin is inclusive of 1.0.
        n = sum(1 for v in values if (lo <= v < hi) or (i == bins - 1 and v >= hi))
        lines.append(f"  [{lo:.1f}, {hi:.1f}){'  ' if i < bins - 1 else ']'} {'#' * n} ({n})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="build", help="compose phase for retrieval")
    parser.add_argument("--k", type=int, default=12, help="retrieval pool k")
    parser.add_argument(
        "--top", type=int, default=max_candidates(), help="top fused fragments scored per probe"
    )
    args = parser.parse_args(argv)

    # Open the live corpus exactly as the app lifespan does (settings-driven
    # paths), so calibration scores the same fragments /compose would assemble.
    settings = get_settings()
    embedding_model = settings.runtime_embedding_model
    store = LadybugStore(settings.ladybug_db_path)
    store.open()
    vector_store = open_or_create(settings.duckdb_path)
    lm = get_embed_client(settings)
    cache = load_runtime_cache(store)

    config = load_config()
    print(f"Stage B scorer: url={config.url} model={config.model} timeout={config.timeout_ms}ms")
    print(f"current LM_ASSIST_KEEP_THRESHOLD default={config.keep_threshold}\n")
    scorer = FragmentScorer(config)

    all_scores: list[float] = []
    rows: list[_Scored] = []
    try:
        for probe_name, task in _DEFAULT_PROBES:
            result = retrieve_domain_candidates(
                cache,
                lm,
                vector_store,
                task=task,
                phase=args.phase,  # type: ignore[arg-type]
                domain_tags=None,
                k=args.k,
                embedding_model=embedding_model,
                raw_scores=True,
            )
            if isinstance(result, EmbeddingErrorResult):
                print(f"[{probe_name}] retrieval degraded: {result.error.message}", file=sys.stderr)
                continue
            head = result.candidates[: args.top]
            documents = [f"{f.skill_id.replace('-', ' ')}: {f.content}" for f in head]
            scored = scorer.score(task, documents)
            print(f"\n=== probe: {probe_name} ({scored.outcome.value}) — {task!r} ===")
            if scored.outcome.value != "hit":
                continue
            paired = sorted(zip(head, scored.scores, strict=True), key=lambda p: p[1], reverse=True)
            for frag, score in paired:
                print(f"  {score:.4f}  {frag.skill_id}  [{frag.fragment_type}]")
                all_scores.append(score)
                rows.append(_Scored(probe_name, frag.skill_id, frag.fragment_type, score))
    finally:
        scorer.close()

    print("\n=== global score distribution ===")
    if all_scores:
        srt = sorted(all_scores)
        q = statistics.quantiles(srt, n=4) if len(srt) >= 2 else [srt[0], srt[0], srt[0]]
        print(
            f"  n={len(srt)} min={srt[0]:.4f} q25={q[0]:.4f} median={q[1]:.4f} "
            f"q75={q[2]:.4f} max={srt[-1]:.4f}"
        )
        print(_histogram(all_scores))
        print(
            "\nPick LM_ASSIST_KEEP_THRESHOLD in the gap between the relevant cluster "
            "(high scores) and the noise floor. If the histogram is bimodal, set it "
            "midway; if flat-low (e.g. the redshift probe), Stage B will keep nothing — "
            "that is the intended 'inject nothing' behaviour."
        )
    else:
        print("  (no scores collected — is the Stage B server reachable?)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
