# Fix: SDD exit gates for qa/ship/sdd-fast should query the store, not disk

## Problem

Three SDD pack exit gates (`qa`, `ship`, `sdd-fast`) used filesystem globs (`path: docs/<phase>/*.md`) instead of store queries (`phase: <phase>, name: "*.md"`). In a store-only workflow, these gates always returned `NOT_MET` because `eval_artifact_exists` and `eval_artifact_contains` predicates branch on `phase` being present — absent `phase` means filesystem fallback.

## Approach That Worked

1. **Direct YAML migration:** The fix was surgical — replace `path: docs/<phase>/*.md` with `phase: <phase>, name: "*.md"` in three pack files. The predicates already supported the store form (proven by spec and design gates). No code changes needed for migration.

2. **Dual-layer validator:** Added the validator in both `pack_validation.py` (Gate 1.5, catches bundled-pack issues at install time) and `ingest.py` `_validate_gate_spec()` (catches customized-pack issues at ingest time). The dual layer is necessary because customized packs bypass the pack-install path but still go through skill ingest.

3. **fnmatch for glob matching:** Used `fnmatch.fnmatch(glob_val, f"docs/{phase}/**")` to detect problematic globs. The `**` in fnmatch matches any characters including `/`, so `docs/qa/**` correctly matches both `docs/qa/file.md` and `docs/qa/subdir/file.md`.

## What Didn't Work / Didn't Try

- **Auto-routing in predicates:** Considered changing `eval_artifact_exists` to auto-detect `docs/<phase>` globs and route to the store silently. Rejected because silent auto-routing is a maintenance hazard — the predicate's behavior becomes context-dependent, and the author never learns the correct form. An explicit error is better.

## Key Decision

The validator uses fnmatch, not regex or literal matching. This means it catches `docs/qa/*.md`, `docs/qa/**/*.md`, `docs/qa/subdir/*.md` etc. — any glob that starts with `docs/<phase>/`. The glob `docs/solutions/*.md` is correctly allowed because it doesn't match any store-backed phase prefix.

## Recurrence Signal

This bug occurred because the pack author used the legacy `path:` form without realizing the gate evaluation layer had migrated to store queries. The validator prevents this from happening again by catching it at pack-load time with a clear error message. The same pattern could affect any future gate that migrates from disk to store — the validator approach should be replicated for any future migration.
