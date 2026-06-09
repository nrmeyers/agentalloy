"""POC task definitions + per-task graders.

Each task exposes a graded set of binary criteria; the harness aggregates
pass-rates per condition (see BENCHMARKS.md). ``gold_skills`` reference
``skill_id``s in the current pack corpus (``src/agentalloy/_packs/``).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    task_id: str
    spec: str
    phase: str
    gold_skills: tuple[str, ...]


TASKS: list[Task] = [
    Task(
        task_id="task_1_tdd_failing_test",
        spec=(
            "You're about to implement `calculate_tax(amount: Decimal, rate: Decimal) "
            "-> Decimal` which multiplies amount by rate and rounds half-up to 2 "
            "decimals. The function doesn't exist yet. Write only the "
            "`test_calculate_tax.py` file. Include at least one edge-case test."
        ),
        phase="build",
        gold_skills=("test-driven-development",),
    ),
    Task(
        task_id="task_2_bugfix_commit",
        spec=(
            "You just fixed a bug where `parse_date(s)` returned `None` for empty "
            "strings instead of raising `ValueError`. The fix adds an explicit "
            "empty-string check. Write the commit title and body you'd use. "
            "Assume the repo follows conventional commits."
        ),
        phase="build",
        gold_skills=("git-workflow", "debugging-systematic"),
    ),
    Task(
        task_id="task_3_code_review_checklist",
        spec=(
            "A teammate's PR adds `POST /admin/users/bulk-delete` that takes a JSON "
            "array of user IDs and deletes them. Generate the top 5 questions "
            "you'd ask in review."
        ),
        phase="qa",
        gold_skills=("code-review-practices",),
    ),
    Task(
        task_id="task_4_flaky_ci_debug",
        spec=(
            "`test_rate_limiter_resets_after_window` passes locally but fails in CI "
            "about 1 in 10 times. Propose a systematic debugging approach and a "
            "fix strategy. Budget: ~300 words."
        ),
        phase="qa",
        gold_skills=("debugging-systematic", "testing-flaky-tests"),
    ),
    Task(
        task_id="task_5_browser_test_plan",
        spec=(
            "You added a new chart to the analytics dashboard that shows last-30-day "
            "active users. Write the browser-testing plan: what to verify, what "
            "tools, what to capture."
        ),
        phase="qa",
        gold_skills=("testing-browser-and-e2e",),
    ),
    # ----- Phase 2 tasks (added 2026-04-25 to test variant findings on a fresh set) -----
    Task(
        task_id="task_6_phone_regex",
        spec=(
            "Write a Python regex that validates US phone numbers in three formats: "
            "(XXX) XXX-XXXX, XXX-XXX-XXXX, and +1XXXXXXXXXX. Return the regex "
            "pattern and a short example showing it correctly matches each format."
        ),
        phase="build",
        # Corpus has no regex-specific skill; closest lexical neighbor. Treat
        # this task as a soft control for retrieval scoring.
        gold_skills=("python-pattern-matching",),
    ),
    Task(
        task_id="task_7_friday_deploy_risks",
        spec=(
            "List the top 3 risks of deploying a database migration on Friday "
            "afternoon. Number them 1-3."
        ),
        phase="ops",
        gold_skills=("incident-response",),
    ),
    Task(
        task_id="task_8_postmortem",
        spec=(
            "Write an incident postmortem for a 30-minute outage where the auth "
            "service exhausted its database connection pool after a deploy. "
            "Include: Timeline, Root Cause, Contributing Factors, Action Items."
        ),
        phase="qa",
        gold_skills=("postmortem", "incident-response"),
    ),
    Task(
        task_id="task_9_retry_strategy",
        spec=(
            "Design an idempotent retry strategy for a payment API. Cover: "
            "retry budget, backoff scheme, idempotency-key handling, when to give up."
        ),
        phase="design",
        gold_skills=("webhooks-idempotency", "rest-common-patterns"),
    ),
    Task(
        task_id="task_10_db_perf_runbook",
        spec=(
            "Write a runbook for handling production database performance "
            "regressions. Include: Triage Steps, Common Root Causes, Fix Strategies, "
            "Rollback Criteria, Communication Checklist."
        ),
        phase="qa",
        gold_skills=(
            "debugging-systematic",
            "finding-the-bottleneck",
            "incident-response",
        ),
    ),
]


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------


def _is_valid_python(text: str) -> bool:
    candidates = [text]
    blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)
    candidates.extend(blocks)
    for c in candidates:
        try:
            ast.parse(c)
            return True
        except SyntaxError:
            continue
    return False


def grade_task_1(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        "parses_as_python": _is_valid_python(output),
        "has_test_function": bool(re.search(r"def test_\w+", output)),
        "imports_calculate_tax": "calculate_tax" in output
        and re.search(r"\bimport\b|\bfrom\b", output) is not None,
        "uses_pytest_style": ("assert " in output) and ("self.assertEqual" not in output),
        "has_edge_case": any(
            w in lower for w in ["zero", "negative", "rounding", "boundary", "0.005"]
        ),
    }


def grade_task_2(output: str) -> dict[str, bool]:
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    subject = lines[0] if lines else ""
    # Strip leading markdown fences/quotes if present.
    if subject.startswith("```"):
        subject = lines[1] if len(lines) > 1 else ""
    body = output.lower()
    return {
        "subject_is_conventional_fix": bool(re.match(r"\s*fix(\([^)]+\))?:", subject)),
        "subject_under_70_chars": 0 < len(subject.strip()) <= 70,
        "body_describes_root_cause": any(
            w in body for w in ["empty", "none", "raise", "valueerror"]
        ),
        "body_mentions_test_evidence": any(w in body for w in ["test", "regression", "reproduce"]),
    }


def grade_task_3(output: str) -> dict[str, bool]:
    items = re.findall(r"^\s*(?:\d+[.)]|[-*])\s+.+", output, re.MULTILINE)
    lower = output.lower()
    return {
        "exactly_five_items": len(items) == 5,
        "addresses_authorization": any(
            w in lower for w in ["auth", "permission", "role", "admin only", "rbac"]
        ),
        "addresses_safety": any(
            w in lower
            for w in ["confirm", "audit", "undo", "soft delete", "dry run", "soft-delete"]
        ),
        "addresses_input_validation": any(
            w in lower for w in ["limit", "validate", "validation", "bounds", "size", "max"]
        ),
    }


def grade_task_4(output: str) -> dict[str, bool]:
    lower = output.lower()
    word_count = len(output.split())
    primary = lower.split("\n", 1)[0] if "\n" in lower else lower
    return {
        "mentions_isolation_technique": any(
            w in lower for w in ["isolate", "reproduce", "minimal", "single test", "in isolation"]
        ),
        "hypothesizes_root_cause": any(
            w in lower for w in ["race", "timing", "clock", "sleep", "shared state", "ordering"]
        ),
        "not_just_retry": not (
            "retry" in primary and not any(w in lower for w in ["isolate", "reproduce", "race"])
        ),
        "under_400_words": word_count <= 400,
    }


def grade_task_5(output: str) -> dict[str, bool]:
    lower = output.lower()
    dimensions = {
        "accessibility": ["accessibility", "a11y", "screen reader", "keyboard"],
        "performance": ["performance", "load time", "render"],
        "responsive": ["responsive", "viewport", "mobile"],
        "loading": ["loading", "spinner", "skeleton"],
        "empty": ["empty state", "no data", "zero state"],
        "error": ["error state", "fallback", "failure"],
        "network": ["network throttling", "slow 3g", "offline"],
    }
    matched = sum(any(w in lower for w in words) for words in dimensions.values())
    return {
        "two_testing_dimensions": matched >= 2,
        "names_a_concrete_tool": any(
            w in lower for w in ["devtools", "lighthouse", "axe", "playwright", "responsive mode"]
        ),
        "specifies_capture_strategy": any(
            w in lower for w in ["screenshot", "video", "recording", "trace", "har", "log"]
        ),
    }


def grade_task_6(output: str) -> dict[str, bool]:
    import re as _re

    # Extract a regex pattern from triple-backtick blocks or bare ``...`` fragments.
    candidates: list[str] = []
    candidates.extend(_re.findall(r"```(?:python|py|regex)?\n(.*?)```", output, _re.DOTALL))
    candidates.extend(_re.findall(r"`([^`\n]{6,})`", output))
    pattern_compiles = False
    matches_all_three = False
    for cand in candidates:
        # Heuristic: extract a literal r-string or quoted regex from the candidate.
        m = _re.search(r"""r['"]([^'"]+)['"]""", cand) or _re.search(
            r"""['"]([\^\\][^'"]+)['"]""", cand
        )
        pat = m.group(1) if m else cand.strip()
        try:
            compiled = _re.compile(pat)
            pattern_compiles = True
            samples = ["(555) 123-4567", "555-123-4567", "+15551234567"]
            if all(compiled.search(s) for s in samples):
                matches_all_three = True
                break
        except _re.error:
            continue
    return {
        "regex_pattern_compiles": pattern_compiles,
        "matches_all_three_formats": matches_all_three,
        "discusses_three_formats": all(
            shape in output for shape in ["(XXX)", "XXX-XXX-XXXX", "+1"]
        ),
        "non_trivial_pattern": any(len(c.strip()) > 20 for c in candidates),
    }


def grade_task_7(output: str) -> dict[str, bool]:
    import re as _re

    items = _re.findall(r"^\s*\d+[.)]\s+.+", output, _re.MULTILINE)
    lower = output.lower()
    return {
        "exactly_three_numbered_items": len(items) == 3,
        "mentions_rollback": any(
            w in lower for w in ["rollback", "roll back", "back out", "backout", "revert"]
        ),
        "mentions_traffic_or_load": any(
            w in lower for w in ["traffic", "load", "weekend", "spike"]
        ),
        "mentions_team_availability": any(
            w in lower
            for w in ["on-call", "on call", "team", "staff", "off-hours", "weekend coverage"]
        ),
    }


def grade_task_8(output: str) -> dict[str, bool]:
    lower = output.lower()
    word_count = len(output.split())
    return {
        "has_timeline_section": any(
            s in lower for s in ["## timeline", "**timeline**", "# timeline"]
        ),
        "has_root_cause_section": any(s in lower for s in ["root cause", "## cause", "**cause**"]),
        "has_action_items_section": any(
            s in lower
            for s in ["action item", "## actions", "**actions**", "follow-up", "follow up"]
        ),
        "mentions_connection_pool": "connection pool" in lower
        or "pool exhaust" in lower
        or "pool size" in lower,
        "under_600_words": word_count <= 600,
    }


def grade_task_9(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        "covers_retry_budget": any(
            w in lower for w in ["retry budget", "max retries", "max attempts", "retry limit"]
        ),
        "covers_backoff": any(w in lower for w in ["exponential", "jitter", "backoff"]),
        "covers_idempotency_key": "idempotency" in lower
        and any(w in lower for w in ["key", "header", "token"]),
        "covers_give_up": any(
            w in lower
            for w in [
                "give up",
                "dead letter",
                "abandon",
                "stop retrying",
                "fail open",
                "fail closed",
                "circuit breaker",
            ]
        ),
    }


def grade_task_10(output: str) -> dict[str, bool]:
    lower = output.lower()
    return {
        "has_triage_section": any(s in lower for s in ["## triage", "triage step", "**triage**"]),
        "has_root_causes_section": any(
            s in lower for s in ["common root cause", "## root cause", "## causes", "**root cause"]
        ),
        "has_fix_strategies_section": any(s in lower for s in ["fix strateg", "## fix", "**fix"]),
        "has_rollback_section": any(
            s in lower
            for s in ["rollback criteria", "## rollback", "when to roll back", "rollback condition"]
        ),
        "has_communication_section": any(
            s in lower
            for s in [
                "communication checklist",
                "## communication",
                "stakeholder",
                "comms checklist",
            ]
        ),
    }


GRADERS = {
    "task_1_tdd_failing_test": grade_task_1,
    "task_2_bugfix_commit": grade_task_2,
    "task_3_code_review_checklist": grade_task_3,
    "task_4_flaky_ci_debug": grade_task_4,
    "task_5_browser_test_plan": grade_task_5,
    "task_6_phone_regex": grade_task_6,
    "task_7_friday_deploy_risks": grade_task_7,
    "task_8_postmortem": grade_task_8,
    "task_9_retry_strategy": grade_task_9,
    "task_10_db_perf_runbook": grade_task_10,
}
