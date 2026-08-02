# Ship: Fix upstream malformed file silent fallback

## Summary

Refuses malformed per-repo `.agentalloy/upstream` files instead of silently falling back to the global upstream.

**Problem:** `read_upstream` returned `Upstream | None`, collapsing "no file" and "file is broken" into the same `None` path. A corrupted or misconfigured per-repo upstream silently routed prompts to the global upstream — a privacy issue where users expecting local/self-hosted routing could accidentally hit `api.openai.com`.

**Fix:** `read_upstream` now returns `UpstreamFile(kind="absent"|"valid"|"error")`, a discriminated result. The proxy returns 503 with `upstream_parse_error` when the file exists but is malformed. The ops API reports the error in `RepoInfo.upstream_error`.

**PR:** https://github.com/nrmeyers/agentalloy/pull/532

**Changelog:**
- **Breaking change** (internal API): `read_upstream()` now returns `UpstreamFile` instead of `Upstream | None`. The `UpstreamFile` dataclass carries `kind` (`"absent"`, `"valid"`, or `"error"`) and `detail` for error cases.
- **Behavior change:** Malformed per-repo upstream files now produce a 503 error response instead of silently falling back to the global upstream. This prevents accidental routing to unintended models.
- **New field:** `RepoInfo.upstream_error` — populated when the per-repo upstream file is malformed.

**Files changed:**
- `src/agentalloy/api/proxy_context.py` — Added `UpstreamFile` dataclass, refactored `read_upstream`
- `src/agentalloy/api/proxy_router.py` — Updated `_resolve_upstream` and handler, added `_upstream_parse_error`
- `src/agentalloy/web/ops_api.py` — Added `upstream_error` to `RepoInfo`, updated `_repo_info`
- `tests/api/test_upstream_resolution.py` — Updated assertions, added 4 error-case tests
- `tests/install/test_add_command.py` — Updated assertions
- `tests/install/test_worktree_command.py` — Updated assertions

## Rollback

Revert this PR:
```bash
git revert <merge-commit>
```

Or cherry-pick the 3 commits in reverse order:
```bash
git revert HEAD~2..HEAD
```

No data migration to undo. No config changes required. The revert restores `read_upstream` returning `Upstream | None` and silences malformed upstream files again.
