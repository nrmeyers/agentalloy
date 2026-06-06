# Messaging Improvements: SSH Key Warnings, FTS DuckDB Bug Messaging, README Note

## Problem Statement

Two known upstream issues affect users during setup but are NOT agentalloy bugs:

1. **Ollama SSH key path mismatch** (native only) — Ollama expects `~/.ollama/id_ed25519` but systems store it at `~/.ssh/id_ed25519`. The `_ensure_ollama_ssh_key()` workaround copies the key and now prints user-facing messages.

2. **DuckDB >= 1.5.3 FTS stopwords corruption** (all installs) — FTS index rebuild fails with stopwords catalog corruption. The `rebuild_fts_index()` function now logs a WARNING instead of raising. Doctor reports FTS status.

3. **README documents SSH key requirement** for Linux users in the Quickstart section.

All four items below are **implemented**. This spec documents the final state for verification and future reference.

---

## Acceptance Criteria

### C01: SSH Key Copied — User Notification

**Given** a native install where the user has an SSH key at `~/.ssh/id_ed25519` but no key at `~/.ollama/id_ed25519`, **when** `agentalloy pull-models` (or `agentalloy setup` with model pull) runs and `_ensure_ollama_ssh_key()` copies the key, **then** a user-facing message is printed to stdout:

```
Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull.
```

**File**: `src/agentalloy/install/subcommands/pull_models.py`, lines 194-196

**Verification**: Run `agentalloy setup` in non-interactive mode on a clean native install with an existing `~/.ssh/id_ed25519` but no `~/.ollama/` directory. Confirm the stdout message appears during the pull-models step.

---

### C02: SSH Key Missing — Warning Message

**Given** a native install where neither `~/.ssh/id_ed25519` nor `~/.ollama/id_ed25519` exists, **when** `agentalloy pull-models` (or `agentalloy setup` with model pull) runs and `_ensure_ollama_ssh_key()` finds no source key, **then** a warning message is printed to stdout before the `ollama pull` call:

```
Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519. Model pulls may fail. Run: cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519 && chmod 600 ~/.ollama/id_ed25519
```

**File**: `src/agentalloy/install/subcommands/pull_models.py`, lines 181-185

**Verification**: Run `agentalloy setup` on a system with no SSH key pair. Confirm the warning message appears before the pull attempt.

---

### C03: FTS Rebuild Failure — User-Facing Warning (Not Exception)

**Given** a native install using DuckDB with FTS stopwords corruption, **when** `rebuild_fts_index()` exhausts all retries and catalog resets and encounters the DuckDB stopwords error, **then** the function logs a clear WARNING-level message (does NOT raise) explaining:

- This is a known DuckDB bug (stopwords catalog corruption)
- It is not an agentalloy issue
- Vector search still works correctly
- FTS (full-text search) will be unavailable until DuckDB is upgraded
- How to retry: `agentalloy reembed --rebuild-fts`

**File**: `src/agentalloy/storage/vector_store.py`, lines 494-504

**Verification**: Run `agentalloy reembed --rebuild-fts` on a system with the DuckDB FTS bug. Confirm the output contains a clear WARNING (not a traceback) about the DuckDB bug and that vector search continues to work.

---

### C04: Doctor Reports FTS Status

**Given** the `doctor` subcommand, **when** `agentalloy doctor` runs, **then** a new check (check 13) verifies FTS index status by checking for FTS tables in the `fts_main_fragment_embeddings` schema:

- If FTS works normally, report as `[OK]` with detail "FTS (BM25 full-text search) index present"
- If FTS is missing (DuckDB 1.5.3 bug), report as `[FAIL]` with the DuckDB bug message and remediation
- If DuckDB file doesn't exist yet, report as `[OK]` with detail "DuckDB not present yet — FTS check deferred"

**File**: `src/agentalloy/install/subcommands/doctor.py`, lines 275-342 (`_check_fts_status()`)
**Integration**: `run_doctor()`, line 382 — `checks.append(_check_fts_status())`

**Verification**: Run `agentalloy doctor` and confirm a new check appears in the output reporting FTS status. On a system with the DuckDB bug, it should show `[FAIL]` with the DuckDB bug message.

---

### C05: README Documents SSH Key Requirement for Linux

**Given** the README.md Quickstart section, **when** a user reads the Quickstart section after the "Note: Windows is not currently supported." line, **then** a note is present:

