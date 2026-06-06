# Test Plan: Messaging Improvements

## 1. Overview

This test plan covers testing for messaging improvements across 4 files. All changes are messaging-only — no logic, retry behavior, or return types are modified. Tests verify that the correct messages are produced in the correct scenarios.

**Scope**: 5 acceptance criteria from the spec:
- C01: SSH key copied notification
- C02: SSH key missing warning
- C03: FTS rebuild failure warning (not exception)
- C04: Doctor reports FTS status
- C05: README documents SSH key requirement

---

## 2. Unit Tests

### 2.1 SSH Key Copied Notification (`_ensure_ollama_ssh_key`)

**Test file**: `tests/test_ollama_ssh_key.py` (extend existing)

**Tests**:

| Test | Description | Assertion |
|------|-------------|-----------|
| `test_copy_prints_notification` | When source key exists and target does not, `print()` is called with the copy confirmation message | `capfd` captures "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." in stdout |
| `test_copy_prints_to_stdout_not_stderr` | The notification goes to stdout, not stderr | `capfd.readouterr().out` contains the message; `capfd.readouterr().err` does not |
| `test_copy_return_value_unchanged` | Return value is still `True` when key was copied | `result is True` |
| `test_no_print_when_key_already_exists` | When target already exists, no print is called (silent skip) | `capfd.readouterr().out` does not contain the copy message |
| `test_no_print_when_no_source_key` | When no source key exists, no copy notification is printed | Only the warning message (if any) is in stdout |

**Implementation pattern** (extend `test_ollama_ssh_key.py`):

```python
def test_copy_prints_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
) -> None:
    """When source exists and target does not, print the copy confirmation."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    _write_key(home, ".ssh/id_ed25519", content="source-key-content")

    result = _ensure_ollama_ssh_key()

    assert result is True
    out, _ = capfd.readouterr()
    assert "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull." in out
```

### 2.2 SSH Key Missing Warning (`_ensure_ollama_ssh_key`)

**Test file**: `tests/test_ollama_ssh_key.py` (extend existing)

**Tests**:

| Test | Description | Assertion |
|------|-------------|-----------|
| `test_no_source_key_prints_warning` | When no source key exists, print the warning message | `capfd.readouterr().out` contains "Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519" |
| `test_warning_includes_fix_command` | The warning message includes the exact fix command | Message contains "cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519" |
| `test_no_source_key_return_value_unchanged` | Return value is still `False` when no source key | `result is False` |
| `test_no_source_key_no_dir_created` | No `~/.ollama/` directory is created when no source key | `(home / ".ollama").exists()` is `False` |

**Implementation pattern**:

```python
def test_no_source_key_prints_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
) -> None:
    """When no source key exists, print a warning with the fix command."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    result = _ensure_ollama_ssh_key()

    assert result is False
    out, _ = capfd.readouterr()
    assert "Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519" in out
    assert "cp ~/.ssh/id_ed25519 ~/.ollama/id_ed25519" in out
```

### 2.3 FTS Rebuild Failure Warning (`rebuild_fts_index`)

**Test file**: `tests/test_reembed.py` (extend existing FTS tests)

**Tests**:

| Test | Description | Assertion |
|------|-------------|-----------|
| `test_rebuild_fts_warns_on_final_failure` | When all retries exhausted, logging.warning() is called (not raise) | `caplog.at_level(logging.WARNING)` captures the DuckDB bug explanation; no exception raised |
| `test_rebuild_fts_warning_explains_upstream_bug` | The warning message mentions DuckDB 1.5.2 | `caplog.text` contains "DuckDB 1.5.2" |
| `test_rebuild_fts_warning_explains_not_agentalloy_issue` | The warning states this is not an agentalloy issue | `caplog.text` contains "NOT an agentalloy issue" or similar |
| `test_rebuild_fts_warning_includes_retry_command` | The warning includes how to retry | `caplog.text` contains "agentalloy reembed --rebuild-fts" |
| `test_rebuild_fts_returns_none_on_failure` | Function returns None (not raise) on final failure | No exception; function completes normally |
| `test_rebuild_fts_non_transient_still_raises` | Non-transient errors (e.g., FTS extension not loaded) still raise | `pytest.raises(Exception)` for non-stopwords errors |

