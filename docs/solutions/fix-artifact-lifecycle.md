# Fix artifact lifecycle (issue #520)

## Problem

Completed work items were never marked complete in the store:
- `sdd_artifact` had no `status` column — every artifact was permanently "current"
- The cycle-end sweep (ship→intake) never archived store contracts or artifacts

## Approach that worked

**1. ALTER TABLE with three steps** — DuckDB doesn't support `ADD COLUMN ...
NOT NULL DEFAULT` in one statement. The working pattern:
```
ALTER TABLE ADD COLUMN (nullable)
UPDATE SET default WHERE IS NULL
→ skip NOT NULL (code layer enforces it)
```
`NOT NULL ALTER COLUMN` fails in DuckDB when other tables reference the table.
Code-layer enforcement via `set_artifact()` is sufficient.

**2. Default `status='active'` filter** — Added optional `status` parameter
(default `'active'`) to `list_artifacts()`, `get_artifact()`, and `set_artifact()`.
`status='all'` opts into history. No breaking change for callers.

**3. Single `archive_all()` transaction** — One DuckDB transaction updates both
`sdd_contract` and `sdd_artifact`. Explicit BEGIN/COMMIT with ROLLBACK on
failure. Returns counts.

## What didn't work

- **One-statement ALTER TABLE:** DuckDB rejects `ADD COLUMN ... NOT NULL DEFAULT`.
- **NOT NULL enforcement:** DuckDB can't `ALTER COLUMN SET NOT NULL` on a table
  with dependencies. Code-layer default is the workaround.
- **Per-slug archiving:** More complex and inconsistent with the filesystem
  sweep that archives everything. Simple "archive all" is correct.

## Key decisions

- No versioned migration system — single ALTER TABLE in migrate() is idempotent
  via column existence check.
- No bulk-archive of existing stale rows — fix only affects future sweeps.
- No DELETE/purge — archived rows remain fetchable.
- Best-effort in phase.py sweep — store failure doesn't block filesystem move.
