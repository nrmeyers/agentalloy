# Test Cycle Fixes

## Problem Statement

Six test cycles (3 native, 3 container) on a remote laptop revealed four distinct issues
affecting the agentalloy installation and bootstrap flow. Two issues block native installs
(Ollama SSH key path mismatch, FTS index rebuild failure). Two issues block container
installs (empty packs = zero skills, LadybugDB lock conflict during pack install).

These issues prevent reliable first-time setup on both native and container deployment
paths.

## Acceptance Criteria

### C01: Ollama SSH Key Auto-Copy (Native)

**Given** a native install where the user has an SSH key at `~/.ssh/id_ed25519` but no
key at `~/.ollama/id_ed25519`, **when** `agentalloy pull-models` (or `agentalloy setup`
with model pull) runs, **then** the agent copies `~/.ssh/id_ed25519` to
`~/.ollama/id_ed25519` with mode `0600` before executing `ollama pull`, and the pull
succeeds without `open /home/.../.ollama/id_ed25519: no such file or directory`.

**Verification**: Run `agentalloy setup` in non-interactive mode on a clean native install
with an existing `~/.ssh/id_ed25519` but no `~/.ollama/` directory. Confirm the pull
completes and `~/.ollama/id_ed25519` exists with mode `0600`.

### C02: FTS Index Rebuild Resilience (Native)

**Given** a native install using DuckDB 1.5.2, **when** `rebuild_fts_index()` is called
on the VectorStore, **then** the method either creates the FTS index successfully or
gracefully degrades (vector search continues working) without surfacing a
`"stopwords" has been deleted` error to the user.

**Verification**: Run `agentalloy verify` after a native install. Confirm all 8 verify
checks pass. Confirm no `WARNING FTS index rebuild failed` message appears in the setup
log.

### C03: Container Default Packs Installed (Container)

**Given** a container install using `-n` (non-interactive) without `--packs`, **when**
the container finishes bootstrap, **then** the always-on packs (core, documentation,
engineering, performance, refactoring) are installed and the container reports at least
5 skills.

**Verification**: Run `agentalloy setup` with `--non-interactive` and `--deployment
container`. After readiness returns `ready`, query the container for skill count via
`podman exec agentalloy uv run agentalloy reembed --dry-run 2>&1 | grep -c skills` or
the `/health` endpoint. Confirm skill count >= 5.

### C04: No LadybugDB Lock Conflict During Pack Install (Container)

**Given** a container install with `--packs` specifying one or more packs, **when** the
container runs pack installation, **then** all packs install successfully without
`IO exception: Could not set lock on file : /app/data/ladybug` errors.

**Verification**: Run `agentalloy setup --non-interactive --deployment container
--packs core,documentation,engineering,performance,refactoring`. Confirm all 5 packs
install and re-embed completes. Confirm no lock errors in container logs.

### C05: Backward Compatibility

**Given** an existing container deployed before these fixes, **when** the container image
is rebuilt with the updated entrypoint, **then** the new container bootstraps correctly
using the existing data volume (no data loss, no schema migration errors).

**Verification**: Build with updated entrypoint, mount an existing data volume, start the
container. Confirm bootstrap detects existing state and does not corrupt data.

## Requirements

### REQ-1: Auto-Copy Ollama SSH Key Before Pull (Native)

**Problem**: Ollama 0.20.3 looks for its SSH key at `~/.ollama/id_ed25519`, but the
system SSH key is at `~/.ssh/id_ed25519`. When the standard key exists but the Ollama
one does not, `ollama pull` fails with `open ~/.ollama/id_ed25519: no such file or
directory`.

**Fix**: The `_ensure_ollama_ssh_key()` function in `pull_models.py` already implements
this logic (lines 152-189). It must be verified to be called in the correct place in
the native setup flow, specifically before `_auto_pull()` executes `ollama pull`.

**Files**:
- `src/agentalloy/install/subcommands/pull_models.py` (lines 152-189, 650-654)

**Key logic**:
- Check if `~/.ollama/id_ed25519` exists; skip if yes (idempotent).
- Check if `~/.ssh/id_ed25519` exists; skip if no source key.
- Create `~/.ollama/` directory if needed.
- Copy with `shutil.copy2()`, then `chmod(0o600)`.
- This is a no-op for containers because `~/.ollama` is bind-mounted from the host.

