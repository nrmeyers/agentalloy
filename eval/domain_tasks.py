"""Domain-specific benchmark tasks targeting house conventions from bundled skill packs.

Each task is answerable generically but the grader checks for specific
conventions stated in each skill's raw_prose — details a model wouldn't
reliably produce from training alone.  Run with::

    uv run python -m eval.run_poc --task-set domain
"""

from __future__ import annotations

from eval.tasks import Task

DOMAIN_TASKS: list[Task] = [
    # ------------------------------------------------------------------ #
    # 1. Webhook signature verification — svix header names + HMAC-SHA256 #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_1_webhook_signature",
        spec=(
            "You are implementing the receiver side of a webhook endpoint. "
            "Describe how to verify that an incoming webhook was genuinely sent "
            "by the platform and not forged or replayed. Include the algorithm, "
            "the relevant HTTP headers to read, and the timestamp check you "
            "would perform."
        ),
        phase="build",
        gold_skills=("webhooks-signature-verification",),
    ),
    # ------------------------------------------------------------------ #
    # 2. Webhook idempotency — webhook-id header + 24 h redis TTL          #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_2_webhook_deduplication",
        spec=(
            "A webhook consumer sometimes receives the same event twice due to "
            "retries from the platform. Describe how you would implement exactly-once "
            "processing. Which header uniquely identifies a delivery attempt vs a "
            "message, and what storage pattern would you use to track seen events?"
        ),
        phase="build",
        gold_skills=("webhooks-idempotency",),
    ),
    # ------------------------------------------------------------------ #
    # 3. Webhook replay / DLQ — retry schedule + DLQ fields required       #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_3_webhook_dlq",
        spec=(
            "Design a webhook delivery system that handles endpoint failures "
            "gracefully. Describe the retry schedule you would use, when a "
            "delivery should be considered permanently failed and moved to a "
            "dead-letter queue, and what data each DLQ entry must contain "
            "to support triage and later replay."
        ),
        phase="design",
        gold_skills=("webhooks-replay-and-dlq",),
    ),
    # ------------------------------------------------------------------ #
    # 4. Webhook versioning — X-API-Version header + 12-month sunset       #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_4_webhook_versioning",
        spec=(
            "You need to make a breaking change to your webhook payload schema: "
            "you want to rename the field `customer_id` to `account_id`. "
            "Walk through the versioning strategy you would follow so that "
            "existing consumers are not broken. Include how you communicate "
            "the change and how long you maintain backward compatibility."
        ),
        phase="design",
        gold_skills=("webhooks-versioning-and-evolution",),
    ),
    # ------------------------------------------------------------------ #
    # 5. Temporal workflow determinism — no datetime.now / random in wf    #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_5_temporal_workflow_determinism",
        spec=(
            "A colleague wrote a Temporal workflow in Python that calls "
            "`datetime.now()` inside the workflow function to timestamp an "
            "event, and uses `random.uuid4()` to generate a correlation ID. "
            "Explain why this is problematic and how to fix it."
        ),
        phase="qa",
        gold_skills=("temporal-workflow-basics",),
    ),
    # ------------------------------------------------------------------ #
    # 6. GitHub Actions OIDC — permissions block + no long-lived secrets   #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_6_github_actions_oidc",
        spec=(
            "Your team wants to deploy to Google Cloud from GitHub Actions "
            "without storing a long-lived service account key in GitHub Secrets. "
            "Describe the approach, including what changes are needed in the "
            "workflow YAML and why it is more secure than using a stored credential."
        ),
        phase="build",
        gold_skills=("github-actions-oidc-providers",),
    ),
    # ------------------------------------------------------------------ #
    # 7. dbt incremental models — is_incremental() + unique_key required   #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_7_dbt_incremental",
        spec=(
            "You have a dbt model over a 500 M-row events table. Full refreshes "
            "take 45 minutes. Explain how to convert this model to run "
            "incrementally. What macro gates the incremental filter, what "
            "configuration parameter prevents duplicate rows when records are "
            "updated, and what happens on the very first run?"
        ),
        phase="build",
        gold_skills=("data-engineering-dbt-incremental",),
    ),
    # ------------------------------------------------------------------ #
    # 8. Slowly Changing Dimensions Type 2 — surrogate key + is_current    #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_8_scd_type2",
        spec=(
            "A customer's `plan` attribute changes over time and you need to "
            "preserve the full history so that fact-table queries can reconstruct "
            "what plan a customer was on at any historical date. Describe the "
            "dimension table design pattern you would use. Include the required "
            "columns and explain how the foreign key in the fact table works."
        ),
        phase="design",
        gold_skills=("data-engineering-slowly-changing-dimensions",),
    ),
]


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------


