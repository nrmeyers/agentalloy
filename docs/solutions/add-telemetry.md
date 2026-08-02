# Fix: soft-fail writes plus mock-only tests hid a telemetry table that recorded nothing

## Problem

`phase_events` recorded **zero rows** from commit `9eb7eca` until `74ac10b` —
two commits and one full work item — while CI stayed green the entire time.

`_INSERT_SQL` in `telemetry/phase_writer.py` carried 13 placeholders against a
14-column table and a 14-value params tuple. Every INSERT raised on arity. The
writer is soft-fail by design:

```python
except Exception:  # noqa: BLE001 — soft-fail by design
    logger.debug("phase_event write failed: %s", exc_info=True)
```

so the exception was swallowed and callers saw a clean return. The `logger.debug`
call was itself broken — a `%s` with no argument — so even the diagnostic path
produced nothing useful.

Tasks 01 and 02 were both merged and marked complete. Both were wired correctly.
Neither ever wrote a row.

The tests passed because they asserted against a **mocked** store: `execute` was
called with the expected SQL, so the assertion held. Nothing ever executed that
SQL against a real database.

## Approach That Worked

1. **Name every column in the INSERT.** `INSERT INTO t VALUES (?,?,...)` couples
   correctness to an invisible ordinal count. Naming the columns makes arity
   drift a load-time error instead of a runtime exception, and makes the diff
   reviewable — a reviewer can see 14 names, not count 14 question marks.

2. **A round-trip test against a real DuckDB.** Write through the public API,
   `SELECT` the row back, assert its contents. This is the only test shape that
   could have caught the bug; every mock-based assertion passes on a writer that
   never writes.

3. **Assert the fixture, not just the result.** The later migration test builds a
   raw 14-column table and asserts `len(cols) == 14` with the message *"fixture
   drifted from the old schema."* Without that, a schema change silently turns
   the migration test into a fresh-database test that exercises nothing.

## What Didn't Work / Didn't Try

- **Trusting the commit message.** `5d37eb1` reads "wire phase events into
  evaluate_signal + all proxy routers," which sounds like the router task was
  done. It wired the `phase_*` events; the `llm_*` events did not exist. Task
  status was ultimately determined by grepping for call sites, not by reading
  history. Commit messages describe intent; only the code describes state.

- **Removing the soft-fail.** Rejected. Telemetry genuinely must not break a
  request or a stream, and that posture is correct. The defect was never the
  `except` — it was having no test that would notice the `except` firing every
  single time.

## Key Decision

Soft-fail error handling and mock-based tests are individually reasonable and
jointly blind. A swallowed exception produces no signal; a mocked collaborator
produces no execution. Together they can hide a component that is 100%
non-functional behind a green suite indefinitely.

**Any code path that swallows exceptions must have at least one test that
exercises it against the real collaborator.** The soft-fail is what removes the
loud failure mode, so it is precisely the code that cannot be verified by
inspection or by mocks.

## Recurrence Signal

Look for the pair, not either half:

- an `except Exception` that logs at DEBUG and returns normally, **and**
- a test suite for that component whose collaborator is a `Mock`/`MagicMock`

Every soft-fail writer in this codebase is a candidate. `DuckDBTelemetryWriter`
shares the posture and deserves the same round-trip coverage.

Corollary observed in the same cycle: `ruff check` and `ruff format` are
separate CI gates, and a green `pytest` is the weakest of the four signals in
`.github/workflows/ci.yml`. The class of error is identical — a partial check
mistaken for a complete one.

Related follow-ups: #521 (`llm_*` absent from two of three proxy surfaces),
#522 (`phase_events` unqueryable per-repo), #523 (pre-commit hook not installed
per checkout, so formatting drift reaches CI).
