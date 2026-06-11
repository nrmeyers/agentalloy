"""Mechanical (deterministic) tag linting for skill corpus quality checks.

Implements Rules R3-stem, W1, tier-ceiling, and system-empty checks.
No LLM calls — all logic is rule-based and testable in isolation.

R2 ("tag redundant with title") was removed 2026-06-11: its premise —
"already retrievable from title" — is ranking logic, but ``domain_tags``
is a hard post-retrieval filter matched by EXACT string membership
(titles never participate). A skill titled "Analytics — Cohorts" MUST
carry the ``analytics`` tag for ``domain_tags=["analytics"]`` queries to
find it; R2 flagged exactly those tags as deletable (635 of the 840
corpus warnings were R2 false positives).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentalloy.ingest import TAG_POLICY_BY_TIER, WORKFLOW_POSITION_MARKERS, WORKFLOW_TAG_POLICY


@dataclass(frozen=True)
class TagVerdict:
    tag: str  # the tag being judged
    rule: str  # e.g. "R2", "R3-stem", "W1", "tier-ceiling", "system-empty"
    verdict: str  # "redundant_with_title" | "synonym_of:<other>" | "missing_position_marker"
    # | "over_ceiling" | "system_has_tags"
    detail: str  # human-readable explanation


def _stems(text: str) -> set[str]:
    # Short tokens are kept: the only consumer is the R3 full-set EQUALITY
    # check, where 'pr' is what distinguishes 'pr-review' from 'review'.
    # (The old len>2 filter served the retired any-overlap heuristics.)
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    suffixes = re.compile(r"(ing|tion|tions|ation|ations|ed|ment|ments|ness|ity|ies|es|s)$")
    return {suffixes.sub("", t) or t for t in tokens}


def lint_tags_mechanical(
    tags: list[str],
    skill_class: str,
    canonical_name: str,
    tier: str | None,
) -> list[TagVerdict]:
    """Run mechanical tag lint rules; return a (possibly empty) list of TagVerdicts."""
    verdicts: list[TagVerdict] = []

    if skill_class == "system":
        for tag in tags:
            verdicts.append(
                TagVerdict(
                    tag=tag,
                    rule="system-empty",
                    verdict="system_has_tags",
                    detail="system skills must have domain_tags: [] — tags are ignored during retrieval",
                )
            )
        return verdicts

    # --- Shared checks for domain and workflow ---

    # Rule 3-stem: morphological duplicates between tags. Equality of full
    # stem sets only ('webhook' vs 'webhooks') — any-intersection flagged
    # distinct concepts that share one stem ('clean-code' vs
    # 'code-simplification') and produced ~75% false positives. Note tags are
    # exact-match filter keys, so even true synonyms aren't interchangeable;
    # the verdict asks for a corpus-canonical form, not silent deletion.
    for i, t1 in enumerate(tags):
        for t2 in tags[i + 1 :]:
            if t1 == t2:
                continue
            s1, s2 = _stems(t1), _stems(t2)
            if s1 and s1 == s2:
                verdicts.append(
                    TagVerdict(
                        tag=t2,
                        rule="R3-stem",
                        verdict=f"synonym_of:{t1}",
                        detail=(
                            f"'{t2}' and '{t1}' are morphological duplicates — pick the "
                            "corpus-canonical form (filters match tags exactly)"
                        ),
                    )
                )

    if skill_class == "domain":
        # Tier ceiling
        policy = TAG_POLICY_BY_TIER.get(tier) if tier else None
        if policy is not None and len(tags) > policy["soft_ceiling"]:
            verdicts.append(
                TagVerdict(
                    tag="(count)",
                    rule="tier-ceiling",
                    verdict="over_ceiling",
                    detail=(
                        f"{len(tags)} tags exceeds {tier} ceiling of "
                        f"{policy['soft_ceiling']} — trim or add tags_rationale"
                    ),
                )
            )

    elif skill_class == "workflow":
        # Tier ceiling (uses WORKFLOW_TAG_POLICY, not tier-keyed)
        policy = WORKFLOW_TAG_POLICY
        if len(tags) > policy["soft_ceiling"]:
            verdicts.append(
                TagVerdict(
                    tag="(count)",
                    rule="tier-ceiling",
                    verdict="over_ceiling",
                    detail=(
                        f"{len(tags)} tags exceeds workflow ceiling of "
                        f"{policy['soft_ceiling']} — trim or add tags_rationale"
                    ),
                )
            )

        # Rule W1: position marker required
        if not any(tag in WORKFLOW_POSITION_MARKERS for tag in tags):
            verdicts.append(
                TagVerdict(
                    tag="(none)",
                    rule="W1",
                    verdict="missing_position_marker",
                    detail=(
                        "workflow skill needs at least one position marker from "
                        "WORKFLOW_POSITION_MARKERS (e.g. phase:spec, phase:build, sdd, code-review)"
                    ),
                )
            )

    return verdicts
