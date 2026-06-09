"""Authoring pipeline CLI.

Usage::

    python -m agentalloy.authoring author <source-dir>          # SKILL.md → pending-qa/
    python -m agentalloy.authoring qa                           # pending-qa/ → pending-review/ | ...
    python -m agentalloy.authoring run <source-dir>             # swap-batched (default)
    python -m agentalloy.authoring run <source-dir> --single-skill  # per-skill convergence
    python -m agentalloy.authoring summary                      # print pipeline state

The swap-batched ``run`` is designed for single-GPU hosts that share an Ollama
endpoint between author and critic. It warms the right model once per phase
(author → critic → author → ...) so each batch hits a fully resident model.

After ``qa`` or ``run``, the operator reviews ``pending-review/*.yaml`` and
the sibling ``.qa.md`` reports, then runs::

    python -m agentalloy.ingest skill-source/pending-review

to load approved skills into LadybugDB.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agentalloy.authoring.driver import run_author, run_revise
from agentalloy.authoring.paths import PipelinePaths, default_paths
from agentalloy.authoring.pipeline import SkillResult, run_per_skill, summarize_results
from agentalloy.authoring.qa_gate import GateResult, run_qa
from agentalloy.config import get_settings
from agentalloy.lm_client import warmup_ollama
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import open_or_create

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_RUNTIME = 2


def _repo_root() -> Path:
    """Walk up from this file to the repo root (where ``fixtures/`` lives)."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "fixtures").is_dir() and (parent / "src").is_dir():
            return parent
    raise RuntimeError("could not locate repo root")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.authoring",
        description="Author + QA pipeline for populating LadybugDB from SKILL.md sources.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_author = sub.add_parser("author", help="Run the Author LLM over SKILL.md files")
    p_author.add_argument("source_dir", help="Directory containing SKILL.md files")

    sub.add_parser("qa", help="Run the QA gate over all drafts in pending-qa/")

    sub.add_parser(
        "revise",
        help="Re-author drafts in pending-revision/ using critic feedback",
    )

    p_run = sub.add_parser(
        "run",
        help=(
            "Swap-batched pipeline (default): author-all → swap → qa-all → swap → "
            "revise-all. Designed for single-GPU hosts where author/critic can't "
            "coexist. Warms the right Ollama model before each phase."
        ),
    )
    p_run.add_argument("source_dir", help="Directory containing SKILL.md files")
    p_run.add_argument(
        "--max-rounds",
        type=int,
        default=4,
        help="Safety ceiling on QA↔revise iterations (default: 4 = 1 initial + 3 bounces)",
    )
    p_run.add_argument(
        "--single-skill",
        action="store_true",
        help=(
            "Per-skill mode: each SKILL.md converges (author↔qa↔revise) before the "
            "next begins. Only useful when both models are pre-loaded on separate "
            "endpoints — incurs heavy swap overhead on a single-GPU host."
        ),
    )
    p_run.add_argument(
        "--no-warmup",
        action="store_true",
        help="Skip Ollama model warmup before each phase (LM Studio / non-Ollama backends).",
    )

    sub.add_parser("summary", help="Report current pipeline state")

    args = parser.parse_args(argv)
    repo_root = _repo_root()
    paths = default_paths(repo_root)

    if args.cmd == "author":
        return _cmd_author(Path(args.source_dir), repo_root, paths)
    if args.cmd == "qa":
        return _cmd_qa(repo_root, paths)
    if args.cmd == "revise":
        return _cmd_revise(repo_root, paths)
    if args.cmd == "run":
        if args.single_skill:
            return _cmd_run_per_skill(Path(args.source_dir), repo_root, paths)
        return _cmd_run_batched(
            Path(args.source_dir),
            repo_root,
            paths,
            args.max_rounds,
            warmup=not args.no_warmup,
        )
    if args.cmd == "summary":
        return _cmd_summary(paths)
    return EXIT_USAGE


def _cmd_author(source_dir: Path, repo_root: Path, paths: PipelinePaths) -> int:
    if not source_dir.is_dir():
        print(f"error: source directory not found: {source_dir}", file=sys.stderr)
        return EXIT_USAGE
    results = run_author(source_dir, repo_root, paths=paths)
    ok = sum(1 for r in results if r.error is None)
    err = sum(1 for r in results if r.error is not None)
    print(f"authored: {ok} ok, {err} error(s)  →  {paths.pending_qa}")
    return EXIT_OK if err == 0 else EXIT_RUNTIME


def _cmd_qa(repo_root: Path, paths: PipelinePaths) -> int:
    settings = get_settings()
    settings.ensure_data_dirs()
    with (
        LadybugStore(settings.ladybug_db_path) as store,
        open_or_create(settings.duckdb_path) as vector_store,
    ):
        results = run_qa(paths, repo_root=repo_root, store=store, vector_store=vector_store)
    _print_qa_summary(results, paths)
    return EXIT_OK


def _cmd_revise(repo_root: Path, paths: PipelinePaths) -> int:
    results = run_revise(repo_root, paths)
    ok = sum(1 for r in results if r.error is None)
    err = sum(1 for r in results if r.error is not None)
    print(f"revised: {ok} ok, {err} error(s)  →  {paths.pending_qa}")
    return EXIT_OK if err == 0 else EXIT_RUNTIME