**Current state**: The function exists and is called at line 654 inside `_auto_pull()`.
The fix is already in place for the native flow. No code changes needed for Issue 1.

### REQ-2: FTS Index Rebuild Workaround (Native)

**Problem**: DuckDB 1.5.2 FTS extension has a bug where the internal stopwords table
gets corrupted during index creation, causing `TransactionContext Error: Failed to
commit: Could not commit creation of dependency, subject "stopwords" has been deleted.`

**Fix**: The `rebuild_fts_index()` method in `vector_store.py` already implements a
two-phase retry strategy (lines 418-493):
- Phase 1: Checkpoint-based retries (3 attempts with 0.25s, 0.5s, 1.0s delays) on the
  specific "stopwords has been deleted" error.
- Phase 2: Full catalog reset (drop index, checkpoint, lock_shm, close/reopen connection,
  re-install FTS extension, retry create).

**Files**:
- `src/agentalloy/storage/vector_store.py` (lines 418-493)

**Key logic**:
- Phase 1 retries with checkpoint between attempts.
- Phase 2 does a full connection close/reopen for a fresh catalog.
- If both phases fail, the error is raised (non-fatal for vector search).

**Current state**: The fix is already in place. The test `test_rebuild_fts_reset_on_persistent_stopwords_error`
in `tests/test_vector_store.py` validates this behavior. No code changes needed for Issue 2.

### REQ-3: Install Always-On Packs When Packs Is Empty (Container)

**Problem**: When `packs` is empty (user passed `-n` without `--packs`), the container
entrypoint script skips pack installation entirely. The `has_packs` flag at
`container_runtime.py:340` is `False`, so the entrypoint only echoes
`"No packs specified - skipping pack installation"` without ever calling
`agentalloy install-packs`. This means always-on packs are never installed.

**Fix**: Modify `_build_entrypoint_script()` in `container_runtime.py` to always call
`agentalloy install-packs` during bootstrap, even when no explicit packs are specified.
When `packs` is empty, the call should be `agentalloy install-packs --non-interactive
--no-restart`, which causes `install_packs._select_packs()` to install only the
always-on packs (core, documentation, engineering, performance, refactoring).

**Files**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 316-491)

**Key logic**:
- Change the `else` branch at line 474-475 from skipping to calling install-packs.
- When `has_packs` is False, still run: `uv run agentalloy install-packs --non-interactive --no-restart`
- When `has_packs` is True, keep the existing per-pack loop.
- The `--non-interactive` flag ensures `install_packs._select_packs()` falls through to
  the non-TTY default (line 402-404 of `install_packs.py`), which installs only always-on packs.

**Current state** (container_runtime.py lines 443-475):
```python
if has_packs:
    lines.extend([
        f"    PACK_LIST=({pack_array_literal})",
        f"    TOTAL={packs_total}",
        # ... per-pack loop ...
    ])
else:
    lines.append('    echo "> No packs specified - skipping pack installation"')
```

**Change**: Replace the `else` branch to run `agentalloy install-packs --non-interactive
--no-restart` instead of skipping.

### REQ-4: Resolve LadybugDB Lock Conflict (Container)

**Problem**: The entrypoint starts `uvicorn` BEFORE pack installation (line 440-441).
The running uvicorn process holds a write lock on the KuzuDB (LadybugDB) file at
`/app/data/ladybug`. When `agentalloy install-packs` tries to write to the same file,
it fails with `IO exception: Could not set lock on file : /app/data/ladybug`.

**Fix**: Reorder the entrypoint script so that pack installation runs BEFORE uvicorn
starts. The new sequence is:
1. Migrations
2. Pack installation (if any)
3. Mark bootstrap complete
4. Start uvicorn

This eliminates the lock conflict entirely because uvicorn is not running when
install-packs writes to the database.

**Files**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 316-491)

**Key logic**:
- Move the uvicorn start (currently at line 439-441) to AFTER the pack installation
  block and bootstrap-complete flag.
- The new sequence:
  ```
  # After bootstrap (ollama + migrations):
  if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
      # Pack installation (always runs, may be no-op if no packs)
      ... pack loop or install-packs call ...
      # Mark bootstrap complete
      rm -f "$LOCK"
      touch "$COMPLETE"
      echo ">> Bootstrap complete"
  fi
  # Start uvicorn AFTER all bootstrap steps
  echo ">> Starting uvicorn..."
  uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &
  UVICORN_PID=$!
  wait $UVICORN_PID
  ```