```
**Note (Linux):** Ollama 0.20.3+ expects its SSH key at `~/.ollama/id_ed25519`, but most Linux systems store it at `~/.ssh/id_ed25519`. If you get an SSH key error during setup, copy it first:

```bash
cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519 && chmod 600 ~/.ollama/id_ed25519
```

The setup wizard will auto-copy the key if it detects this mismatch.
```

**File**: `README.md`, lines 75-81

**Verification**: Read the README.md Quickstart section. Confirm the Linux SSH key note appears immediately after the Windows unsupported note.

---

## Requirements

### REQ-1: SSH Key Copied Notification

- **File**: `src/agentalloy/install/subcommands/pull_models.py`, line 194
- **Implementation**: `print("Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull.")` after `target.chmod(0o600)` and before `return True`
- **Output**: stdout (not stderr)

### REQ-2: SSH Key Missing Warning

- **File**: `src/agentalloy/install/subcommands/pull_models.py`, line 181
- **Implementation**: `print()` with warning message in the `if not source.exists():` branch, before `return False`
- **Output**: stdout (not stderr)
- **Return value**: Still `False` (unchanged)

### REQ-3: FTS Rebuild Failure Warning

- **File**: `src/agentalloy/storage/vector_store.py`, line 494
- **Implementation**: `logging.warning()` with multi-line message replaces the previous `raise last_exc`
- **Log level**: WARNING (not ERROR)
- **Return value**: Still `None` (graceful exit, unchanged)

### REQ-4: Doctor FTS Status Check

- **File**: `src/agentalloy/install/subcommands/doctor.py`, line 275
- **Implementation**: New `_check_fts_status()` function that queries `information_schema.tables` for `fts_main_fragment_embeddings` schema
- **Integration**: Appended to checks list in `run_doctor()` at line 382
- **Returns**: dict with `name`, `passed`, `duration_ms`, and optional `detail`/`error`/`remediation`

### REQ-5: README SSH Key Note

- **File**: `README.md`, line 75
- **Implementation**: Two-line note after the Windows unsupported note (line 73)
- **Content**: Explains the Ollama SSH key path mismatch, provides the copy command, and mentions the auto-copy workaround

---

## Non-Goals

- Do NOT fix the DuckDB FTS bug — it is an upstream DuckDB issue
- Do NOT fix the Ollama SSH key path mismatch — it is an upstream Ollama issue
- Do NOT change the SSH key copy logic (`shutil.copy2`, `chmod 0600`, idempotency)
- Do NOT change the FTS retry logic (checkpoint-based retries, catalog reset)
- Do NOT change existing test behavior
- Do NOT add new dependencies
- Do NOT change the return type of `_ensure_ollama_ssh_key()` (backward compatible: `bool`)
- Do NOT change the return type of `rebuild_fts_index()` (backward compatible: `None`)

---

## Current State (Implemented)

### SSH Key Handling (`pull_models.py`: 152-197)

```
_ensure_ollama_ssh_key() -> bool
  - Line 176-177: Skip if target already exists (idempotent)
  - Line 181-185: Print warning if no source key exists, return False
  - Line 188-189: Create ~/.ollama/ if needed
  - Line 192-193: Copy with chmod 0600
  - Line 194-196: Print confirmation, return True
```

### FTS Error Handling (`vector_store.py`: 418-505)

```
rebuild_fts_index() -> None
  - Phase 1: checkpoint-based retries (3 attempts, lines 450-467)
  - Phase 2: full catalog reset (lines 469-492)
  - Lines 494-504: logging.warning() instead of raise (graceful exit)
```

### Doctor FTS Check (`doctor.py`: 275-342)

```
_check_fts_status(duck_path: str) -> dict
  - Line 285-293: Skip if DuckDB file doesn't exist
  - Line 295-308: Query information_schema for FTS tables
  - Line 312-318: Return [OK] if FTS tables found
  - Line 320-333: Return [FAIL] with DuckDB 1.5.3 bug message
  - Line 334-342: Catch-all for other errors
```

### Doctor DuckDB Version Check (`doctor.py`: 231-272)

```
_check_duckdb_version() -> dict
  - Parses version string "X.Y.Z" -> (X, Y, Z)
  - Requires >= 1.5.3 (fixes FTS stopwords bug)
  - Line 251: Detail includes version and bug reference
  - Line 259-262: Error message with upgrade command
```

### README (`README.md`: 73-81)