def grade_domain_1_webhook_signature(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # webhooks-signature-verification: HMAC with SHA-256 is the algorithm
        "mentions_hmac_sha256": ("hmac" in lower and "sha" in lower)
        or "hmac-sha256" in lower
        or "hmac_sha256" in lower,
        # webhooks-signature-verification: signed content = svix-id.svix-timestamp.body
        "mentions_signed_content_tuple": (
            ("timestamp" in lower and "body" in lower)
            and any(w in lower for w in ["tuple", "concatenat", "signed content", "sign the"])
        ),
        # webhooks-signature-verification: svix-signature / svix-timestamp / svix-id headers
        "mentions_svix_headers": any(
            h in lower for h in ["svix-signature", "svix-timestamp", "svix-id", "svix_signature"]
        ),
        # webhooks-signature-verification: ±5-minute tolerance window for timestamp check
        "mentions_tolerance_window": any(
            w in lower for w in ["5 minute", "5-minute", "five minute", "tolerance", "±5", "+/-5"]
        ),
        # webhooks-signature-verification: constant-time comparison to prevent timing attacks
        "mentions_constant_time_comparison": any(
            w in lower
            for w in ["constant-time", "constant time", "timing attack", "hmac.compare_digest"]
        ),
    }


def grade_domain_2_webhook_deduplication(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # webhooks-idempotency: the webhook-id header is reused across retries of the same msg
        "identifies_webhook_id_header": any(
            h in lower for h in ["webhook-id", "webhook_id", "`webhook-id`"]
        ),
        # webhooks-idempotency: store the ID in redis with a 24 hr expiry
        "mentions_redis_for_deduplication": "redis" in lower,
        # webhooks-idempotency: 24-hour expiry / TTL on the dedup store
        "mentions_24h_ttl": any(
            w in lower for w in ["24h", "24 h", "24-hour", "24 hour", "one day", "1 day"]
        ),
        # webhooks-idempotency: at-least-once delivery from the platform side
        "acknowledges_at_least_once_delivery": any(
            w in lower for w in ["at-least-once", "at least once", "exactly once", "duplicate"]
        ),
    }


def grade_domain_3_webhook_dlq(output: str) -> dict[str, bool]:
    lower = output.lower()

    # Check for an exponential retry schedule with reasonable escalation
    has_retry_schedule = any(
        w in lower for w in ["immediate", "5s", "5 s", "30s", "2 min", "1 hr", "24 hr", "24h"]
    ) or (
        any(w in lower for w in ["exponential", "backoff"])
        and any(w in lower for w in ["schedule", "attempt", "retry"])
    )

    return {
        # webhooks-replay-and-dlq: exponential retry schedule (immediate → 5s → 30s → … → 24h)
        "describes_retry_schedule_with_escalation": has_retry_schedule,
        # webhooks-replay-and-dlq: after N attempts the delivery is given up / DLQ
        "mentions_max_attempts_or_give_up": any(
            w in lower
            for w in [
                "5 attempt",
                "five attempt",
                "max attempt",
                "final attempt",
                "give up",
                "dead letter",
                "dlq",
                "permanently fail",
            ]
        ),
        # webhooks-replay-and-dlq: DLQ entry must contain original payload + error trace
        "dlq_includes_original_payload": any(
            w in lower for w in ["original payload", "original event", "payload", "message body"]
        ),
        # webhooks-replay-and-dlq: DLQ entry must contain attempt count + error info
        "dlq_includes_attempt_count_and_error": (
            any(w in lower for w in ["attempt count", "attempt", "retry count"])
            and any(w in lower for w in ["error", "trace", "stack", "reason", "status"])
        ),
        # webhooks-replay-and-dlq: replay capability after code fix
        "mentions_replay_after_fix": any(
            w in lower for w in ["replay", "re-deliver", "redeliver", "reprocess"]
        ),
    }


def grade_domain_4_webhook_versioning(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # webhooks-versioning-and-evolution: X-API-Version header on every payload
        "mentions_version_header": any(
            w in lower
            for w in ["x-api-version", "version header", "x_api_version", "webhook-version"]
        ),
        # webhooks-versioning-and-evolution: additive/rename distinction — rename requires new ver
        "explains_rename_requires_new_version": any(
            w in lower
            for w in [
                "rename",
                "breaking change",
                "new version",
                "bump version",
                "version bump",
            ]
        ),
        # webhooks-versioning-and-evolution: per-endpoint version pinning
        "mentions_per_endpoint_pinning": any(
            w in lower for w in ["pin", "per endpoint", "endpoint pin", "pinned version"]
        ),
        # webhooks-versioning-and-evolution: 12-month minimum deprecation/sunset window
        "mentions_12_month_sunset": any(
            w in lower
            for w in [
                "12 month",
                "12-month",
                "one year",
                "1 year",
                "twelve month",
                "sunset",
            ]
        ),
        # webhooks-versioning-and-evolution: send deprecation headers on old-version payloads
        "mentions_deprecation_header_or_warning": any(
            w in lower for w in ["x-deprecated", "deprecation header", "x-sunset", "warn"]
        ),
    }


