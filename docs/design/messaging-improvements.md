# Design Document: Messaging Improvements

## 1. Overview

This project adds user-facing messaging to three existing code paths that currently fail silently or with cryptic errors. These are **messaging-only** changes — no logic, retry behavior, or return types are modified.

**Problem**: Two known upstream issues (Ollama SSH key path mismatch, DuckDB 1.5.2 FTS stopwords corruption) fail silently or with unhelpful errors. Users lack guidance on what happened and how to fix it.

**Scope**: 4 files across 3 modules + README:
- `src/agentalloy/install/subcommands/pull_models.py` — SSH key messaging
- `src/agentalloy/storage/vector_store.py` — FTS rebuild failure messaging
- `src/agentalloy/install/subcommands/doctor.py` — New FTS status check
- `README.md` — Linux SSH key requirement note

**Constraints**:
- Do NOT change SSH key copy logic (shutil.copy2, chmod 0600, idempotency)
- Do NOT change FTS retry logic (checkpoint-based retries, catalog reset)
- Do NOT change return types of `_ensure_ollama_ssh_key()` or `rebuild_fts_index()`
- Do NOT add new dependencies
- All changes must be backward compatible

---

## 2. Architecture

### 2.1 Current State

```
pull_models.py:
  _ensure_ollama_ssh_key() -> bool
    - Returns True if key was copied, False otherwise
    - Silent in both cases — no user feedback

vector_store.py:
  rebuild_fts_index() -> None
    - Phase 1: checkpoint-based retries (3 attempts)
    - Phase 2: full catalog reset (drop + re-open connection)
    - On final failure: raises last_exc (produces traceback)

doctor.py:
  run_doctor() -> dict
    - Runs 12 checks (6 preflight-early + 8 verify + 4 doctor)
    - No FTS status check exists
```

### 2.2 Desired State

```
pull_models.py:
  _ensure_ollama_ssh_key() -> bool
    - Line ~181: if no source key exists, print warning to stdout
    - Line ~189: if key was copied, print confirmation to stdout
    - Return value unchanged (True/False)

vector_store.py:
  rebuild_fts_index() -> None
    - Lines 494-496: replace `raise last_exc` with:
      - logging.warning() with multi-line explanation
      - return None (graceful exit)
    - All retry logic unchanged

doctor.py:
  _check_fts_status() -> dict  [NEW]
    - Opens vector store, runs simple FTS query
    - Returns [OK] if query succeeds, [WARN] if stopwords error
    - Skips gracefully if vector store doesn't exist
  run_doctor() -> dict
    - Appends _check_fts_status() as check 13

README.md:
  - After line 73 (Windows unsupported note), add Linux SSH key note
```

---

## 3. File Changes

### 3.1 `src/agentalloy/install/subcommands/pull_models.py`

**Function**: `_ensure_ollama_ssh_key()` (lines 152-189)

**Change 1 — SSH key missing warning (line ~180-181)**

```python
    # Skip if source doesn't exist.
    if not source.exists():
        print(
            "Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519. "
            "Model pulls may fail. Run: "
            "cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519 && chmod 600 ~/.ollama/id_ed25519"
        )
        return False
```

**Change 2 — SSH key copied notification (line ~188-189)**

```python
    _shutil.copy2(str(source), str(target))
    target.chmod(0o600)
    print(
        "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull."
    )
    return True
```

**Rationale**: Both changes are single `print()` calls to stdout, placed at natural decision points within the existing function. No control flow changes.

### 3.2 `src/agentalloy/storage/vector_store.py`

**Function**: `rebuild_fts_index()` (lines 418-496)

**Change — Replace raise with logging.warning + return (lines 494-496)**

```python
        # All retries exhausted — log a user-friendly warning instead of raising.
        # This is a known DuckDB 1.5.2 FTS bug (stopwords catalog corruption).
        # Vector search continues to work; only BM25 (full-text search) is affected.
        import logging as _logging
        _logging.warning(
            "FTS index rebuild failed after all retries. "
            "This is a known DuckDB 1.5.2 bug (stopwords catalog corruption) — "
            "it is NOT an agentalloy issue. Vector search continues to work correctly. "
            "Full-text search (BM25) will be unavailable until DuckDB is upgraded. "
            "To retry after upgrading DuckDB: agentalloy reembed --rebuild-fts"
        )
        return
```

**Rationale**: The spec explicitly says `rebuild_fts_index()` already returns `None` on success, so replacing `raise last_exc` with `return` maintains the same return type. The warning is logged at WARNING level (not ERROR), so it won't trigger error monitoring.

### 3.3 `src/agentalloy/install/subcommands/doctor.py`

**New function**: `_check_fts_status()` (inserted after line 226, before line 229)

