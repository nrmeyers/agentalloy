"""Per-skill pipeline: author → [qa ↔ revise] → converge, one skill at a time.

Replaces the stage-batched ``run`` flow (author-all then qa-all then revise-all).
Per-skill gives faster first-result, crash-tolerant partial success, and cleaner
bounce accounting.

Public API
----------

- :func:`process_one_skill` — drives a single SKILL.md through authoring and
  the full QA loop until a terminal verdict (approve / reject / needs-human)
  or the bounce budget is exhausted.
- :func:`run_per_skill` — walks a source tree, calling ``process_one_skill``
  for each SKILL.md discovered.

Granular subcommands (``author``, ``qa``, ``revise``) still work stage-wise
for operators who want to move batches between stages manually.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from agentalloy.authoring.driver import (
    author_one,
    discover_skill_md,
    load_authoring_prompt,
    revise_one,
)
from agentalloy.authoring.paths import PipelinePaths
from agentalloy.authoring.prompt_loader import load_prompt
from agentalloy.authoring.qa_gate import (
    GateResult,
    load_bounces,
    qa_one,
    save_bounces,
)
from agentalloy.config import AuthoringConfig, Settings, get_settings
from agentalloy.lm_client import OpenAICompatClient
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore, open_or_create

logger = logging.getLogger(__name__)


@dataclass
class SkillResult:
    """Outcome of a single skill running through the full pipeline."""

    source: Path
    skill_id: str | None
    final_verdict: str  # "approve" | "reject" | "needs-human" | "revise-exhausted" | "error"
    final_dir: Path | None
    rounds: int  # number of QA passes executed
    last_gate: GateResult | None
    error: str | None = None


def process_one_skill(
    source: Path,
    *,
    system_prompt: str,
    qa_prompt: str,
    paths: PipelinePaths,
    store: LadybugStore,
    vector_store: VectorStore,
    lm_client: OpenAICompatClient,
    embed_client: OpenAICompatClient,
    bounces: dict[str, int],
    settings: Settings,
    ac: AuthoringConfig,
) -> SkillResult:
    """Author → [qa → revise]* loop for one SKILL.md. Converges per skill."""
    # --- author ---
    draft = author_one(
        source,
        client=lm_client,
        model=ac.model,
        system_prompt=system_prompt,
        paths=paths,
    )
    if draft.error or draft.draft_path is None:
        return SkillResult(
            source=source,
            skill_id=draft.skill_id,
            final_verdict="error",
            final_dir=None,
            rounds=0,
            last_gate=None,
            error=draft.error or "author returned no draft path",
        )

    current_draft: Path = draft.draft_path
    skill_id = draft.skill_id or source.stem
    last_gate: GateResult | None = None

    # --- QA ↔ revise loop ---
    # One initial QA pass + up to ``budget`` revise rounds. ``qa_one`` increments
    # bounces[skill_id] when it routes a file as "revise"; ``route()`` escalates
    # to needs-human when the next revise would exceed budget.
    max_iterations = settings.bounce_budget + 1
    for round_num in range(1, max_iterations + 1):
        logger.info("  round %d: QA", round_num)
        gate = qa_one(
            current_draft,
            store=store,
            vector_store=vector_store,
            lm_client=lm_client,
            embed_client=embed_client,
            qa_prompt=qa_prompt,
            paths=paths,
            hard_threshold=settings.dedup_hard_threshold,
            soft_threshold=settings.dedup_soft_threshold,
            embedding_model=ac.embedding_model,
            critic_model=ac.critic_model,
            budget=settings.bounce_budget,
            bounces=bounces,
        )
        last_gate = gate
        logger.info("  round %d: verdict=%s → %s", round_num, gate.verdict, gate.final_dir.name)

        # Terminal verdicts: done.
        if gate.verdict in ("approve", "reject", "needs-human"):
            return SkillResult(
                source=source,
                skill_id=skill_id,
                final_verdict=gate.verdict,
                final_dir=gate.final_dir,
                rounds=round_num,
                last_gate=gate,
            )

        # ``revise`` — re-author and loop.
        revise_result = revise_one(
            gate.draft_path,
            client=lm_client,
            model=ac.model,
            system_prompt=system_prompt,
            paths=paths,
        )
        if revise_result.error or revise_result.draft_path is None:
            logger.warning("  round %d: revise failed: %s", round_num, revise_result.error)
            return SkillResult(
                source=source,
                skill_id=skill_id,
                final_verdict="error",
                final_dir=gate.final_dir,
                rounds=round_num,
                last_gate=gate,
                error=f"revise: {revise_result.error}",
            )
        current_draft = revise_result.draft_path

    # Exhausted the loop without a terminal verdict (unexpected — route should
    # have escalated by now). Treat as needs-human.
    logger.warning("loop exhausted without terminal verdict for %s", skill_id)
    return SkillResult(
        source=source,
        skill_id=skill_id,
        final_verdict="revise-exhausted",
        final_dir=last_gate.final_dir if last_gate else None,
        rounds=max_iterations,
        last_gate=last_gate,
    )


def run_per_skill(
    source_dir: Path,
    repo_root: Path,
    paths: PipelinePaths,
    *,
    store: LadybugStore,
    vector_store: VectorStore | None = None,
    lm_client: OpenAICompatClient | None = None,
    embed_client: OpenAICompatClient | None = None,
) -> list[SkillResult]:
    """Walk ``source_dir`` for SKILL.md files, run each through the full pipeline."""
    settings = get_settings()
    ac = settings.require_authoring_config()
    paths.ensure_all()

    owned_lm = lm_client is None
    owned_embed = embed_client is None
    owned_vs = vector_store is None
    _lm = lm_client or OpenAICompatClient(ac.lm_studio_base_url)
    _embed = embed_client or OpenAICompatClient(ac.embed_base_url)
    _vs = vector_store or open_or_create(settings.duckdb_path)

    system_prompt = load_authoring_prompt(repo_root)
    qa_fixture = repo_root / "fixtures" / "skill-qa-agent.md"
    if not qa_fixture.exists():
        raise FileNotFoundError(f"QA fixture missing: {qa_fixture}")
    qa_prompt, _prompt_version = load_prompt(qa_fixture)
    logger.debug("pipeline loaded qa prompt version=%s", _prompt_version or "(none)")

    bounces = load_bounces(paths)

    try:
        results: list[SkillResult] = []
        sources = list(discover_skill_md(source_dir))
        logger.info("per-skill pipeline: %d SKILL.md file(s) discovered", len(sources))
        for i, source in enumerate(sources, start=1):
            logger.info("[%d/%d] %s", i, len(sources), source)
            result = process_one_skill(
                source,
                system_prompt=system_prompt,
                qa_prompt=qa_prompt,
                paths=paths,
                store=store,
                vector_store=_vs,
                lm_client=_lm,
                embed_client=_embed,
                bounces=bounces,
                settings=settings,
                ac=ac,
            )
            logger.info(
                "[%d/%d] %s → %s (%d rounds)",
                i,
                len(sources),
                result.skill_id or source.name,
                result.final_verdict,
                result.rounds,
            )
            results.append(result)
            # Persist bounce state after every skill so a crash mid-batch leaves
            # a recoverable snapshot.
            save_bounces(paths, bounces)
        return results
    finally:
        if owned_lm:
            _lm.close()
        if owned_embed:
            _embed.close()
        if owned_vs:
            _vs.close()


def summarize_results(results: list[SkillResult]) -> dict[str, int]:
    """Tally results by final_verdict."""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.final_verdict] = counts.get(r.final_verdict, 0) + 1
    return counts
