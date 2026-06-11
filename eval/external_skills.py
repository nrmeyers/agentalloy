"""External-skill arm materials for the POC harness.

The ``external`` condition injects third-party skill prose verbatim — the
incumbent practice of wiring a popular off-the-shelf pack into the system
prompt. Unlike ``flat`` (same content as composed, different format), this
arm changes both content and format: it answers "does composed beat
installing a popular pack?", not "does composition beat flat injection?".

Sourcing rules (see eval/campaign-2026-06.md):

* Skills must be real, installable, third-party artifacts (anthropics/skills,
  Vercel agent skills, high-star community SKILL.md repos) — never edited,
  never authored in-house, never product corpus.
* Each registry entry records source URL + commit SHA + license so the
  condition is reproducible and auditable.
* Domain tasks map to the external skill covering their domain; generic
  tasks all receive the same fixed bundle (the context-swamping test).

Skill prose lives as files under ``eval/external/``. The registry below is
the single source of truth; ``run_poc`` refuses to start an external leg
until every selected task resolves to an existing file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

EXTERNAL_ROOT = Path(__file__).resolve().parent / "external"


@dataclass(frozen=True)
class ExternalSkill:
    name: str
    source_url: str
    commit: str  # commit SHA the file was taken at
    license: str
    filename: str  # relative to eval/external/, content injected verbatim

    @property
    def path(self) -> Path:
        return EXTERNAL_ROOT / self.filename


# Populate when sourcing external skills. Example:
#
#   "vercel-webhooks": ExternalSkill(
#       name="vercel-webhooks",
#       source_url="https://github.com/vercel/agent-skills/blob/main/skills/webhooks/SKILL.md",
#       commit="<sha>",
#       license="MIT",
#       filename="vercel-webhooks.md",
#   ),
REGISTRY: dict[str, ExternalSkill] = {
    # Domain-specific skills — genuine third-party artifacts, sourced verbatim.
    # Sourcing notes per entry; unmapped domains (4, 8) have no entry here.
    #
    # domain_1_webhook_signature: Clerk Webhooks skill from hookdeck/webhook-skills.
    #   Covers svix-id/svix-timestamp/svix-signature headers, HMAC-SHA256,
    #   signed-content tuple, ±5-minute tolerance, constant-time comparison.
    "clerk-webhooks": ExternalSkill(
        name="clerk-webhooks",
        source_url="https://github.com/hookdeck/webhook-skills/blob/da37fc751d55f042ca4eaacb48b0c37ae54b66e0/skills/clerk-webhooks/SKILL.md",
        commit="da37fc751d55f042ca4eaacb48b0c37ae54b66e0",
        license="MIT",
        filename="clerk-webhooks.md",
    ),
    # domain_2_webhook_deduplication: OpenAI Webhooks skill from hookdeck/webhook-skills.
    #   Covers webhook-id header, at-least-once delivery, idempotency via event ID.
    #   Plausible substitute: uses webhook-id for dedup (grader-aligned); lacks
    #   explicit Redis 24h TTL (grader checks redis + 24h — partial match).
    "openai-webhooks": ExternalSkill(
        name="openai-webhooks",
        source_url="https://github.com/hookdeck/webhook-skills/blob/da37fc751d55f042ca4eaacb48b0c37ae54b66e0/skills/openai-webhooks/SKILL.md",
        commit="da37fc751d55f042ca4eaacb48b0c37ae54b66e0",
        license="MIT",
        filename="openai-webhooks.md",
    ),
    # domain_3_webhook_dlq: Webhook Handler Patterns skill from hookdeck/webhook-skills.
    #   Covers DLQ, exponential backoff retry, error handling, replay capability.
    #   Plausible substitute: covers retry+DLQ concepts generically; specific
    #   schedule (immediate→5s→30s→24h) is not enumerated — partial match.
    "webhook-handler-patterns": ExternalSkill(
        name="webhook-handler-patterns",
        source_url="https://github.com/hookdeck/webhook-skills/blob/da37fc751d55f042ca4eaacb48b0c37ae54b66e0/skills/webhook-handler-patterns/SKILL.md",
        commit="da37fc751d55f042ca4eaacb48b0c37ae54b66e0",
        license="MIT",
        filename="webhook-handler-patterns.md",
    ),
    # domain_4_webhook_versioning: NO genuine third-party skill found after thorough
    # search. hookdeck/webhook-skills has no versioning/x-api-version/sunset skill.
    # Domain 4 is left unmapped — see TASK_MAPPING.
    #
    # domain_5_temporal_workflow_determinism: Official Temporal Developer skill from
    #   temporalio/skill-temporal-developer (MIT, Temporal Technologies Inc, 2026).
    #   Covers workflow determinism, history replay, datetime.now() → workflow.now(),
    #   uuid.uuid4() → workflow.uuid4(), non-determinism errors.
    "temporal-developer": ExternalSkill(
        name="temporal-developer",
        source_url="https://github.com/temporalio/skill-temporal-developer/blob/3973e73202f72cb6b157b827f270c04f96ad8c1f/SKILL.md",
        commit="3973e73202f72cb6b157b827f270c04f96ad8c1f",
        license="MIT",
        filename="temporal-developer.md",
    ),
    # domain_6_github_actions_oidc: Mastering gcloud CLI skill from
    #   SpillwaveSolutions/mastering-gcloud-commands (MIT, Richard Hightower).
    #   Covers id-token: write, google-github-actions/auth@v2, Workload Identity
    #   Federation, no long-lived service-account keys, short-lived tokens.
    #   Plausible substitute: GCP-CLI-focused skill; OIDC/WIF is a subsection
    #   but covers all grader criteria inline.
    "mastering-gcloud-commands": ExternalSkill(
        name="mastering-gcloud-commands",
        source_url="https://github.com/SpillwaveSolutions/mastering-gcloud-commands/blob/6a9099999b7819068d658559cd80abc251efd237/SKILL.md",
        commit="6a9099999b7819068d658559cd80abc251efd237",
        license="MIT",
        filename="mastering-gcloud-commands.md",
    ),
    # domain_7_dbt_incremental: Official dbt analytics engineering skill from
    #   dbt-labs/dbt-agent-skills (Apache 2.0, dbt Labs).
    #   Plausible substitute: broad analytics engineering skill; mentions incremental
    #   builds but does not explicitly teach is_incremental()/unique_key/first-run
    #   behavior — partial match. No dedicated dbt-incremental SKILL.md exists in
    #   any searched public repo.
    "dbt-analytics-engineering": ExternalSkill(
        name="dbt-analytics-engineering",
        source_url="https://github.com/dbt-labs/dbt-agent-skills/blob/2e412857db5099d668c303e589b38edd733da3be/skills/dbt/skills/using-dbt-for-analytics-engineering/SKILL.md",
        commit="2e412857db5099d668c303e589b38edd733da3be",
        license="Apache 2.0",
        filename="dbt-analytics-engineering.md",
    ),
    # domain_8_scd_type2: NO genuine third-party skill found after thorough search.
    # No public SKILL.md / cursor rule covers SCD Type 2 + surrogate key +
    # is_current / valid_from/valid_to + fact-FK pattern. Domain 8 is unmapped.
    #
    # General skills from Anthropic public repo
    "anthropic-claude-api": ExternalSkill(
        name="anthropic-claude-api",
        source_url="https://github.com/anthropics/skills/blob/57546260929473d4e0d1c1bb75297be2fdfa1949/skills/claude-api/SKILL.md",
        commit="575462609",  # First 9 chars of the commit
        license="Apache 2.0",
        filename="anthropic-claude-api.md",
    ),
    "anthropic-webapp-testing": ExternalSkill(
        name="anthropic-webapp-testing",
        source_url="https://github.com/anthropics/skills/blob/57546260929473d4e0d1c1bb75297be2fdfa1949/skills/webapp-testing/SKILL.md",
        commit="575462609",
        license="Apache 2.0",
        filename="anthropic-webapp-testing.md",
    ),
    "anthropic-skill-creator": ExternalSkill(
        name="anthropic-skill-creator",
        source_url="https://github.com/anthropics/skills/blob/57546260929473d4e0d1c1bb75297be2fdfa1949/skills/skill-creator/SKILL.md",
        commit="575462609",
        license="Apache 2.0",
        filename="anthropic-skill-creator.md",
    ),
}

# task_id -> registry names injected for that task (domain task set).
# Domains 4 (webhook versioning) and 8 (SCD Type 2) are intentionally omitted:
# no genuine third-party skill artifact was found for those topics after a thorough
# search. Running a domain external leg should either skip those tasks or treat their
# absence as a data point (external arm undefined for those two).
TASK_MAPPING: dict[str, tuple[str, ...]] = {
    "domain_1_webhook_signature": ("clerk-webhooks",),
    "domain_2_webhook_deduplication": ("openai-webhooks",),
    "domain_3_webhook_dlq": ("webhook-handler-patterns",),
    # domain_4_webhook_versioning — unmapped, no genuine skill found
    "domain_5_temporal_workflow_determinism": ("temporal-developer",),
    "domain_6_github_actions_oidc": ("mastering-gcloud-commands",),
    "domain_7_dbt_incremental": ("dbt-analytics-engineering",),
    # domain_8_scd_type2 — unmapped, no genuine skill found
}

# Fixed bundle injected for EVERY generic task — realistic static-pack
# wiring, deliberately not tailored per task.
GENERIC_BUNDLE: tuple[str, ...] = (
    "anthropic-claude-api",
    "anthropic-webapp-testing",
    "anthropic-skill-creator",
)


def skills_for(task_id: str, task_set: str) -> tuple[ExternalSkill, ...]:
    """Resolve the external skills injected for a task. Raises KeyError if unmapped."""
    names = GENERIC_BUNDLE if task_set == "generic" else TASK_MAPPING[task_id]
    return tuple(REGISTRY[name] for name in names)


def validate(task_ids: list[str], task_set: str) -> list[str]:
    """Return human-readable problems blocking an external leg (empty = good to go)."""
    problems: list[str] = []
    if task_set == "generic":
        if not GENERIC_BUNDLE:
            problems.append("GENERIC_BUNDLE is empty — no fixed bundle defined for generic tasks")
        names = set(GENERIC_BUNDLE)
    else:
        names = set()
        for task_id in task_ids:
            mapped = TASK_MAPPING.get(task_id)
            if not mapped:
                problems.append(f"no external skill mapped for task: {task_id}")
            else:
                names.update(mapped)
    for name in sorted(names):
        skill = REGISTRY.get(name)
        if skill is None:
            problems.append(f"skill named in mapping but missing from REGISTRY: {name}")
        elif not skill.path.is_file():
            problems.append(f"skill file missing on disk: {skill.path}")
    return problems


def load_external_prompt(task_id: str, task_set: str) -> str:
    """Build the external-arm system prompt: third-party prose, verbatim."""
    parts: list[str] = [
        "You are an experienced software engineer. Use the following skill "
        "guidance to answer the task that follows.\n"
    ]
    for skill in skills_for(task_id, task_set):
        parts.append(f"\n# Skill: {skill.name}\n\n{skill.path.read_text()}\n")
    return "\n".join(parts)


def manifest_entry(task_ids: list[str], task_set: str) -> dict[str, Any]:
    """Frozen record of the mapping for the run manifest (reproducibility/audit)."""
    if task_set == "generic":
        mapping: dict[str, list[str]] = {task_id: list(GENERIC_BUNDLE) for task_id in task_ids}
    else:
        mapping = {task_id: list(TASK_MAPPING.get(task_id, ())) for task_id in task_ids}
    used = sorted({name for names in mapping.values() for name in names})
    return {
        "task_mapping": mapping,
        "skills": {name: asdict(REGISTRY[name]) for name in used if name in REGISTRY},
    }