**Current state** (container_runtime.py lines 435-488):
```python
# Fast-start uvicorn (BEFORE pack install)
echo ">> Starting uvicorn (fast-start)..."
uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &
UVICORN_PID=$!

if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    # Pack installation runs AFTER uvicorn starts (LOCK CONFLICT)
    ...
    rm -f "$LOCK"
    touch "$COMPLETE"
    echo ">> Bootstrap complete"
fi

wait $UVICORN_PID
```

**Change**: Move the uvicorn start block to after the pack installation + bootstrap
complete block.

### REQ-5: Maintain Backward Compatibility

**Problem**: The entrypoint is a bash script generated by Python and mounted into the
container. Changes must not break existing containers or data volumes.

**Requirements**:
- The new entrypoint must correctly detect and skip bootstrap if `.bootstrap-complete`
  already exists (already handled by the `BOOTSTRAP_NEEDED` check).
- The new entrypoint must handle stale locks from crashed containers (already handled
  by the stale lock recovery at lines 361-371).
- The new entrypoint must handle partial bootstrap (crash mid-pack) and resume from
  checkpoints (already handled by the checkpoint logic).
- The new entrypoint must trap SIGTERM for graceful shutdown (already handled at line 433).

**Files**:
- `src/agentalloy/install/subcommands/container_runtime.py` (lines 316-491)

## Non-Goals

- **Issue 2 root cause fix**: The DuckDB 1.5.2 FTS stopwords bug is a upstream issue.
  The workaround (checkpoint retries + catalog reset) is sufficient. A permanent fix
  requires either upgrading DuckDB or patching the FTS extension.
- **Container read-only DB mode**: Do not attempt to start uvicorn with read-only DB
  access during bootstrap. The health checks probe the database, and read-only mode
  would cause false health failures.
- **Alternative pack installation methods**: Do not change how `agentalloy install-packs`
  discovers or installs packs. Only change the entrypoint's invocation of it.
- **Native install pack flow**: The native `install-packs` already handles always-on
  packs correctly via `_select_packs()` (line 379-404). No changes needed.

## Current State

### Issue 1: Ollama SSH Key Path Mismatch (RESOLVED)

- **File**: `src/agentalloy/install/subcommands/pull_models.py`
- **Function**: `_ensure_ollama_ssh_key()` at lines 152-189
- **Caller**: `_auto_pull()` at line 654 (called before `ollama pull` at line 659)
- **Status**: Fix is already in place. The function copies `~/.ssh/id_ed25519` to
  `~/.ollama/id_ed25519` if the target is missing and the source exists.
- **Note**: This is a no-op for containers because `~/.ollama` is bind-mounted from
  the host (mounted at `/root/.ollama` in the container at `container_runtime.py:561`).

### Issue 2: FTS Index Rebuild Failure (RESOLVED)

- **File**: `src/agentalloy/storage/vector_store.py`
- **Function**: `VectorStore.rebuild_fts_index()` at lines 418-492
- **Status**: Two-phase retry strategy is already implemented:
  - Phase 1: Checkpoint-based retries (3 attempts, 0.25s/0.5s/1.0s delays)
  - Phase 2: Full catalog reset (drop, checkpoint, lock_shm, close/reopen, retry)
- **Tests**: `test_rebuild_fts_reset_on_persistent_stopwords_error` in
  `tests/test_vector_store.py` validates the fallback behavior.
- **Note**: The workaround degrades BM25 search to empty results if both phases fail,
  but vector search continues working. This is acceptable as documented.

### Issue 3: Empty Packs = 0 Skills (NEEDS FIX)

- **File**: `src/agentalloy/install/subcommands/container_runtime.py`
- **Function**: `_build_entrypoint_script()` at lines 316-491
- **Location**: Line 474-475 — the `else` branch when `has_packs` is False
- **Current behavior**:
  ```python
  else:
      lines.append('    echo "> No packs specified - skipping pack installation"')
  ```
- **Root cause**: When `packs` is empty, `has_packs = False`, and the entrypoint
  skips pack installation entirely. The `agentalloy install-packs` command is never
  called, so always-on packs are never installed.