```python
def _check_fts_status() -> dict[str, Any]:
    """Check 13: Verify FTS index status by running a simple query."""
    import logging as _logging
    import duckdb as _duckdb
    from pathlib import Path as _Path
    from agentalloy.storage.vector_store import VectorStore, VectorStoreError
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    t0 = time.monotonic()
    try:
        repo_root = _repo_root()
        env_path = (repo_root / ".env").read_text() if (repo_root / ".env").exists() else ""
        duckdb_path_match = None
        for line in env_path.splitlines():
            if line.startswith("DUCKDB_PATH="):
                duckdb_path_match = line.split("=", 1)[1].strip()
                break

        if not duckdb_path_match:
            return {
                "name": "fts_status",
                "passed": True,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "detail": "DUCKDB_PATH not configured — FTS check skipped",
            }

        db_path = repo_root / duckdb_path_match
        if not db_path.exists():
            return {
                "name": "fts_status",
                "passed": True,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "detail": "Vector store file does not exist — FTS check skipped",
            }

        # Open the store and run a simple FTS query
        conn = _duckdb.connect(str(db_path))
        conn.execute("INSTALL fts; LOAD fts;")
        vs = VectorStore(conn, db_path=str(db_path))

        result = vs.search_bm25("test")
        vs.close()

        duration = int((time.monotonic() - t0) * 1000)
        return {
            "name": "fts_status",
            "passed": True,
            "duration_ms": duration,
            "detail": f"FTS query returned {len(result)} results",
        }

    except Exception as exc:
        msg = str(exc)
        duration = int((time.monotonic() - t0) * 1000)
        if "stopwords" in msg and "has been deleted" in msg:
            return {
                "name": "fts_status",
                "passed": False,
                "duration_ms": duration,
                "error": "FTS index corrupted (known DuckDB 1.5.2 bug: stopwords catalog corruption)",
                "remediation": "Upgrade DuckDB, then run: agentalloy reembed --rebuild-fts",
            }
        return {
            "name": "fts_status",
            "passed": False,
            "duration_ms": duration,
            "error": f"FTS check failed: {msg}",
            "remediation": "Run: agentalloy reembed --rebuild-fts",
        }
```

**Integration**: Append in `run_doctor()` after line 258:

```python
    checks.append(_check_fts_status())
```

**Rationale**: The function follows the same pattern as other doctor checks: try the operation, return a dict with `name`, `passed`, `duration_ms`, and optional `detail`/`error`/`remediation`. It opens a fresh DuckDB connection, runs a minimal query, and closes it.

### 3.4 `README.md`

**Change**: Insert after line 73 (after `**Note:** Windows is not currently supported.`):

```markdown
**Note:** Linux users: If Ollama's SSH key is stored in `~/.ssh/` (default), copy it before setup:
`cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519 && chmod 600 ~/.ollama/id_ed25519`
```

**Rationale**: Placed immediately after the Windows unsupported note in the Quickstart section, giving Linux users pre-emptive guidance before they encounter the SSH key issue.

---

## 4. API Changes

**None.** All changes are internal messaging only:
- `_ensure_ollama_ssh_key()` return type: `bool` (unchanged)
- `rebuild_fts_index()` return type: `None` (unchanged — was raising, now returns)
- `_check_fts_status()` is a new private function
- No public API surface is modified

---

## 5. Error Handling

### 5.1 SSH Key Missing
- **Before**: Silent `return False`, then `ollama pull` fails with opaque SSH error
- **After**: Warning printed to stdout before `ollama pull`, giving the user the exact fix command

### 5.2 FTS Rebuild Failure
- **Before**: `raise last_exc` produces a full traceback with no explanation
- **After**: `logging.warning()` with a clear multi-line message explaining:
  1. This is a known DuckDB 1.5.2 bug
  2. It is NOT an agentalloy issue
  3. Vector search still works
  4. FTS will be unavailable until DuckDB is upgraded
  5. How to retry: `agentalloy reembed --rebuild-fts`

### 5.3 Doctor FTS Check
- **No vector store file**: Returns `[OK]` with detail "Vector store file does not exist — FTS check skipped"
- **FTS works**: Returns `[OK]` with result count
- **Stopwords error**: Returns `[WARN]` with DuckDB bug message and remediation
- **Other errors**: Returns `[FAIL]` with error message and generic remediation

---

## 6. Constraints & Non-Goals

### Constraints
1. **No logic changes** — only add messaging (print/logging statements)
2. **No return type changes** — backward compatible
3. **No new dependencies** — use stdlib `logging` and `duckdb` (already a dependency)
4. **FTS check must be idempotent and fast** — opens/closes a fresh connection
5. **SSH key messages go to stdout** — user-facing
6. **FTS warning is WARNING level** — not ERROR

### Non-Goals
- Do NOT fix the DuckDB 1.5.2 FTS bug (upstream issue)
- Do NOT fix the Ollama SSH key path mismatch (upstream issue)
- Do NOT change the SSH key copy logic
- Do NOT change the FTS retry logic
- Do NOT change existing test behavior
- Do NOT add new CLI flags or subcommands

---

## 7. Risk Assessment

| Change | Risk | Mitigation |
|--------|------|-----------|
| SSH key print() calls | LOW | Simple stdout print, no control flow change |
| FTS raise -> logging.warning | LOW | Return type unchanged (None); callers already treat failure as non-fatal per docstring |
| New doctor check | LOW | Isolated function, graceful skip if deps missing |
| README note | NONE | Documentation only |

---

## 8. Implementation Order

1. **SSH key copied notification** (pull_models.py) — no dependencies
2. **SSH key missing warning** (pull_models.py) — no dependencies
3. **FTS rebuild failure warning** (vector_store.py) — no dependencies
4. **README SSH key note** (README.md) — no dependencies, can be done in parallel
5. **Doctor FTS status check** (doctor.py) — depends on vector_store changes for proper error detection

Steps 1-4 have no interdependencies and can be done in parallel. Step 5 should follow step 3 to ensure the error detection logic is in place.