def grade_domain_5_temporal_workflow_determinism(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # temporal-workflow-basics: workflows must be deterministic — no side effects
        "explains_determinism_requirement": any(
            w in lower
            for w in ["determinism", "deterministic", "replay", "replaying", "non-deterministic"]
        ),
        # temporal-workflow-basics: datetime.now() breaks replay because clock differs on replay
        "identifies_datetime_now_problem": any(
            w in lower
            for w in [
                "datetime.now",
                "datetime.utcnow",
                "time.time()",
                "current time",
                "system clock",
            ]
        ),
        # temporal-workflow-basics: workflow.now() or workflow.unsafe is the correct fix
        "suggests_workflow_now_fix": any(
            w in lower
            for w in [
                "workflow.now",
                "workflow.unsafe",
                "temporal time",
                "workflow time",
                "workflow clock",
            ]
        ),
        # temporal-workflow-basics: random/uuid must come from activity or workflow.uuid4()
        "addresses_random_uuid_problem": any(
            w in lower
            for w in [
                "random",
                "uuid",
                "workflow.uuid4",
                "activity",
                "side effect",
            ]
        ),
    }


def grade_domain_6_github_actions_oidc(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # github-actions-oidc-providers: requires `id-token: write` permission
        "mentions_id_token_write_permission": any(
            w in lower
            for w in [
                "id-token: write",
                "id-token:write",
                "id_token: write",
                "id-token",
                "id_token",
            ]
        ),
        # github-actions-oidc-providers: uses google-github-actions/auth action
        "mentions_google_github_actions_auth": any(
            w in lower
            for w in [
                "google-github-actions/auth",
                "google-github-actions",
                "workload identity",
                "workload_identity",
            ]
        ),
        # github-actions-oidc-providers: eliminates long-lived service account key
        "explains_no_long_lived_secret": any(
            w in lower
            for w in [
                "no long-lived",
                "no stored",
                "without storing",
                "long-lived",
                "service account key",
                "static credential",
            ]
        ),
        # github-actions-oidc-providers: short-lived token exchanged at runtime
        "mentions_short_lived_token": any(
            w in lower
            for w in [
                "short-lived",
                "short lived",
                "oidc token",
                "jwt",
                "access token",
                "federat",
            ]
        ),
    }


def grade_domain_7_dbt_incremental(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # data-engineering-dbt-incremental: is_incremental() macro gates the WHERE filter
        "names_is_incremental_macro": "is_incremental" in lower or "is_incremental()" in lower,
        # data-engineering-dbt-incremental: materialized='incremental' config block
        "mentions_incremental_materialization": any(
            w in lower
            for w in [
                "materialized='incremental'",
                'materialized="incremental"',
                "materialized: incremental",
                "materialization",
                "incremental model",
            ]
        ),
        # data-engineering-dbt-incremental: unique_key prevents duplicate rows on updates
        "mentions_unique_key": "unique_key" in lower or "unique key" in lower,
        # data-engineering-dbt-incremental: first run loads all rows (no is_incremental filter)
        "explains_first_run_full_load": any(
            w in lower
            for w in [
                "first run",
                "initial run",
                "full refresh",
                "full load",
                "first time",
                "empty table",
            ]
        ),
    }


def grade_domain_8_scd_type2(output: str) -> dict[str, bool]:
    lower = output.lower()

    # SCD Type 2 must have surrogate key (not just natural key)
    has_surrogate = any(
        w in lower for w in ["surrogate", "surrogate key", "customer_key", "dimension key"]
    )

    # is_current / current_flag boolean column
    has_current_flag = any(
        w in lower
        for w in ["is_current", "current_flag", "is current", "current row", "active flag"]
    )

    # valid_from / valid_to or effective/expiry date columns
    has_date_range = any(
        w in lower
        for w in [
            "valid_from",
            "valid_to",
            "effective_date",
            "expiry_date",
            "start_date",
            "end_date",
            "valid from",
            "valid to",
        ]
    )

    return {
        # data-engineering-slowly-changing-dimensions: Type 2 = versioned rows pattern name
        "identifies_scd_type_2": any(
            w in lower for w in ["type 2", "type-2", "scd 2", "scd2", "versioned row"]
        ),
        # data-engineering-slowly-changing-dimensions: surrogate key (new per version)
        "includes_surrogate_key": has_surrogate,
        # data-engineering-slowly-changing-dimensions: is_current / current_flag boolean
        "includes_current_flag": has_current_flag,
        # data-engineering-slowly-changing-dimensions: valid_from / valid_to date range
        "includes_date_range_columns": has_date_range,
        # data-engineering-slowly-changing-dimensions: fact FK -> whichever key was current at event time
        "explains_fact_fk_historical_join": any(
            w in lower
            for w in [
                "fact table",
                "foreign key",
                "fk",
                "historical",
                "at the time",
                "at event time",
                "join",
            ]
        ),
    }


DOMAIN_GRADERS: dict[str, object] = {
    "domain_1_webhook_signature": grade_domain_1_webhook_signature,
    "domain_2_webhook_deduplication": grade_domain_2_webhook_deduplication,
    "domain_3_webhook_dlq": grade_domain_3_webhook_dlq,
    "domain_4_webhook_versioning": grade_domain_4_webhook_versioning,
    "domain_5_temporal_workflow_determinism": grade_domain_5_temporal_workflow_determinism,
    "domain_6_github_actions_oidc": grade_domain_6_github_actions_oidc,
    "domain_7_dbt_incremental": grade_domain_7_dbt_incremental,
    "domain_8_scd_type2": grade_domain_8_scd_type2,
}