**Implementation pattern**:

```python
def test_rebuild_fts_warns_on_final_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When all retries exhausted, log a warning instead of raising."""
    import duckdb

    db_path = tmp_path / "fts_warn.duck"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fragment_embeddings (
            fragment_id VARCHAR PRIMARY KEY,
            embedding FLOAT[1024] NOT NULL,
            skill_id VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            fragment_type VARCHAR NOT NULL,
            embedded_at BIGINT NOT NULL,
            embedding_model VARCHAR NOT NULL,
            prose VARCHAR NOT NULL DEFAULT ''
        );
    """)
    conn.close()

    conn = duckdb.connect(str(db_path))
    conn.execute("INSTALL fts; LOAD fts;")
    conn.execute("""
        INSERT INTO fragment_embeddings
        VALUES ('frag-0',
            (SELECT array_agg(0.0)::float[1024] FROM generate_series(1, 1024)),
            's', 'e', 't', 0, 'm', 'test prose');
    """)
    conn.close()

    conn = duckdb.connect(str(db_path))
    conn.execute("INSTALL fts; LOAD fts;")

    def mock_always_fail(*args, **kwargs):
        raise Exception("subject 'stopwords' has been deleted")

    conn.execute = mock_always_fail

    vs = VectorStore(conn)  # type: ignore[arg-type]

    with (
        caplog.at_level(logging.WARNING),
        patch("time.sleep"):
        # All retries exhausted — should NOT raise, should log warning
        vs.rebuild_fts_index()

    assert "stopwords" in caplog.text.lower()
    assert "duckdb" in caplog.text.lower()
    assert "agentalloy" in caplog.text.lower()
```

### 2.4 Doctor FTS Status Check (`_check_fts_status`)

**Test file**: `tests/install/test_doctor.py` (extend existing)

**Tests**:

| Test | Description | Assertion |
|------|-------------|-----------|
| `test_fts_status_ok` | When FTS query succeeds, returns `[OK]` with detail | `result["passed"] is True`, `result["name"] == "fts_status"` |
| `test_fts_status_warn_stopwords` | When FTS fails with stopwords error, returns `[WARN]` | `result["passed"] is False`, `result["error"]` contains "stopwords" |
| `test_fts_status_skips_no_db` | When vector store file doesn't exist, returns OK with skip detail | `result["passed"] is True`, `result["detail"]` contains "does not exist" |
| `test_fts_status_includes_in_doctor_result` | When `run_doctor()` runs, the FTS check appears in the checks list | `result["checks"]` contains a check with `name == "fts_status"` |
| `test_fts_status_total_checks_13` | Doctor now runs 13 checks (was 12) | `len(result["checks"])` includes the new check |
| `test_fts_status_other_error` | When FTS fails with a non-stopwords error, returns `[FAIL]` | `result["passed"] is False`, `result["error"]` contains the actual error |

**Implementation pattern**:

```python
class TestFtsStatus:
    def test_fts_status_skips_no_db_file(self, repo_root: Path) -> None:
        """When the vector store file doesn't exist, skip gracefully."""
        from agentalloy.install.subcommands.doctor import _check_fts_status

        # No DUCKDB_PATH set — should skip
        result = _check_fts_status()
        assert result["name"] == "fts_status"
        assert result["passed"] is True
        assert "skipped" in result["detail"].lower()

    def test_fts_status_in_doctor_result(self, repo_root: Path) -> None:
        """run_doctor includes the FTS status check."""
        _minimal_state(repo_root)
        from urllib.error import URLError

        with (
            patch("agentalloy.install.subcommands.verify.urlopen", side_effect=URLError("no network")),
            patch("agentalloy.install.subcommands.doctor.urlopen", side_effect=URLError("no network")),
            patch("agentalloy.install.subcommands.doctor._check_fts_status") as mock_fts,
        ):
            mock_fts.return_value = {
                "name": "fts_status",
                "passed": True,
                "duration_ms": 0,
                "detail": "FTS check OK",
            }
            result = run_doctor(root=repo_root)

        names = [c["name"] for c in result["checks"]]
        assert "fts_status" in names
```

