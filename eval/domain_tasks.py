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
        # data-engineering-dbt-incremental was deprecated (superseded_by
        # data-engineering-dbt-models); the survivor absorbed the incremental
        # materialization / is_incremental() / unique_key coverage this task
        # grades. Point gold at the active survivor so the benchmark is winnable.
        gold_skills=("data-engineering-dbt-models",),
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
    # ------------------------------------------------------------------ #
    # 9. Temporal activity timeouts — start_to_close + heartbeat           #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_9_temporal_activity_timeouts",
        spec=(
            "You are writing a Temporal activity in Python that calls a slow "
            "third-party API which can take up to two minutes and occasionally "
            "hangs forever — and the only available client library is blocking "
            "(synchronous). Describe the timeout configuration you would set on "
            "the activity, how the workflow can tell that a long-running "
            "activity is still making progress, and how you would implement "
            "the activity given that the client library blocks."
        ),
        phase="build",
        gold_skills=("temporal-activity-basics",),
    ),
    # ------------------------------------------------------------------ #
    # 10. GH Actions concurrency — group keys + cancel-in-progress         #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_10_gha_concurrency",
        spec=(
            "Your CI workflow runs on every push to a PR branch, and your "
            "deploy workflow pushes to production. Rapid successive pushes "
            "waste compute on stale CI runs, and two deploys must never run "
            "at the same time. Show how you would configure this in GitHub "
            "Actions workflow YAML, and explain why the two workflows need "
            "different settings."
        ),
        phase="build",
        gold_skills=("github-actions-concurrency",),
    ),
    # ------------------------------------------------------------------ #
    # 11. GH Actions caching — hashFiles key + restore-keys                #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_11_gha_caching",
        spec=(
            "A Node.js CI workflow reinstalls all dependencies from scratch on "
            "every run. Explain how to add dependency caching with GitHub "
            "Actions: how the cache key should be constructed so the cache is "
            "invalidated when the lockfile changes, what fallback you would "
            "configure to still get partial reuse, and what happens on a cache "
            "miss."
        ),
        phase="build",
        gold_skills=("github-actions-caching-and-artifacts",),
    ),
    # ------------------------------------------------------------------ #
    # 12. Redis streams — consumer groups + PEL + reclaim                  #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_12_redis_streams_workers",
        spec=(
            "You need a durable work queue on Redis where multiple workers "
            "consume events in parallel, no event is lost if a worker crashes "
            "mid-processing, and stuck events eventually get reassigned to a "
            "healthy worker. Which Redis data structure and commands would you "
            "use? Walk through the lifecycle: producing an event, a worker "
            "claiming and finishing it, and recovering from a crashed worker."
        ),
        phase="build",
        gold_skills=("redis-streams",),
    ),
    # ------------------------------------------------------------------ #
    # 13. Redis WATCH — optimistic locking around MULTI/EXEC               #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_13_redis_optimistic_lock",
        spec=(
            "Implement a safe read-modify-write of an account balance stored "
            "in Redis, without using Lua scripts. Two clients may race on the "
            "same key. Describe the transaction mechanism you would use, how a "
            "racing write is detected, and what the losing client should do."
        ),
        phase="build",
        gold_skills=("redis-transactions-multi-exec",),
    ),
    # ------------------------------------------------------------------ #
    # 14. Snowflake time travel — AT|BEFORE, UNDROP, retention, streams    #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_14_snowflake_time_travel",
        spec=(
            "An analyst ran a bad UPDATE that corrupted a Snowflake table an "
            "hour ago, and a staging table was accidentally dropped yesterday. "
            "Explain how to query the table's state from before the bad "
            "UPDATE, how to recover the dropped table, and what setting bounds "
            "how far back these operations can reach. Separately: how would "
            "you set up incremental downstream processing that consumes only "
            "the rows that changed in this table?"
        ),
        phase="build",
        gold_skills=("snowflake-time-travel-and-streams",),
    ),
    # ------------------------------------------------------------------ #
    # 15. Snowflake warehouses — auto-suspend, multi-cluster, billing      #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_15_snowflake_warehouse_cost",
        spec=(
            "Your Snowflake bill is dominated by a warehouse that sits idle "
            "between hourly batch loads, and on Monday mornings BI dashboard "
            "queries queue up behind each other on the same warehouse. "
            "Describe the warehouse configuration changes you would make to "
            "cut the idle cost and absorb the concurrency spike, and explain "
            "how compute billing works while a warehouse is running versus "
            "idle."
        ),
        phase="design",
        gold_skills=("snowflake-warehouses-and-cost",),
    ),
    # ------------------------------------------------------------------ #
    # 16. OTel tracing — trace/parent IDs, propagation, sampling           #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_16_otel_trace_propagation",
        spec=(
            "Service A calls service B over HTTP and you want a single "
            "distributed trace covering both services. Explain how spans "
            "created in service B end up in the same trace as service A's "
            "spans: what identifies the trace, how a span declares its "
            "parent, how that context crosses the HTTP boundary, and how you "
            "can tell which span is the root of the trace. Finally, how do "
            "you keep tracing costs bounded in high-traffic systems?"
        ),
        phase="build",
        gold_skills=("analytics-otel-traces",),
    ),
    # ------------------------------------------------------------------ #
    # 17. Airflow best practices — lean XComs, Connections, idempotency    #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_17_airflow_task_hygiene",
        spec=(
            "An Airflow DAG has three problems: one task produces a 2 GB "
            "dataframe that the next task needs; several tasks embed database "
            "passwords directly in the code; and re-running a task after a "
            "failure double-inserts rows into the target table. Describe the "
            "best-practice fix for each problem."
        ),
        phase="build",
        gold_skills=("data-engineering-airflow-best-practices",),
    ),
    # ------------------------------------------------------------------ #
    # 18. Redshift table design — DISTKEY collocation, ALL, SORTKEY        #
    # ------------------------------------------------------------------ #
    Task(
        task_id="domain_18_redshift_table_design",
        spec=(
            "You are designing a star schema in Amazon Redshift: a 2-billion-"
            "row fact table joined to a large customer dimension, several "
            "small lookup dimensions, and queries that almost always filter "
            "on a date range. Explain your distribution strategy for the fact "
            "table, the large dimension, and the small lookups, your sort "
            "strategy, and the storage mechanism that makes range filters on "
            "the sort column fast."
        ),
        phase="design",
        gold_skills=("redshift-table-design",),
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
        # (synonym audit 2026-06: external clerk skill writes `signed_content`;
        # also accept dot-join phrasing)
        "mentions_signed_content_tuple": (
            ("timestamp" in lower and "body" in lower)
            and any(
                w in lower
                for w in [
                    "tuple",
                    "concatenat",
                    "signed content",
                    "signed_content",
                    "sign the",
                    "joined",
                ]
            )
        ),
        # webhooks-signature-verification: svix-signature / svix-timestamp / svix-id headers
        "mentions_svix_headers": any(
            h in lower for h in ["svix-signature", "svix-timestamp", "svix-id", "svix_signature"]
        ),
        # webhooks-signature-verification: ±5-minute tolerance window for timestamp check
        # (synonym audit 2026-06: accept any bounded-freshness phrasing, not just
        # our pack's "5 minute tolerance" wording)
        "mentions_tolerance_window": any(
            w in lower
            for w in [
                "5 minute",
                "5-minute",
                "five minute",
                "tolerance",
                "±5",
                "+/-5",
                "300 second",
                "clock skew",
                "too old",
                "replay window",
                "freshness",
            ]
        ),
        # webhooks-signature-verification: constant-time comparison to prevent timing attacks
        # (synonym audit 2026-06: accept timing-safe / secure-compare phrasings)
        "mentions_constant_time_comparison": any(
            w in lower
            for w in [
                "constant-time",
                "constant time",
                "constant_time",
                "timing attack",
                "timing-safe",
                "timing safe",
                "timingsafeequal",
                "secure compar",
                "hmac.compare_digest",
            ]
        ),
    }