- **Expected behavior**: Always call `agentalloy install-packs --non-interactive
  --no-restart` during bootstrap. When no explicit packs are given, the `--non-interactive`
  flag causes `_select_packs()` to install only always-on packs.

### Issue 4: LadybugDB Lock Conflict (NEEDS FIX)

- **File**: `src/agentalloy/install/subcommands/container_runtime.py`
- **Function**: `_build_entrypoint_script()` at lines 316-491
- **Location**: Line 439-441 (uvicorn start) and lines 446-471 (pack install)
- **Current order**:
  1. Start uvicorn (line 440)
  2. Pack installation (line 466)
  3. Mark bootstrap complete (line 482)
- **Root cause**: Uvicorn holds a write lock on `/app/data/ladybug` (KuzuDB). When
  pack installation runs, it tries to open the same database and fails with
  `IO exception: Could not set lock on file`.
- **Expected order**:
  1. Pack installation
  2. Mark bootstrap complete
  3. Start uvicorn

## Desired State

### Entry Point Script Flow (After Fix)

```
#!/bin/bash
set -e

APP_DIR=${APP_DIR:-/app}

# 1. Stale lock recovery (existing)
# 2. Checkpoint helpers (existing)
# 3. Bootstrap decision (existing)

if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    date -Iseconds > "$LOCK"

    # Ollama installation (existing)
    if ! command -v ollama &> /dev/null; then
        echo ">> Installing Ollama..."
        curl -fsSL https://ollama.ai/install.sh | sh
    fi

    echo ">> Starting Ollama..."
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &
    OLLAMA_PID=$!

    # Wait for Ollama (existing)
    for i in $(seq 1 30); do
        if curl -sf http://127.0.0.1:11434 > /dev/null 2>&1; then
            echo ">> Ollama is ready"
            break
        fi
        sleep 1
    done

    # Embedding model pull (existing)
    echo ">> Checking embedding model..."
    if ! ollama list | grep -q qwen3-embedding; then
        echo ">> Pulling qwen3-embedding:0.6b..."
        ollama pull qwen3-embedding:0.6b
    fi

    # Migrations (existing)
    echo ">> Running migrations..."
    uv run python -m agentalloy.migrate

    # --- PACK INSTALLATION (moved before uvicorn) ---
    if [ -n "$PACKS" ]; then
        # Per-pack loop (existing)
        PACK_LIST=($PACKS)
        TOTAL=${#PACK_LIST[@]}
        # ... per-pack install with checkpoints ...
    else
        # Always-on packs only (new)
        echo ">> Installing always-on packs..."
        uv run agentalloy install-packs --non-interactive --no-restart
    fi

    # Mark bootstrap complete (existing, but now BEFORE uvicorn)
    rm -f "$LOCK"
    touch "$COMPLETE"
    echo ">> Bootstrap complete"
fi

# --- Start uvicorn AFTER all bootstrap steps (moved) ---
echo ">> Starting uvicorn..."
uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &
UVICORN_PID=$!

wait $UVICORN_PID
```

### Key Differences from Current State

| Aspect | Current | After Fix |
|--------|---------|-----------|
| Uvicorn start | Before pack install | After pack install + bootstrap complete |
| Empty packs | Skip entirely | Run `install-packs --non-interactive` |
| Lock conflict | uvicorn holds lock during pack install | No uvicorn during pack install |
| Fast-start /readiness | Available during bootstrap | Available after bootstrap |

## Implementation Plan

### Phase 1: Fix Container Entrypoint (Issues 3 & 4)

**File**: `src/agentalloy/install/subcommands/container_runtime.py`

**Change 1**: Reorder the entrypoint script so uvicorn starts AFTER pack installation.

**Change 2**: Replace the empty-packs skip with an `install-packs --non-interactive` call.

**Steps**:
1. In `_build_entrypoint_script()`, move the uvicorn start block (lines 435-441) to
   after the pack installation block (after line 484).
2. Replace the `else` branch (lines 474-475) with an `install-packs --non-interactive
   --no-restart` call.
3. Ensure the SIGTERM trap covers only the new uvicorn PID (OLLAMA_PID is no longer
   relevant since Ollama is started in the background before the pack loop and stays
   running).
4. Update the function docstring to reflect the new order.

**Testing**:
- Unit test: Mock the subprocess calls and verify the generated script contains
  `install-packs --non-interactive` in the empty-packs branch.