### 2.5 README Note

**Test file**: `tests/test_readme.py` (new file) or inline in an existing test module

**Tests**:

| Test | Description | Assertion |
|------|-------------|-----------|
| `test_readme_has_linux_ssh_key_note` | README contains the Linux SSH key note | `readme_text` contains "Linux users" and "id_ed25519" and "cp ~/.ssh/id_ed25519" |
| `test_linux_note_after_windows_note` | The Linux note appears after the Windows unsupported note | `readme_text.index("Linux users") > readme_text.index("Windows is not currently supported")` |
| `test_linux_note_in_quickstart` | The Linux note is in the Quickstart section | `readme_text` section between `## Quickstart` and `## Demo` contains the note |

**Implementation pattern**:

```python
def test_readme_has_linux_ssh_key_note() -> None:
    """README Quickstart section documents the Linux SSH key requirement."""
    readme = Path("README.md").read_text()
    assert "Linux users" in readme
    assert "id_ed25519" in readme
    assert "cp ~/.ssh/id_ed25519" in readme
    assert "chmod 600" in readme

def test_linux_note_after_windows_note() -> None:
    """The Linux note appears immediately after the Windows unsupported note."""
    readme = Path("README.md").read_text()
    windows_pos = readme.index("Windows is not currently supported")
    linux_pos = readme.index("Linux users")
    assert linux_pos > windows_pos, "Linux note should appear after the Windows note"
```

---

## 3. Integration Tests

### 3.1 End-to-End: `agentalloy setup` with existing SSH key

**Scenario**: Run `agentalloy setup -n --runner ollama ...` on a clean native install with an existing `~/.ssh/id_ed25519` but no `~/.ollama/` directory.

**Verification**:
- stdout contains "Copied SSH key from ~/.ssh/id_ed25519 to ~/.ollama/id_ed25519 for Ollama model pull."
- `~/.ollama/id_ed25519` exists with correct permissions (0600)
- `ollama pull` proceeds normally

### 3.2 End-to-End: `agentalloy setup` with no SSH key

**Scenario**: Run `agentalloy setup -n --runner ollama ...` on a system with no SSH key pair.

**Verification**:
- stdout contains "Ollama SSH key not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519"
- `ollama pull` is still attempted (behavior unchanged — user was warned)
- The warning includes the fix command

### 3.3 End-to-End: `agentalloy reembed --rebuild-fts` with DuckDB 1.5.2 bug

**Scenario**: Run `agentalloy reembed --rebuild-fts` on a system with the DuckDB 1.5.2 stopwords bug.

**Verification**:
- No traceback is shown
- Warning message appears in logs/stderr explaining the DuckDB bug
- Exit code is 0 (success — the function treats this as non-fatal)
- `agentalloy doctor` shows FTS status as [WARN]

### 3.4 End-to-End: `agentalloy doctor` with FTS working

**Scenario**: Run `agentalloy doctor` on a system where FTS is functional.

**Verification**:
- A new check (13) appears in the output
- Check name is "fts_status"
- Status is [OK]
- Detail includes result count

### 3.5 End-to-End: `agentalloy doctor` with FTS corrupted

**Scenario**: Run `agentalloy doctor` on a system with the DuckDB 1.5.2 stopwords bug.

**Verification**:
- Check 13 appears with name "fts_status"
- Status is [WARN] (not [FAIL])
- Error message mentions "stopwords" and "DuckDB"
- Remediation includes "agentalloy reembed --rebuild-fts"

---

## 4. Edge Cases

### 4.1 Container Install — SSH Key Copy

**Scenario**: Container install where `~/.ollama` is mounted from the host.

**Expected**: The `_ensure_ollama_ssh_key()` function returns `False` (target already exists) and prints nothing. No interference with container setup.

**Test**: Mock `target.exists()` to return `True` and verify no stdout output.

