## Manage per-worktree stream_id for workflow state isolation (GH #548)

When a repository has multiple git worktrees, each worktree needs its own workflow state (phase, contracts, approvals) in the DuckDB store. The `stream_id` column provides that isolation while `repo_slug` keeps code-index lookups worktree-independent.

The core `stream_id` infrastructure was merged in PR #584/#585. This PR completes the user-facing surface by adding the missing CLI subcommand.

## What this adds

**`agentalloy stream`** — CLI subcommand to manage per-worktree stream_id.

Three actions:
- **`status`** — show the resolved stream_id and how it was determined (binding file / env var / worktree path hash)
- **`use <id>`** — pin an explicit stream_id to `.agentalloy/.stream` so this worktree keeps the same identifier across sessions
- **`clear`** — remove the pin, falling back to the worktree path hash

### Changes

| File | Description |
|---|---|
| `src/agentalloy/install/subcommands/stream.py` | New subcommand module (132 lines) |
| `src/agentalloy/install/__main__.py` | Registered in imports + `_SUBCOMMANDS` list |
| `tests/install/test_stream_command.py` | 11 tests covering all actions and edge cases |

### QA notes

- `ruff check --fix` ✅
- `ruff format` ✅
- `pyright` ✅
- `pytest` 11/11 ✅

The QA agent caught and fixed:
1. **Crash** on non-existent `--project-root` → added `root.exists()` guard
2. Inline `__import__("os")` → proper top-level import
3. Misleading test name → renamed for clarity
4. Added regression test for non-existent root edge case