def _cmd_run_per_skill(source_dir: Path, repo_root: Path, paths: PipelinePaths) -> int:
    """Per-skill pipeline: each SKILL.md converges before the next begins."""
    if not source_dir.is_dir():
        print(f"error: source directory not found: {source_dir}", file=sys.stderr)
        return EXIT_USAGE
    settings = get_settings()
    settings.ensure_data_dirs()
    with (
        LadybugStore(settings.ladybug_db_path) as store,
        open_or_create(settings.duckdb_path) as vector_store,
    ):
        results = run_per_skill(
            source_dir, repo_root, paths, store=store, vector_store=vector_store
        )
    _print_per_skill_summary(results, paths)
    return EXIT_OK


def _cmd_run_batched(
    source_dir: Path,
    repo_root: Path,
    paths: PipelinePaths,
    max_rounds: int,
    *,
    warmup: bool = True,
) -> int:
    """Swap-batched pipeline: author-all → swap → qa-all → swap → revise-all.

    Designed for single-GPU hosts where author and critic models can't coexist.
    Before each phase, fires an Ollama warmup against the model that phase
    needs — the first call after a swap takes ~20s for a 20GB GGUF, so doing
    it once per phase (instead of once per skill) keeps wall-clock sane.

    A partial Author failure (``EXIT_RUNTIME``) does NOT short-circuit the rest
    of the pipeline — drafts that *did* make it into pending-qa still get
    QA'd and routed. Only a usage-level failure aborts.
    """
    settings = get_settings()
    ac = settings.require_authoring_config()

    def _warm(role: str, base_url: str, model: str) -> None:
        if not warmup:
            return
        logger.info("warmup: loading %s model %r on %s", role, model, base_url)
        try:
            warmup_ollama(base_url, model)
        except Exception as e:  # noqa: BLE001 — surface but continue
            logger.warning("warmup failed (continuing): %s", e)

    _warm("author", ac.lm_base_url, ac.model)
    rc = _cmd_author(source_dir, repo_root, paths)
    if rc == EXIT_USAGE:
        return rc

    for round_num in range(1, max_rounds + 1):
        print(f"\n=== QA round {round_num} ===")
        _warm("critic", ac.lm_studio_base_url, ac.critic_model)
        rc = _cmd_qa(repo_root, paths)
        if rc != EXIT_OK:
            return rc

        pending_rev = list(paths.pending_revision.glob("*.yaml"))
        if not pending_rev:
            print("\nno drafts in pending-revision — loop converged.")
            break

        print(f"\n=== revision round {round_num} ({len(pending_rev)} draft(s)) ===")
        _warm("author", ac.lm_base_url, ac.model)
        rc = _cmd_revise(repo_root, paths)
        if rc != EXIT_OK:
            return rc
    else:
        print(f"\nmax-rounds={max_rounds} reached — remaining drafts stay in pending-revision")

    print()
    return _cmd_summary(paths)


def _print_per_skill_summary(results: list[SkillResult], paths: PipelinePaths) -> None:
    counts = summarize_results(results)
    print()
    print(f"Per-skill pipeline complete — {len(results)} skill(s) processed")
    for verdict in ("approve", "revise-exhausted", "reject", "needs-human", "error"):
        n = counts.get(verdict, 0)
        if n:
            print(f"  {verdict:20s} {n}")
    print()
    print(f"Next: review {paths.pending_review}/*.yaml and .qa.md reports.")
    print(f"      Then:  python -m agentalloy.ingest {paths.pending_review}")


def _cmd_summary(paths: PipelinePaths) -> int:
    counts = {
        "pending-qa": len(list(paths.pending_qa.glob("*.yaml"))),
        "pending-review": len(list(paths.pending_review.glob("*.yaml"))),
        "pending-revision": len(list(paths.pending_revision.glob("*.yaml"))),
        "rejected": len(list(paths.rejected.glob("*.yaml"))),
        "needs-human": len(list(paths.needs_human.glob("*.yaml"))),
    }
    print(f"Pipeline root: {paths.root}")
    for name, n in counts.items():
        print(f"  {name:18s}  {n}")
    return EXIT_OK


def _print_qa_summary(results: list[GateResult], paths: PipelinePaths) -> None:
    buckets: dict[str, list[GateResult]] = {}
    for r in results:
        buckets.setdefault(r.verdict, []).append(r)
    print()
    print(f"QA complete — {len(results)} draft(s) processed")
    for verdict in ("approve", "revise", "reject", "needs-human"):
        items = buckets.get(verdict, [])
        if not items:
            continue
        print(f"\n  {verdict} ({len(items)}):")
        for r in items:
            print(f"    {r.skill_id}  →  {r.final_dir.name}/{r.draft_path.name}")
            if r.report_path:
                print(f"      report: {r.report_path}")
    print()
    print(f"Next: review {paths.pending_review}/*.yaml and .qa.md reports.")
    print(f"      Then:  python -m agentalloy.ingest {paths.pending_review}")


if __name__ == "__main__":
    sys.exit(main())