### 4.2 FTS Index Not Yet Built

**Scenario**: Vector store file exists but FTS index hasn't been built yet.

**Expected**: The doctor check should handle this gracefully. The `search_bm25("test")` call in `VectorStore.search_bm25()` already catches exceptions and returns `[]`, so the doctor check should report [OK] with an empty result count.

### 4.3 Vector Store File Missing

**Scenario**: Doctor runs on a fresh install where the vector store file doesn't exist yet.

**Expected**: The check should skip gracefully with `[OK]` and a detail message like "Vector store file does not exist — FTS check skipped".

### 4.4 Non-Stopwords FTS Error

**Scenario**: FTS check encounters an error that is NOT the stopwords bug (e.g., corrupted index, permission error).

**Expected**: The doctor check returns `[FAIL]` with the actual error message and a generic remediation.

### 4.5 DuckDB Version < 1.5.2

**Scenario**: System uses a DuckDB version that does NOT have the stopwords bug.

**Expected**: FTS rebuild succeeds normally. No warning is logged. Doctor check reports [OK].

### 4.6 Multiple SSH Key Locations

**Scenario**: User has SSH key at `~/.ssh/id_rsa` but NOT at `~/.ssh/id_ed25519`.

**Expected**: The function checks specifically for `id_ed25519`. The warning message says "not found at ~/.ssh/id_ed25519 or ~/.ollama/id_ed25519" — accurate for this case.

### 4.7 Concurrent Doctor Runs

**Scenario**: Multiple `agentalloy doctor` processes run simultaneously.

**Expected**: Each opens a separate DuckDB connection. DuckDB allows multiple reader processes. No lock contention expected for read-only FTS queries.

---

## 5. Regression Tests (Ensure Existing Tests Still Pass)

| Test File | Tests to Verify |
|-----------|----------------|
| `tests/test_ollama_ssh_key.py` | All existing tests pass (no_source_key_skips_cleanly, target_already_exists_skips, copy_creates_dir_and_sets_perms, copy_preserves_content) |
| `tests/test_vector_store.py` | All existing tests pass including FTS rebuild tests (retry on stopwords, no retry on non-transient, catalog reset) |
| `tests/test_reembed.py` | All existing tests pass including rebuild_fts_index integration tests |
| `tests/install/test_doctor.py` | All existing tests pass including test_returns_all_checks, test_output_shape |

---

## 6. Test Execution Strategy

### 6.1 Unit Tests (Fast, Run on Every Commit)

```bash
pytest tests/test_ollama_ssh_key.py tests/test_vector_store.py tests/install/test_doctor.py -v
```

### 6.2 Integration Tests (Slower, Run in CI)

```bash
pytest tests/test_reembed.py -v
```

### 6.3 Manual Verification (Pre-Release)

1. Clean native install with existing SSH key → verify copy message
2. Clean native install with no SSH key → verify warning message
3. `agentalloy reembed --rebuild-fts` with DuckDB 1.5.2 → verify warning (no traceback)
4. `agentalloy doctor` → verify check 13 appears
5. Read README.md → verify Linux SSH key note in Quickstart

---

## 7. Test Data Requirements

- **SSH key tests**: Need a temporary directory with a fake SSH key file (0600 permissions). Use `tmp_path` fixture from pytest.
- **FTS tests**: Need a real DuckDB database file with the fragment_embeddings schema and FTS extension loaded. Use `tmp_path` fixture.
- **Doctor tests**: Need a minimal install state with `.env` file containing `DUCKDB_PATH`. Use the existing `repo_root` and `_minimal_state` fixtures.
- **README test**: No special data needed — just read `README.md`.

---

## 8. Test Coverage Goals

| Module | Target Coverage | Notes |
|--------|----------------|-------|
| `pull_models.py` (SSH key section) | 100% | New print() branches must be covered |
| `vector_store.py` (rebuild_fts_index) | 95%+ | Existing retry logic already tested; add failure-path test |
| `doctor.py` (_check_fts_status) | 100% | New function, all branches covered |
| `README.md` | N/A | Documentation, verified by assertion test |