- Unit test: Verify uvicorn start line appears after bootstrap-complete in the script.
- E2E: Run container install with `-n` (no packs) and verify always-on packs are installed.
- E2E: Run container install with `--packs core,documentation` and verify all packs install.

### Phase 2: Verify Native Fixes (Issues 1 & 2)

**No code changes needed.** Verify:
1. `_ensure_ollama_ssh_key()` is called in `_auto_pull()` before `ollama pull`.
2. `rebuild_fts_index()` handles the stopwords error via checkpoint retries + catalog reset.
3. Run `agentalloy setup --non-interactive --deployment native` on a clean native install
   and verify all checks pass.

### Phase 3: Update Tests

**Files to update**:
- `tests/install/test_container_runtime_readiness.py` — update expectations for the
  new entrypoint script order.
- `tests/test_container_edge_cases.py` — add tests for empty packs = always-on install.
- `tests/test_container_edge_cases.py` — add tests for no lock conflict during pack install.

## Constraints

- The entrypoint is a bash script generated by Python. Changes must produce valid bash.
- The entrypoint is mounted as a read-only volume (`:ro`) into the container.
- The entrypoint must maintain backward compatibility with existing containers and data
  volumes.
- The `_ensure_ollama_ssh_key()` fix is native-only; containers mount `~/.ollama` from
  the host, so the key path issue does not apply to containers.
- The DuckDB FTS workaround is a software-level fix in `vector_store.py`; it does not
  require schema changes or data migration.
- The entrypoint change must not break the existing checkpoint-based resume logic for
  partial bootstrap crashes.

## Edge Cases

- **Stale lock from crashed container**: The existing stale lock recovery (lines 361-371)
  detects locks older than 2 hours and wipes them. This continues to work after the
  reordering because the lock file is still created at the same point in the bootstrap.

- **Partial bootstrap crash mid-pack**: The existing checkpoint logic (lines 377-380,
  452-470) records completed packs in `.bootstrap-checkpoints`. On restart, the script
  skips packs already in the checkpoint file. This continues to work after reordering.

- **Container restart during pack install**: If the container is restarted while pack
  installation is in progress, the stale lock recovery will detect the stale lock and
  start fresh (not resume from checkpoints, since the lock is wiped). This is acceptable
  behavior — the user can re-run setup or let the checkpoint resume on the next restart.

- **Empty container data volume**: On first boot, the data volume is empty. The entrypoint
  runs migrations, pack installation, and marks bootstrap complete. The new order ensures
  pack installation completes before uvicorn starts, avoiding the lock conflict.

- **Existing data volume from prior version**: If upgrading from a version with the old
  entrypoint, the `.bootstrap-complete` flag may already exist. The `BOOTSTRAP_NEEDED`
  check at line 394-397 will skip the entire bootstrap block, and uvicorn will start
  directly. This is correct behavior — no re-installation needed.

- **`--non-interactive` with explicit `--packs`**: When the user provides explicit packs
  via `--packs`, the entrypoint runs the per-pack loop (not the `--non-interactive`
  install-packs call). The `--non-interactive` flag is only used when no packs are
  specified, ensuring always-on packs are installed.

- **Multiple container restarts**: If the container is stopped and restarted multiple
  times, the `.bootstrap-complete` flag persists in the data volume. Each restart
  detects the flag and skips bootstrap, starting uvicorn directly. This is correct
  and efficient.

## Success Criteria

1. **Native install succeeds**: `agentalloy setup --non-interactive --deployment native`
   completes with all 8 verify checks passing, no SSH key errors, and no FTS warnings.

2. **Container install with empty packs**: `agentalloy setup --non-interactive`
   (container, no `--packs`) installs always-on packs and reports >= 5 skills.

3. **Container install with packs**: `agentalloy setup --non-interactive --deployment
   container --packs core,documentation,engineering,performance,refactoring` installs
   all 5 packs without lock errors.

4. **Container install with all packs**: `agentalloy setup --non-interactive --deployment
   container --packs all` installs all available packs without lock errors.

5. **Backward compatibility**: Rebuilding the container image with the updated entrypoint
   and mounting an existing data volume does not corrupt data or require manual
   intervention.

6. **No regression in existing tests**: All existing tests in
   `tests/install/test_container_runtime_readiness.py`,
   `tests/test_container_edge_cases.py`, and
   `tests/test_vector_store.py` continue to pass.