def grade_domain_2_webhook_deduplication(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # webhooks-idempotency: the webhook-id header is reused across retries of the same msg
        "identifies_webhook_id_header": any(
            h in lower for h in ["webhook-id", "webhook_id", "`webhook-id`"]
        ),
        # webhooks-idempotency: store the ID in redis with a 24 hr expiry.
        # (synonym audit 2026-06: redis is OUR pack's convention — any concrete
        # dedup store is a correct answer; requiring "redis" rigs the arm
        # comparison toward composed/flat)
        "mentions_dedup_store": any(
            w in lower
            for w in [
                "redis",
                "memcached",
                "key-value",
                "key/value",
                "dynamodb",
                "cache",
                "database",
                "idempotency store",
                "processed events table",
                "unique constraint",
            ]
        ),
        # webhooks-idempotency: bounded retention (our pack: 24 h TTL).
        # (synonym audit 2026-06: accept any TTL/expiry phrasing, not just 24h)
        "mentions_bounded_ttl": any(
            w in lower
            for w in [
                "24h",
                "24 h",
                "24-hour",
                "24 hour",
                "one day",
                "1 day",
                "ttl",
                "expir",
                "retention",
                "time-to-live",
                "time to live",
            ]
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
        # temporal-workflow-basics: workflow.now() or workflow.unsafe is the correct fix.
        # (synonym audit 2026-06: Temporal's own docs/skill teach "move side
        # effects into an activity" as the fix — equally correct, accept it)
        "suggests_workflow_now_fix": any(
            w in lower
            for w in [
                "workflow.now",
                "workflow.unsafe",
                "temporal time",
                "workflow time",
                "workflow clock",
                "side effect",
                "sideeffect",
                "an activity",
                "into activities",
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


def grade_domain_9_temporal_activity_timeouts(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # temporal-activity-basics: start_to_close_timeout is the per-attempt timeout
        "names_start_to_close_timeout": any(
            w in lower for w in ["start_to_close", "start-to-close", "start to close"]
        ),
        # temporal-activity-basics: heartbeat is how long activities prove liveness
        "mentions_heartbeat": "heartbeat" in lower,
        # temporal-activity-basics: schedule_to_close bounds total time incl. retries
        "mentions_schedule_to_close_or_total_bound": any(
            w in lower
            for w in [
                "schedule_to_close",
                "schedule-to-close",
                "schedule to close",
                "total time",
                "including retries",
                "across retries",
            ]
        ),
        # temporal-activity-basics: blocking calls must not run in an async activity —
        # use a synchronous activity (ThreadPoolExecutor) or an async-safe library,
        # never block the asyncio event loop
        "avoids_blocking_event_loop": any(
            w in lower
            for w in [
                "threadpoolexecutor",
                "thread pool",
                "synchronous activity",
                "sync activity",
                "event loop",
                "asyncio loop",
                "run_in_executor",
                "to_thread",
            ]
        ),
    }


def grade_domain_10_gha_concurrency(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # github-actions-concurrency: the concurrency keyword / group
        "uses_concurrency_group": "concurrency" in lower and "group" in lower,
        # github-actions-concurrency: group key built from workflow + ref/PR context
        "group_key_uses_ref_or_pr": any(
            w in lower
            for w in [
                "github.ref",
                "github.workflow",
                "pull_request.number",
                "per branch",
                "per-branch",
                "per pr",
            ]
        ),
        # github-actions-concurrency: CI cancels stale runs
        "ci_cancels_in_progress": any(
            w in lower for w in ["cancel-in-progress: true", "cancel-in-progress:true"]
        )
        or ("cancel-in-progress" in lower and "true" in lower),
        # github-actions-concurrency: deploys serialize, never cancel mid-deploy
        "deploy_never_cancels": ("cancel-in-progress" in lower and "false" in lower)
        or any(
            w in lower
            for w in ["never cancel", "don't cancel", "do not cancel", "queue", "serializ"]
        ),
    }


def grade_domain_11_gha_caching(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # github-actions-caching-and-artifacts: actions/cache is the primitive
        "uses_actions_cache": "actions/cache" in lower or "cache action" in lower,
        # github-actions-caching-and-artifacts: key includes a lockfile hash
        "key_hashes_lockfile": ("hashfiles" in lower)
        or (
            any(w in lower for w in ["lock file", "lockfile", "package-lock", "pnpm-lock"])
            and "hash" in lower
        ),
        # github-actions-caching-and-artifacts: restore-keys prefix fallback
        "configures_restore_keys": any(
            w in lower for w in ["restore-keys", "restore keys", "fallback key", "prefix match"]
        ),
        # github-actions-caching-and-artifacts: miss -> new cache saved on job success
        "explains_cache_miss_save": "miss" in lower
        and any(w in lower for w in ["saved", "save", "created", "uploaded", "new cache"]),
    }


def grade_domain_12_redis_streams_workers(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # redis-streams: streams + XADD are the right structure for durable queues
        "uses_streams_xadd": "stream" in lower and "xadd" in lower,
        # redis-streams: consumer groups partition delivery across workers
        "uses_consumer_groups": any(w in lower for w in ["consumer group", "xgroup", "xreadgroup"]),
        # redis-streams: XACK confirms processing
        "acks_with_xack": "xack" in lower or "acknowledg" in lower,
        # redis-streams: pending-entries list holds unacked deliveries
        "mentions_pending_entries": any(
            w in lower for w in ["pending entries", "pending-entries", "pel", "xpending"]
        ),
        # redis-streams: XCLAIM / XAUTOCLAIM reassigns stuck entries
        "reclaims_stuck_entries": any(
            w in lower for w in ["xclaim", "xautoclaim", "reassign", "re-assign", "claim"]
        ),
    }


def grade_domain_13_redis_optimistic_lock(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # redis-transactions-multi-exec: MULTI/EXEC atomic block
        "uses_multi_exec": "multi" in lower and "exec" in lower,
        # redis-transactions-multi-exec: WATCH detects the race
        "uses_watch": "watch" in lower,
        # redis-transactions-multi-exec: this is optimistic concurrency control
        "names_optimistic_locking": any(
            w in lower for w in ["optimistic lock", "optimistic concurrency", "optimistic"]
        ),
        # redis-transactions-multi-exec: on conflict EXEC returns nil -> retry loop
        "retries_on_conflict": any(w in lower for w in ["retry", "retries", "try again", "loop"])
        and any(w in lower for w in ["nil", "null", "abort", "fail", "none"]),
    }


def grade_domain_14_snowflake_time_travel(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # snowflake-time-travel-and-streams: AT | BEFORE clause with timestamp/offset
        "uses_at_before_clause": any(
            w in lower
            for w in ["at(", "before(", "at |", "at | before", "at/before", "time travel"]
        ),
        # snowflake-time-travel-and-streams: UNDROP restores dropped objects
        "uses_undrop": "undrop" in lower,
        # snowflake-time-travel-and-streams: retention period setting bounds the window
        "mentions_retention_period": any(
            w in lower
            for w in [
                "data_retention_time_in_days",
                "retention period",
                "retention",
                "90 day",
                "1 day",
            ]
        ),
        # snowflake-time-travel-and-streams: streams are the CDC primitive
        "uses_streams_for_cdc": "stream" in lower
        and any(w in lower for w in ["cdc", "change", "insert", "delta"]),
    }


def grade_domain_15_snowflake_warehouse_cost(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # snowflake-warehouses-and-cost: auto-suspend kills idle burn
        "configures_auto_suspend": any(
            w in lower
            for w in ["auto_suspend", "auto-suspend", "auto suspend", "suspend automatically"]
        ),
        # snowflake-warehouses-and-cost: multi-cluster absorbs concurrency spikes
        "uses_multi_cluster_for_concurrency": any(
            w in lower for w in ["multi-cluster", "multi cluster", "multicluster"]
        ),
        # snowflake-warehouses-and-cost: billed in credits only while running
        "explains_credit_billing_while_running": "credit" in lower
        and any(
            w in lower
            for w in [
                "while running",
                "when running",
                "per second",
                "per-second",
                "only when",
                "suspended",
                "idle",
            ]
        ),
        # snowflake-warehouses-and-cost: sizing (X-Small…) is a cost lever
        "discusses_warehouse_sizing": any(
            w in lower
            for w in ["x-small", "xsmall", "warehouse size", "resize", "downsize", "right-siz"]
        ),
    }


def grade_domain_16_otel_trace_propagation(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # analytics-otel-traces: trace_id is shared by every span in the trace
        "explains_trace_id": any(w in lower for w in ["trace_id", "trace id", "traceid"]),
        # analytics-otel-traces: spans declare parent via parent span id
        "explains_parent_id": any(
            w in lower for w in ["parent_id", "parent id", "parent span", "parent-id"]
        ),
        # analytics-otel-traces: context propagation carries trace context across HTTP
        "explains_context_propagation": any(
            w in lower for w in ["propagat", "traceparent", "context across", "inject", "extract"]
        ),
        # analytics-otel-traces: root span has no parent
        "identifies_root_span": "root span" in lower
        or (
            "root" in lower
            and any(w in lower for w in ["no parent", "null parent", "without a parent"])
        ),
        # analytics-otel-traces: sampling bounds tracing cost
        "mentions_sampling": "sampl" in lower,
    }


def grade_domain_17_airflow_task_hygiene(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # airflow-best-practices: XComs are for small messages only
        "keeps_xcom_lean": "xcom" in lower
        and any(w in lower for w in ["small", "lean", "metadata", "not for large", "too large"]),
        # airflow-best-practices: large data goes to remote storage, pass the path
        "passes_storage_path_not_data": any(
            w in lower for w in ["s3", "gcs", "hdfs", "object stor", "remote stor", "blob"]
        )
        and any(w in lower for w in ["path", "uri", "reference", "pointer", "key"]),
        # airflow-best-practices: credentials live in Connections, not code
        "uses_connections_for_credentials": any(
            w in lower for w in ["connection", "secrets backend", "secret backend"]
        ),
        # airflow-best-practices: tasks must be idempotent (upsert/delete-insert/overwrite)
        "makes_tasks_idempotent": "idempoten" in lower
        or any(w in lower for w in ["upsert", "merge", "delete then insert", "overwrite"]),
    }


def grade_domain_18_redshift_table_design(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        # redshift-table-design: DISTKEY on the join column of fact + dimension
        "distkey_collocates_join": any(
            w in lower for w in ["distkey", "dist key", "distribution key", "diststyle key"]
        )
        and any(w in lower for w in ["join", "colloc", "co-loc", "same node", "same slice"]),
        # redshift-table-design: small dimensions get ALL distribution
        "all_distribution_for_small_dims": any(
            w in lower
            for w in ["diststyle all", "all distribution", "distribution style all", "dist all"]
        )
        or ("all" in lower and "every node" in lower),
        # redshift-table-design: SORTKEY on the date/timestamp filter column
        "sortkey_on_date": any(w in lower for w in ["sortkey", "sort key"])
        and any(w in lower for w in ["date", "timestamp", "time"]),
        # redshift-table-design: zone maps / min-max block skipping make range scans fast
        "explains_zone_map_pruning": any(
            w in lower
            for w in [
                "zone map",
                "zone-map",
                "skip block",
                "skips block",
                "skip entire block",
                "min and max",
                "min/max",
                "block skipping",
                "prune",
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
    "domain_9_temporal_activity_timeouts": grade_domain_9_temporal_activity_timeouts,
    "domain_10_gha_concurrency": grade_domain_10_gha_concurrency,
    "domain_11_gha_caching": grade_domain_11_gha_caching,
    "domain_12_redis_streams_workers": grade_domain_12_redis_streams_workers,
    "domain_13_redis_optimistic_lock": grade_domain_13_redis_optimistic_lock,
    "domain_14_snowflake_time_travel": grade_domain_14_snowflake_time_travel,
    "domain_15_snowflake_warehouse_cost": grade_domain_15_snowflake_warehouse_cost,
    "domain_16_otel_trace_propagation": grade_domain_16_otel_trace_propagation,
    "domain_17_airflow_task_hygiene": grade_domain_17_airflow_task_hygiene,
    "domain_18_redshift_table_design": grade_domain_18_redshift_table_design,
}
