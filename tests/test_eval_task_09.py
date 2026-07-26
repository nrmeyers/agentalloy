"""Grading tests for task 09: retry strategy.

Covers ``grade_task_9`` from ``eval/tasks.py`` with passing and failing
samples.  The task asks the agent to design an idempotent retry strategy
for a payment API, covering retry budget, backoff scheme, idempotency-key
handling, and when to give up.
"""

from __future__ import annotations

from eval.tasks import grade_task_9

# ---------------------------------------------------------------------------
# Sample outputs
# ---------------------------------------------------------------------------

_GOOD_RETRY_STRATEGY = """
## Idempotent Retry Strategy for Payment API

### Retry Budget

- **Max retries**: 5 attempts
- **Retry budget**: The system should not spend more than 30 seconds total
  on retries for a single payment request.

### Backoff Scheme

- Use **exponential backoff** with **jitter** to prevent thundering herd.
- Base delay: 1 second, doubling each retry (1s, 2s, 4s, 8s, 16s).
- Add random jitter (±50%) to spread retry load.

### Idempotency Key Handling

- Every payment request MUST include an idempotency key in the header.
- The server stores the idempotency key and returns the cached response
  for duplicate requests within 24 hours.
- If no idempotency key is provided, return 400 Bad Request.

### When to Give Up

- After 5 retries, **give up** and return a 500 error to the client.
- Log the failure and send the request to a **dead letter queue** for
  manual review.
- Consider implementing a **circuit breaker** to stop retrying when the
  payment service is down for an extended period.
""".strip()

_GOOD_RETRY_STRATEGY_ALT = """
# Retry Strategy

## Retry Budget

Use a max retries limit of 3. Do not exceed 3 attempts for any payment.

## Backoff

Exponential backoff starting at 500ms with jitter.

## Idempotency

Use an idempotency token in the request header to ensure safe retries.

## Give Up

After max retries, abandon the request and send to dead letter queue.
""".strip()

_BAD_TOO_FEW = """
Just retry the payment a few times. If it keeps failing, give up.
""".strip()

_BAD_MISSING_IDEMPOTENCY = """
## Retry Strategy

### Backoff

Use exponential backoff with jitter.

### Retry Budget

Max 5 retries.

### Give Up

After 5 retries, stop retrying.
""".strip()

_BAD_MISSING_BACKOFF = """
## Retry Strategy

### Retry Budget

Max 3 retries with a retry budget of 30 seconds.

### Idempotency Key

Every request must include an idempotency key in the header.

### Give Up

After max retries, send to dead letter queue.
""".strip()

_BAD_MISSING_GIVE_UP = """
## Retry Strategy

### Backoff

Exponential backoff with jitter.

### Retry Budget

Max 5 retries.

### Idempotency Key

Include an idempotency key in the header.
""".strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGradeTask9:
    """Verify grade_task_9 produces correct results."""

    def test_good_retry_strategy_passes_all_criteria(self) -> None:
        grades = grade_task_9(_GOOD_RETRY_STRATEGY)
        assert grades["covers_retry_budget"] is True
        assert grades["covers_backoff"] is True
        assert grades["covers_idempotency_key"] is True
        assert grades["covers_give_up"] is True
        assert all(grades.values()), f"expected all True, got {grades}"

    def test_good_retry_strategy_alt_passes_all_criteria(self) -> None:
        grades = grade_task_9(_GOOD_RETRY_STRATEGY_ALT)
        assert grades["covers_retry_budget"] is True
        assert grades["covers_backoff"] is True
        assert grades["covers_idempotency_key"] is True
        assert grades["covers_give_up"] is True

    def test_good_retry_strategy_max_attempts(self) -> None:
        """'max attempts' is accepted as retry budget."""
        grades = grade_task_9(
            "Max attempts: 5. Exponential backoff. Idempotency key required. Give up after 5."
        )
        assert grades["covers_retry_budget"] is True

    def test_good_retry_strategy_retry_limit(self) -> None:
        """'retry limit' is accepted as retry budget."""
        grades = grade_task_9(
            "Retry limit is 3. Use backoff. Include idempotency token. Fail closed after."
        )
        assert grades["covers_retry_budget"] is True

    def test_good_retry_strategy_circuit_breaker(self) -> None:
        """'circuit breaker' is accepted as give-up strategy."""
        grades = grade_task_9(
            "Retry budget: 3. Exponential backoff. Idempotency key. Circuit breaker on failure."
        )
        assert grades["covers_give_up"] is True

    def test_good_retry_strategy_fail_closed(self) -> None:
        """'fail closed' is accepted as give-up strategy."""
        grades = grade_task_9(
            "Retry budget: 3. Backoff. Idempotency key. Fail closed after timeout."
        )
        assert grades["covers_give_up"] is True

    def test_bad_too_few_fails_multiple_criteria(self) -> None:
        grades = grade_task_9(_BAD_TOO_FEW)
        assert grades["covers_retry_budget"] is False
        assert grades["covers_backoff"] is False
        assert grades["covers_idempotency_key"] is False
        assert grades["covers_give_up"] is False

    def test_bad_missing_idempotency_fails_one_criteria(self) -> None:
        grades = grade_task_9(_BAD_MISSING_IDEMPOTENCY)
        assert grades["covers_retry_budget"] is True
        assert grades["covers_backoff"] is True
        assert grades["covers_idempotency_key"] is False
        assert grades["covers_give_up"] is True

    def test_bad_missing_backoff_fails_one_criteria(self) -> None:
        grades = grade_task_9(_BAD_MISSING_BACKOFF)
        assert grades["covers_retry_budget"] is True
        assert grades["covers_backoff"] is False
        assert grades["covers_idempotency_key"] is True
        assert grades["covers_give_up"] is True

    def test_bad_missing_give_up_fails_one_criteria(self) -> None:
        grades = grade_task_9(_BAD_MISSING_GIVE_UP)
        assert grades["covers_retry_budget"] is True
        assert grades["covers_backoff"] is True
        assert grades["covers_idempotency_key"] is True
        assert grades["covers_give_up"] is False

    def test_empty_output_fails_everything(self) -> None:
        grades = grade_task_9("")
        assert all(v is False for v in grades.values()), (
            f"expected all False for empty output, got {grades}"
        )

    def test_jitter_counts_as_backoff(self) -> None:
        """'jitter' alone is not enough; needs 'exponential' or 'backoff'."""
        grades = grade_task_9("Use jitter with max retries of 3. Idempotency key. Give up after.")
        assert grades["covers_backoff"] is False

    def test_exponential_counts_as_backoff(self) -> None:
        """'exponential' alone is enough."""
        grades = grade_task_9("Exponential backoff. Max retries 3. Idempotency key. Give up.")
        assert grades["covers_backoff"] is True