```
Line 73:  **Note:** Windows is not currently supported.
Line 75:  **Note (Linux):** Ollama 0.20.3+ expects its SSH key...
Line 77:  ```bash
Line 78:  cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519 && chmod 600 ~/.ollama/id_ed25519
Line 79:  ```
Line 81:  The setup wizard will auto-copy the key if it detects this mismatch.
```

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Container installs (`~/.ollama` mounted from host) | SSH key copy is a no-op (target already exists). No message printed. |
| No SSH key pair at all | Warning printed to stdout with exact fix command. `ollama pull` may still fail (upstream issue). |
| FTS index already built | Doctor check reports `[OK]` with detail "FTS (BM25 full-text search) index present" |
| FTS index not yet built | Doctor check reports `[FAIL]` with DuckDB 1.5.3 bug message and remediation |
| Vector store file missing | Doctor check reports `[OK]` with detail "DuckDB not present yet — FTS check deferred" |
| DuckDB version < 1.5.3 | Doctor check 14 (`duckdb_version_ok`) reports `[FAIL]` with upgrade command |
| Both source and target keys exist | No action, no message. Function returns `False` (idempotent). |
| Source exists but `~/.ollama/` parent doesn't | `mkdir(parents=True, exist_ok=True)` creates it before copy. |

---

## Tests

### SSH Key Tests (`tests/test_ollama_ssh_key.py`)

| Test | What it verifies |
|---|---|
| `test_no_source_key_skips_cleanly` | No exception, no `~/.ollama/` created, returns `False` |
| `test_target_already_exists_skips` | Returns `False`, target unchanged |
| `test_copy_creates_dir_and_sets_perms` | Creates `~/.ollama/`, copies file, `chmod 0600` |
| `test_copy_preserves_content` | Copied content matches source exactly |
| `test_no_source_key_prints_warning` | `capfd` captures warning message in stdout |
| `test_warning_includes_fix_command` | Warning contains the `cp` command |
| `test_no_source_key_return_value_unchanged` | Returns `False` (backward compatible) |
| `test_no_source_key_no_dir_created` | `~/.ollama/` not created when no source |
| `test_copy_prints_notification` | `capfd` captures confirmation message in stdout |
| `test_copy_prints_to_stdout_not_stderr` | Message in `out`, not `err` |
| `test_copy_return_value_unchanged` | Returns `True` (backward compatible) |
| `test_no_print_when_key_already_exists` | No message when target already exists |

### Doctor Tests (`tests/install/test_doctor.py`)

| Test | What it verifies |
|---|---|
| `TestFtsStatus.test_duckdb_not_present` | Returns `[OK]` with "deferred" when DB file missing |
| `TestFtsStatus.test_fts_index_present` | Returns `[OK]` with "FTS" in detail |
| `TestFtsStatus.test_fts_index_missing` | Returns `[FAIL]` with "DuckDB 1.5.3" and "NOT an agentalloy issue" |
| `TestDuckdbVersion.test_version_ok` | Current DuckDB version passes (>= 1.5.3) |
| `TestDuckdbVersion.test_version_parse` | Version parsing handles "1.5.3" and "1.5" |
| `TestRunDoctor.test_returns_all_checks` | All 14+ checks present including `fts_index_status` and `duckdb_version_ok` |
| `TestRunDoctor.test_output_shape` | All checks have `name` and `passed` keys |

---

## Implementation Order (for reference)

1. SSH key copied notification (`pull_models.py`) — no dependencies
2. SSH key missing warning (`pull_models.py`) — no dependencies
3. FTS rebuild failure warning (`vector_store.py`) — no dependencies
4. FTS status check (`doctor.py`) — standalone (queries FTS tables directly)
5. DuckDB version check (`doctor.py`) — standalone
6. README SSH key note (`README.md`) — no dependencies, can be done in parallel

---

## Success Criteria

- [x] `agentalloy setup` with existing `~/.ssh/id_ed25519` prints the "Copied SSH key" message to stdout
- [x] `agentalloy setup` with no SSH key prints the "Ollama SSH key not found" warning to stdout
- [x] `agentalloy reembed --rebuild-fts` on DuckDB with FTS bug logs a clear WARNING message instead of raising
- [x] `agentalloy doctor` includes check 13 (`fts_index_status`) reporting FTS status
- [x] `agentalloy doctor` includes check 14 (`duckdb_version_ok`) reporting DuckDB version >= 1.5.3
- [x] README.md Quickstart section includes the Linux SSH key note after the Windows unsupported note
- [x] No existing tests are broken by these changes
- [x] All changes are backward compatible (no API changes, no breaking changes to return types)
