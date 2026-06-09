"""QA gate: deterministic checks → dedup → Critic LLM → routed verdict.

Input: draft review YAMLs under ``skill-source/pending-qa/``.
Output: drafts moved to ``pending-review/``, ``pending-revision/``,
``rejected/``, or ``needs-human/`` with a sibling ``.qa.md`` report for
the operator.

Bounce tracking lives in ``skill-source/.qa-state.json`` — a simple
``{skill_id: bounce_count}`` map. After ``bounce_budget`` revisions, the
next revise verdict escalates to ``needs-human/``.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from agentalloy.authoring.dedup import dedup_candidates
from agentalloy.authoring.paths import PipelinePaths
from agentalloy.authoring.prompt_loader import load_prompt
from agentalloy.config import get_settings
from agentalloy.ingest import (
    ReviewRecord,
    _load_yaml,  # pyright: ignore[reportPrivateUsage]
    _validate,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.lm_client import LMClientError, OpenAICompatClient
from agentalloy.reads.active import get_active_fragments
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class DedupHit:
    near_dup_skill_id: str
    fragment_id: str
    score: float
    excerpt: str


@dataclass
class CriticVerdict:
    verdict: str  # "approve" | "revise" | "reject"
    summary: str
    blocking_issues: list[str] = field(default_factory=lambda: [])
    per_fragment: list[dict[str, Any]] = field(default_factory=lambda: [])
    dedup_decisions: list[dict[str, Any]] = field(default_factory=lambda: [])
    suggested_edits: str = ""
    tag_verdicts: list[dict[str, Any]] = field(default_factory=lambda: [])
    prompt_version: str = ""

    @classmethod
    def unparseable(cls, raw: str, err: str) -> CriticVerdict:
        return cls(
            verdict="needs-human",
            summary=f"critic output unparseable: {err}",
            blocking_issues=[f"raw: {raw[:500]}"],
        )


@dataclass
class GateResult:
    draft_path: Path
    skill_id: str
    final_dir: Path
    verdict: str
    schema_errors: list[str]
    hard_dup: DedupHit | None
    soft_dups: list[DedupHit]
    critic: CriticVerdict | None
    bounces: int
    report_path: Path | None


# ---------------------------------------------------------------------------
# Stage 1: deterministic
# ---------------------------------------------------------------------------


def run_deterministic(
    draft_path: Path, store: LadybugStore
) -> tuple[ReviewRecord | None, list[str]]:
    """Parse YAML, validate schema + vocab, check skill_id collision.

    Returns ``(record, errors)``. ``record`` is None iff parsing failed.
    """
    errors: list[str] = []
    try:
        record = _load_yaml(draft_path)
    except Exception as exc:
        return None, [f"parse: {exc}"]

    errors.extend(_validate(record))

    existing = store.execute(
        "MATCH (s:Skill {skill_id: $id}) RETURN s.skill_id",
        {"id": record.skill_id},
    )
    if existing:
        errors.append(
            f"skill_id '{record.skill_id}' already exists in LadybugDB "
            "(operator must decide: --force overwrite, rename, or reject)"
        )

    return record, errors


# ---------------------------------------------------------------------------
# Stage 2: dedup
# ---------------------------------------------------------------------------


def run_dedup(
    record: ReviewRecord,
    *,
    store: LadybugStore,
    vector_store: VectorStore,
    embedder: OpenAICompatClient,
    embedding_model: str,
    hard_threshold: float,
    soft_threshold: float,
) -> tuple[DedupHit | None, list[DedupHit]]:
    """Embed each fragment, query DuckDB for near-duplicates against the
    active corpus.

    Returns ``(hard_dup_or_none, soft_dups)``. A hard match short-circuits
    the gate — no critic call needed. Soft matches go to the Critic for
    judgment in the 0.80–0.92 band.

    The actual classification logic lives in
    :mod:`agentalloy.authoring.dedup`; this wrapper composes the per-skill
    fragments into the labeled-content list and converts results into the
    legacy ``DedupHit`` shape that the rest of the QA gate consumes.
    """
    fragments_to_check: list[tuple[str, str]] = []
    if record.skill_type == "system":
        # Ingest generates one guardrail fragment from raw_prose for system skills.
        fragments_to_check.append(("raw_prose", record.raw_prose))
    else:
        for frag in record.fragments:
            fragments_to_check.append((f"frag-{frag.sequence}", frag.content))

    if not fragments_to_check:
        return None, []

    try:
        result = dedup_candidates(
            labeled_contents=fragments_to_check,
            embedder=embedder,
            vector_store=vector_store,
            embedding_model=embedding_model,
            hard_similarity=hard_threshold,
            soft_similarity=soft_threshold,
        )
    except LMClientError as exc:
        logger.warning("dedup embedding failed: %s — skipping dedup stage", exc)
        return None, []
    except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
        logger.warning("dedup query failed: %s — skipping dedup stage", exc)
        return None, []

    # Map vector_store hits back to LadybugDB skill_ids + content excerpts for
    # the QA report.
    hit_ids: set[str] = set()
    if result.hardest is not None:
        hit_ids.add(result.hardest.fragment_id)
    for h in result.soft_all:
        hit_ids.add(h.fragment_id)

    fragment_meta: dict[str, tuple[str, str]] = {}  # fragment_id -> (skill_id, excerpt)
    if hit_ids:
        try:
            active = get_active_fragments(store)
        except Exception as exc:  # pyright: ignore[reportBroadExceptionCaught]
            logger.warning("dedup metadata lookup failed: %s — skipping dedup stage", exc)
            return None, []
        for af in active:
            if af.fragment_id in hit_ids:
                fragment_meta[af.fragment_id] = (af.skill_id, af.content[:240])

    def _to_legacy(hit_obj: object) -> DedupHit:
        from agentalloy.storage.vector_store import SimilarityHit

        h = cast(SimilarityHit, hit_obj)
        meta = fragment_meta.get(h.fragment_id, (h.skill_id, ""))
        return DedupHit(
            near_dup_skill_id=meta[0],
            fragment_id=h.fragment_id,
            score=1.0 - h.distance,
            excerpt=meta[1],
        )

    hard_dup = _to_legacy(result.hardest) if result.hardest is not None else None
    soft_dups = [_to_legacy(h) for h in result.soft_all][:10]
    return hard_dup, soft_dups


# ---------------------------------------------------------------------------
# Stage 3: critic
# ---------------------------------------------------------------------------


def run_critic(
    *,
    client: OpenAICompatClient,
    model: str,
    qa_prompt: str,
    source_md: str,
    draft_yaml_text: str,
    soft_dups: list[DedupHit],
    semantic_tag_block: str = "",
) -> CriticVerdict:
    dedup_block = _format_dedup_for_prompt(soft_dups)
    user_prompt = (
        f"---SOURCE SKILL.md---\n{source_md}\n---END SOURCE---\n\n"
        f"---DRAFT REVIEW YAML---\n{draft_yaml_text}\n---END DRAFT---\n\n"
        f"---DEDUP CONTEXT (0.80-0.92 band)---\n{dedup_block}\n---END DEDUP---\n\n"
        + (f"{semantic_tag_block}\n\n" if semantic_tag_block else "")
        + "Return JSON only. Schema per your system prompt."
    )
    # LM Studio / Ollama support OpenAI-style ``json_schema`` Structured
    # Outputs; ``json_object`` is rejected. We pin the schema to the QA
    # verdict shape so the model can't return a bare ``tag_verdicts`` array.
    # The schema is permissive on inner shapes (``additional_properties``)
    # to tolerate model paraphrasing across critic models.
    qa_verdict_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
            "summary": {"type": "string"},
            "blocking_issues": {"type": "array", "items": {"type": "string"}},
            "per_fragment": {"type": "array", "items": {"type": "object"}},
            "dedup_decisions": {"type": "array", "items": {"type": "object"}},
            "suggested_edits": {"type": "string"},
            "tag_verdicts": {"type": "array", "items": {"type": "object"}},
            "prompt_version": {"type": "string"},
        },
        "required": [
            "verdict",
            "summary",
            "blocking_issues",
            "per_fragment",
            "dedup_decisions",
            "suggested_edits",
            "tag_verdicts",
        ],
    }
    try:
        raw = client.chat(
            model=model,
            system=qa_prompt,
            user=user_prompt,
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "qa_verdict",
                    "strict": True,
                    "schema": qa_verdict_schema,
                },
            },
        )
    except LMClientError as exc:
        return CriticVerdict.unparseable("<llm error>", str(exc))

    # Strip a possible ```json ... ``` fence before parsing.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")].rstrip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return CriticVerdict.unparseable(raw, f"json: {exc}")
    if not isinstance(payload, dict):
        return CriticVerdict.unparseable(raw, "top-level is not an object")

    data = cast("dict[str, Any]", payload)
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in ("approve", "revise", "reject"):
        return CriticVerdict.unparseable(raw, f"unknown verdict {verdict!r}")

    base_issues = [str(x) for x in cast(list[Any], data.get("blocking_issues") or [])]
    tag_verdict_issues = [
        f"tag [{tv.get('rule', '?')}] '{tv.get('tag', '?')}': {tv.get('verdict', '?')} — {tv.get('detail', '')}"
        for tv in cast(list[dict[str, Any]], data.get("tag_verdicts") or [])
        if tv.get("verdict", "pass") != "pass"
    ]
    return CriticVerdict(
        verdict=verdict,
        summary=str(data.get("summary", "")),
        blocking_issues=base_issues + tag_verdict_issues,
        per_fragment=cast(list[dict[str, Any]], data.get("per_fragment") or []),
        dedup_decisions=cast(list[dict[str, Any]], data.get("dedup_decisions") or []),
        suggested_edits=str(data.get("suggested_edits", "")),
        tag_verdicts=cast(list[dict[str, Any]], data.get("tag_verdicts") or []),
        prompt_version=str(data.get("prompt_version", "")),
    )


def _format_dedup_for_prompt(hits: list[DedupHit]) -> str:
    if not hits:
        return "(none — no near-duplicates in the 0.80–0.92 band)"
    lines: list[str] = []
    for h in hits:
        lines.append(
            f"- skill_id={h.near_dup_skill_id} fragment_id={h.fragment_id} "
            f"score={h.score:.3f}\n  excerpt: {h.excerpt!r}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bounce state
# ---------------------------------------------------------------------------


def load_bounces(paths: PipelinePaths) -> dict[str, int]:
    p = paths.qa_state
    if not p.exists():
        return {}
    try:
        data: Any = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in cast(dict[str, Any], data).items()}


def save_bounces(paths: PipelinePaths, bounces: dict[str, int]) -> None:
    paths.qa_state.parent.mkdir(parents=True, exist_ok=True)
    paths.qa_state.write_text(json.dumps(bounces, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route(
    *,
    draft_path: Path,
    record: ReviewRecord | None,
    schema_errors: list[str],
    hard_dup: DedupHit | None,
    soft_dups: list[DedupHit],
    critic: CriticVerdict | None,
    bounces: int,
    budget: int,
    paths: PipelinePaths,
) -> tuple[Path, str]:
    """Decide destination directory + final verdict label.

    Order of precedence:
    1. Parse failed → needs-human
    2. Schema errors → pending-revision (if under budget) else needs-human
    3. Hard dup → rejected
    4. Critic unparseable → needs-human
    5. Critic approve → pending-review
    6. Critic revise → pending-revision (if under budget) else needs-human
    7. Critic reject → rejected
    """
    paths.ensure_all()

    if record is None:
        return paths.needs_human, "needs-human"
    if schema_errors:
        if bounces < budget:
            return paths.pending_revision, "revise"
        return paths.needs_human, "needs-human"
    if hard_dup is not None:
        return paths.rejected, "reject"
    if critic is None:
        return paths.needs_human, "needs-human"
    if critic.verdict == "approve":
        return paths.pending_review, "approve"
    if critic.verdict == "revise":
        if bounces < budget:
            return paths.pending_revision, "revise"
        return paths.needs_human, "needs-human"
    if critic.verdict == "reject":
        return paths.rejected, "reject"
    return paths.needs_human, "needs-human"


# ---------------------------------------------------------------------------
# Top-level QA runner
# ---------------------------------------------------------------------------


def qa_one(
    draft_path: Path,
    *,
    store: LadybugStore,
    vector_store: VectorStore,
    lm_client: OpenAICompatClient,
    embed_client: OpenAICompatClient,
    qa_prompt: str,
    paths: PipelinePaths,
    hard_threshold: float,
    soft_threshold: float,
    embedding_model: str,
    critic_model: str,
    budget: int,
    bounces: dict[str, int],
) -> GateResult:
    record, schema_errors = run_deterministic(draft_path, store)
    skill_id = record.skill_id if record else draft_path.stem

    hard_dup: DedupHit | None = None
    soft_dups: list[DedupHit] = []
    critic: CriticVerdict | None = None

    if record is not None and not schema_errors:
        hard_dup, soft_dups = run_dedup(
            record,
            store=store,
            vector_store=vector_store,
            embedder=embed_client,
            embedding_model=embedding_model,
            hard_threshold=hard_threshold,
            soft_threshold=soft_threshold,
        )
        if hard_dup is None:
            from agentalloy.lint_tags_semantic import build_semantic_lint_block

            source_md = _find_source_md(record, draft_path)
            critic = run_critic(
                client=lm_client,
                model=critic_model,
                qa_prompt=qa_prompt,
                source_md=source_md,
                draft_yaml_text=draft_path.read_text(encoding="utf-8"),
                soft_dups=soft_dups,
                semantic_tag_block=build_semantic_lint_block(
                    record.domain_tags, record.canonical_name, record.raw_prose
                ),
            )

    current_bounces = bounces.get(skill_id, 0)
    final_dir, final_verdict = route(
        draft_path=draft_path,
        record=record,
        schema_errors=schema_errors,
        hard_dup=hard_dup,
        soft_dups=soft_dups,
        critic=critic,
        bounces=current_bounces,
        budget=budget,
        paths=paths,
    )

    moved = final_dir / draft_path.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(draft_path), str(moved))

    report = _write_report(
        moved,
        skill_id=skill_id,
        schema_errors=schema_errors,
        hard_dup=hard_dup,
        soft_dups=soft_dups,
        critic=critic,
        bounces=current_bounces,
        final_verdict=final_verdict,
    )

    if final_verdict == "revise":
        bounces[skill_id] = current_bounces + 1

    return GateResult(
        draft_path=moved,
        skill_id=skill_id,
        final_dir=final_dir,
        verdict=final_verdict,
        schema_errors=schema_errors,
        hard_dup=hard_dup,
        soft_dups=soft_dups,
        critic=critic,
        bounces=current_bounces,
        report_path=report,
    )


def run_qa(
    paths: PipelinePaths,
    *,
    repo_root: Path,
    store: LadybugStore,
    vector_store: VectorStore,
    lm_client: OpenAICompatClient | None = None,
    embed_client: OpenAICompatClient | None = None,
) -> list[GateResult]:
    """Process every draft currently in ``pending-qa/``."""
    settings = get_settings()
    ac = settings.require_authoring_config()
    paths.ensure_all()

    owned_lm = lm_client is None
    owned_embed = embed_client is None
    _lm = lm_client or OpenAICompatClient(ac.lm_studio_base_url)
    _embed = embed_client or OpenAICompatClient(ac.embed_base_url)

    qa_fixture = repo_root / "fixtures" / "skill-qa-agent.md"
    if not qa_fixture.exists():
        raise FileNotFoundError(f"QA fixture missing: {qa_fixture}")
    qa_prompt, _prompt_version = load_prompt(qa_fixture)
    logger.debug("qa_gate loaded prompt version=%s", _prompt_version or "(none)")

    bounces = load_bounces(paths)

    try:
        results: list[GateResult] = []
        drafts = sorted(paths.pending_qa.glob("*.yaml"))
        for draft in drafts:
            logger.info("QA %s", draft)
            result = qa_one(
                draft,
                store=store,
                vector_store=vector_store,
                lm_client=_lm,
                embed_client=_embed,
                qa_prompt=qa_prompt,
                paths=paths,
                hard_threshold=settings.dedup_hard_threshold,
                soft_threshold=settings.dedup_soft_threshold,
                embedding_model=ac.embedding_model,
                critic_model=ac.critic_model,
                budget=settings.bounce_budget,
                bounces=bounces,
            )
            logger.info("  → %s (%s)", result.verdict, result.final_dir.name)
            results.append(result)

        save_bounces(paths, bounces)
        return results
    finally:
        if owned_lm:
            _lm.close()
        if owned_embed:
            _embed.close()


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def _write_report(
    draft_path: Path,
    *,
    skill_id: str,
    schema_errors: list[str],
    hard_dup: DedupHit | None,
    soft_dups: list[DedupHit],
    critic: CriticVerdict | None,
    bounces: int,
    final_verdict: str,
) -> Path:
    report_path = draft_path.with_suffix(".qa.md")
    lines: list[str] = [
        f"# QA Report: {skill_id}",
        "",
        f"- **Verdict:** `{final_verdict}`",
        f"- **Draft:** `{draft_path.name}`",
        f"- **Bounces so far:** {bounces}",
        "",
    ]
    if schema_errors:
        lines += ["## Schema / vocab errors", ""]
        lines += [f"- {e}" for e in schema_errors]
        lines.append("")
    if hard_dup is not None:
        lines += [
            "## Hard duplicate (above hard threshold) — auto-rejected",
            "",
            f"- **skill_id:** `{hard_dup.near_dup_skill_id}`",
            f"- **fragment_id:** `{hard_dup.fragment_id}`",
            f"- **score:** {hard_dup.score:.3f}",
            f"- **excerpt:** {hard_dup.excerpt!r}",
            "",
        ]
    if soft_dups:
        lines += ["## Near-duplicates (0.80–0.92 band, critic ruled on each)", ""]
        for h in soft_dups:
            lines.append(
                f"- `{h.near_dup_skill_id}` / `{h.fragment_id}` "
                f"(score={h.score:.3f}) — {h.excerpt!r}"
            )
        lines.append("")
    if critic is not None:
        lines += [
            "## Critic verdict",
            "",
            f"- **Raw verdict:** `{critic.verdict}`",
            f"- **Summary:** {critic.summary}",
            "",
        ]
        if critic.blocking_issues:
            lines += ["### Blocking issues", ""]
            lines += [f"- {b}" for b in critic.blocking_issues]
            lines.append("")
        if critic.per_fragment:
            lines += ["### Per-fragment notes", ""]
            for pf in critic.per_fragment:
                seq = pf.get("sequence", "?")
                issue = pf.get("issue")
                if issue:
                    lines.append(f"- seq {seq}: {issue}")
            lines.append("")
        if critic.dedup_decisions:
            lines += ["### Dedup decisions", ""]
            for dd in critic.dedup_decisions:
                lines.append(
                    f"- `{dd.get('near_dup_skill_id')}` score={dd.get('score')} "
                    f"distinct={dd.get('distinct')} — {dd.get('reason')}"
                )
            lines.append("")
        if critic.suggested_edits:
            lines += ["### Suggested edits", "", critic.suggested_edits, ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _find_source_md(record: ReviewRecord, draft_path: Path) -> str:
    """Best-effort: recover the original SKILL.md text via ``raw_prose``.

    The draft YAML always carries the full source in ``raw_prose``, so the
    critic has the author's input even if the original filesystem path is
    no longer easily derivable.
    """
    _ = draft_path  # reserved for future: sidecar link to original SKILL.md
    return record.raw_prose or "(raw_prose empty — source unavailable)"
