# Implementation Plan: GHCR Migration

**Source Design:** `design/container-ghcr-migration.md` (v1.1)
**Source Spec:** `specs/container-ghcr-migration.md`
**Created:** 2026-06-07
**Status:** Ready for delegation

---

## Overview

This plan breaks the GHCR migration into 10 discrete, sequential tasks. Each task is self-contained and can be executed by a build subagent. Tasks must be executed in order due to dependencies.

### Key Findings from Code Audit

- `_build_image()` does NOT exist in `container_runtime.py` — the build path at `simple_setup.py:1182-1202` is dead code that would raise `NameError` at runtime.
- `compose_path` is referenced at `simple_setup.py:1184` but is NOT defined anywhere in the source — another dead code artifact.
- `_locate_build_context()`, `_has_assets()` do NOT exist in `container_runtime.py` — the test class `TestLocateBuildContext` tests non-existent functions and will fail with ImportError.
- `_DEFAULT_IMAGE` is already set to `"ghcr.io/nrmeyers/agentalloy:latest"` at `container_runtime.py:54`.
- `_pull_image()` already supports both online pull and offline load paths.
- `SetupConfig.image_tag` defaults to `"agentalloy:local"` at `simple_setup.py:82` — needs updating to GHCR URL for state recording.
- The pull path at `simple_setup.py:1024-1075` already works correctly and returns 0 on success — the readiness code at lines 1204+ is unreachable from the pull path because the pull path returns at line 1075.
- `preflight.py` has `_check_build_context()` (lines 555-608) and `_check_image_build_deps()` (lines 705-746) that need removal.
- `preflight.py` `run_preflight()` at line 766 accepts `build_context` parameter that needs removal.
- `preflight.py` `add_parser()` at lines 902-906 has `--build-context` argument that needs removal.

### File Inventory

| File | Lines | Description |
|---|---|---|
| `src/agentalloy/install/subcommands/container_runtime.py` | 624 | Pull timeout, offline verification |
| `src/agentalloy/install/subcommands/simple_setup.py` | 1904 | Remove dead build path, update image_tag default |
| `src/agentalloy/install/subcommands/preflight.py` | 942 | Remove build-context checks, add GHCR reachability |
| `tests/install/test_container_runtime.py` | 325 | Rename TestBuildImage, remove TestLocateBuildContext, add new tests |
| `tests/test_container_e2e.py` | 711 | Update build references to pull |
| `tests/test_simple_setup.py` | 2292 | Update TestContainerFlow assertions |

---

## Task 1: Increase Pull Timeout and Add Remediation Messages

**Requirement:** REQ-2 (AC-2.3, AC-2.4)
**Effort:** Small
**Risk:** Low

### Description

Update `_pull_image()` in `container_runtime.py` to increase the online pull timeout from 300s to 600s and add remediation messages for pull failures.

### Files and Line Numbers

- `src/agentalloy/install/subcommands/container_runtime.py`
  - Line 113: `timeout=300` -> `timeout=600`
  - Line 118: Add remediation text after pull failure message
  - Line 121: Update timeout message from "300s" to "600s" and add remediation text

### Step-by-Step Instructions

1. **Change online pull timeout (line 113):**
   - Find: `timeout=300,`
   - Replace with: `timeout=600,`

2. **Add remediation text to CalledProcessError handler (line 118):**
   - Find: `_print(f"  [red]Failed to pull image (exit {exc.returncode})[/red]")`
   - Replace with:
     ```python
     _print(f"  [red]Failed to pull image (exit {exc.returncode})[/red]")
     _print("  [dim]Remediation: Check network connectivity to ghcr.io, "
            "or use --image-path for offline mode.[/dim]")
     ```

3. **Update TimeoutExpired message (line 121):**
   - Find: `timeout=300,` and `timed out after 300s`
   - Replace timeout value with `600` and message with `"Image pull timed out after 600s"`
   - Add remediation text after the timeout message:
     ```python
     _print("  [dim]Remediation: Check network connectivity, "
            "or use --image-path for offline mode.[/dim]")
     ```

### Verification

```bash
# Run unit tests for _pull_image
cd /home/nmeyers/dev/agentalloy
python -m pytest tests/install/test_container_runtime.py::TestBuildImage -v

# Verify the timeout is 600s in the source
grep -n "timeout=600" src/agentalloy/install/subcommands/container_runtime.py
grep -n "600s" src/agentalloy/install/subcommands/container_runtime.py
```

### Rollback

Revert the single file: `timeout=300` and remove the two `_print` remediation lines.

---

## Task 2: Add Post-Load Verification for Offline Mode

**Requirement:** REQ-3 (AC-3.1-3.5)
**Effort:** Small
**Risk:** Low

### Description

After `podman load` in offline mode, verify the expected image tag is present. Handle the edge case where the tarball contains a different image tag.

### Files and Line Numbers

- `src/agentalloy/install/subcommands/container_runtime.py`
  - Lines 98-99: After successful load, before return 0

### Step-by-Step Instructions

1. **Add post-load verification after line 99 (`_print("  [green]-> Image loaded from tarball[/green]")`):**
   - Insert after the success print, before `return 0`:
     ```python
     # Verify the image is present after load (handles tarball tag mismatch)
     result = subprocess.run(
         [runtime, "images", "--format", "{{.Repository}}:{{.Tag}}"],
         capture_output=True,
         text=True,
         timeout=10,
     )
     # Also check by ID for digest-based matching
     id_result = subprocess.run(
         [runtime, "images", "--format", "{{.ID}}"],
         capture_output=True,
         text=True,
         timeout=10,
     )
     if not (image in result.stdout or (id_result.returncode == 0 and id_result.stdout.strip())):
         _print(f"  [red]Image {image} not found after load[/red]")
         return 1
     ```

### Verification

```bash
# Run offline mode tests
python -m pytest tests/install/test_container_runtime.py -v -k "offline"

# Manual test with a real tarball (if available)
# podman save ghcr.io/nrmeyers/agentalloy:latest > /tmp/test.tar
# python -c "from agentalloy.install.subcommands.container_runtime import _pull_image; print(_pull_image('podman', offline=True, tarball_path='/tmp/test.tar'))"
```

### Rollback

Remove the post-load verification block (6 lines) inserted after line 99.

---

## Task 3: Remove Dead Build Path from _run_container_flow()

**Requirement:** REQ-6 (AC-6.6), REQ-4 (AC-4.7)
**Effort:** Medium
**Risk:** High

### Description

Remove the dead build path at `simple_setup.py:1182-1202` that references undefined `compose_path` and `_build_image()`. This block is unreachable from the pull path (which returns at line 1075), but it must be removed to prevent confusion and potential code review issues.

The readiness code at lines 1204+ is also unreachable from the pull path. After removing the build path, the readiness code (lines 1204-1299) must be connected to the pull path.

### Files and Line Numbers

- `src/agentalloy/install/subcommands/simple_setup.py`
  - Lines 1076-1181: Additional config setup (port, mode, harness, deployment, pack selection, summary, stale container check) — this code is unreachable from the pull path because the pull path returns at line 1075. This entire block needs to be moved to connect to the pull path.
  - Lines 1182-1202: **DEAD BUILD PATH** — remove entirely
  - Lines 1204-1299: Readiness, state recording, env writing — needs to connect to pull path

### Critical Analysis

Looking at the code flow:
- Lines 1024-1038: Pull/load image — returns at line 1038 on failure, continues at line 1040
- Lines 1040-1075: Volume, entrypoint, run, readiness, cleanup — returns 0 at line 1075
- Lines 1076-1181: Config setup, pack selection, summary, stale container check — **UNREACHABLE** from pull path
- Lines 1182-1202: Dead build path — **UNREACHABLE** (NameError on `compose_path`)
- Lines 1204-1299: Readiness with pack counting, state recording, env writing — **UNREACHABLE** from pull path

The function has TWO separate readiness flows:
1. Lines 1058-1075: Simple readiness (no pack counting, no state recording) — used by pull path
2. Lines 1204-1299: Full readiness (with pack counting, state recording, env writing) — used by build path

After removing the build path, the full readiness flow (lines 1204-1299) should replace the simple one. The config setup (lines 1076-1181) needs to be integrated.

### Step-by-Step Instructions

1. **Remove lines 1076-1202 entirely** (the unreachable config setup + dead build path):
   - Delete lines 1076-1202 (127 lines)
   - This removes: `cfg.port = 47950` through `_print("  [green]  Done.[/green]")`

2. **Replace the simple readiness block (lines 1058-1075) with the full readiness flow:**
   - Delete lines 1058-1075 (the simple `_wait_for_readiness` call and immediate return)
   - Insert the full readiness flow from the original lines 1204-1299:
     - Pack counting and timeout selection
     - `_on_progress` callback definition
     - Full `_wait_for_readiness` call
     - State recording (lines 1259-1279)
     - Env writing (lines 1281-1299)
     - Return 0

3. **Verify the new flow:**
   ```
   _run_container_flow():
     1. Preflight (early) -> return 1 on fatal
     2. Detect runtime -> return 1 on missing
     3. Pull/load image -> return on failure
     4. Ensure volume/ollama dir
     5. Generate entrypoint
     6. Run container -> cleanup on failure
     7. Full readiness (pack counting, progress, state recording, env)
     8. Return 0
   ```

### Verification

```bash
# Syntax check
python -m py_compile src/agentalloy/install/subcommands/simple_setup.py

# Unit tests for container flow
python -m pytest tests/test_container_e2e.py -v
python -m pytest tests/test_simple_setup.py::TestContainerFlow -v

# Verify no references to compose_path or _build_image remain
grep -n "compose_path\|_build_image" src/agentalloy/install/subcommands/simple_setup.py
# Should return nothing
```

### Rollback

Restore the original file from git:
```bash
git checkout HEAD -- src/agentalloy/install/subcommands/simple_setup.py
```

---

## Task 4: Update SetupConfig.image_tag Default

**Requirement:** REQ-2 (AC-2.5)
**Effort:** Small
**Risk:** Low

### Description

Update `SetupConfig.image_tag` default from `"agentalloy:local"` to `"ghcr.io/nrmeyers/agentalloy:latest"` for state recording. Keep `_DEFAULT_IMAGE` in `container_runtime.py` as the source of truth for actual pull/run operations.

### Files and Line Numbers

- `src/agentalloy/install/subcommands/simple_setup.py`
  - Line 82: `image_tag: str = "agentalloy:local"` -> `image_tag: str = "ghcr.io/nrmeyers/agentalloy:latest"`

### Step-by-Step Instructions

1. **Change line 82:**
   - Find: `image_tag: str = "agentalloy:local"  # container image tag`
   - Replace with: `image_tag: str = "ghcr.io/nrmeyers/agentalloy:latest"  # container image tag for GHCR`

### Verification

```bash
# Verify the default value
python -c "from agentalloy.install.subcommands.simple_setup import SetupConfig; print(SetupConfig().image_tag)"
# Expected: ghcr.io/nrmeyers/agentalloy:latest

# Run tests that check image_tag
python -m pytest tests/test_simple_setup.py::TestContainerFlow -v -k "image_tag"
```

### Rollback

Revert line 82 to `image_tag: str = "agentalloy:local"`.

---

## Task 5: Remove Build-Context Preflight Checks

**Requirement:** REQ-7 (AC-7.4)
**Effort:** Medium
**Risk:** Medium

### Description

Remove `_check_build_context()` and `_check_image_build_deps()` from `preflight.py`, remove their calls from `run_preflight()`, and remove the `--build-context` CLI argument from `add_parser()`.

### Files and Line Numbers

- `src/agentalloy/install/subcommands/preflight.py`
  - Lines 555-608: `_check_build_context()` function — **DELETE**
  - Lines 705-746: `_check_image_build_deps()` function — **DELETE**
  - Line 797: `checks.append(_check_build_context(build_context))` — **DELETE**
  - Line 801: `checks.append(_check_image_build_deps(build_context))` — **DELETE**
  - Line 771: `build_context: str | None = None` parameter in `run_preflight()` — **REMOVE**
  - Lines 902-906: `--build-context` argument in `add_parser()` — **REMOVE**
  - Line 932: `build_context=getattr(args, "build_context", None)` in `_run()` — **REMOVE**

### Step-by-Step Instructions

1. **Delete `_check_build_context()` function (lines 555-608):**
   - Delete all 54 lines of this function

2. **Delete `_check_image_build_deps()` function (lines 705-746):**
   - After deleting `_check_build_context`, `_check_image_build_deps` shifts up. Delete all 42 lines.

3. **Remove calls from `run_preflight()` container phase (lines 797, 801):**
   - Delete: `checks.append(_check_build_context(build_context))`
   - Delete: `checks.append(_check_image_build_deps(build_context))`

4. **Remove `build_context` parameter from `run_preflight()` signature (line 771):**
   - Find: `build_context: str | None = None,`
   - Delete this line

5. **Remove `--build-context` argument from `add_parser()` (lines 902-906):**
   - Delete the entire `p.add_argument("--build-context", ...)` block

6. **Remove `build_context` from `_run()` function call (line 932):**
   - Find: `build_context=getattr(args, "build_context", None),`
   - Delete this line

### Verification

```bash
# Syntax check
python -m py_compile src/agentalloy/install/subcommands/preflight.py

# Verify no references to build_context checks remain
grep -n "_check_build_context\|_check_image_build_deps" src/agentalloy/install/subcommands/preflight.py
# Should return nothing

# Verify --build-context argument removed
grep -n "build-context" src/agentalloy/install/subcommands/preflight.py
# Should return nothing
```

### Rollback

Restore from git:
```bash
git checkout HEAD -- src/agentalloy/install/subcommands/preflight.py
```

---

## Task 6: Add GHCR Reachability and Disk Space Preflight Checks

**Requirement:** REQ-7 (AC-7.3, AC-7.5)
**Effort:** Medium
**Risk:** Low

### Description

Add two new preflight checks for the container phase:
1. `_check_ghcr_reachable()` — checks network connectivity to ghcr.io (severity="warn", non-fatal)
2. `_check_disk_space()` — checks available disk space for image pull (severity="fatal" if <2GB)

### Files and Line Numbers

- `src/agentalloy/install/subcommands/preflight.py`
  - After line 553 (end of `_check_runtime_binary`): Insert `_check_ghcr_reachable()` function
  - After line 553 (end of `_check_runtime_binary`): Insert `_check_disk_space()` function
  - Lines 795-796 (container phase, after `_check_runtime_binary` and `_check_git_present`): Add the two new checks

### Step-by-Step Instructions

1. **Add `_check_ghcr_reachable()` function after `_check_runtime_binary()`:**
   ```python
   def _check_ghcr_reachable() -> dict[str, Any]:
       """Check network connectivity to ghcr.io (for image pulls)."""
       t0 = time.monotonic()
       try:
           req = urllib.request.Request("https://ghcr.io", method="HEAD")
           with urllib.request.urlopen(req, timeout=5) as resp:
               status = getattr(resp, "status", 200)
       except (urllib.error.URLError, OSError) as exc:
           return _check(
               "ghcr_reachable",
               passed=False,
               started=t0,
               severity="warn",
               error=f"Cannot reach ghcr.io: {exc}",
               remediation=(
                   "Container image pull will fail without network access to ghcr.io. "
                   "Use --image-path to deploy from a local tarball instead."
               ),
           )
       return _check(
           "ghcr_reachable",
           passed=True,
           started=t0,
           detail=f"ghcr.io reachable (HTTP {status})",
       )
   ```

2. **Add `_check_disk_space()` function after `_check_ghcr_reachable()`:**
   ```python
   def _check_disk_space(min_bytes: int = 2 * 1024**3) -> dict[str, Any]:
       """Check available disk space before pulling/loading an image."""
       t0 = time.monotonic()
       paths_to_check = [
           os.environ.get("CONTAINER_STORAGE", ""),
       ]
       # Will be populated with runtime-specific paths at call time
       available = 0
       for path in paths_to_check:
           if path and os.path.exists(path):
               available = shutil.disk_usage(path).free
               break
       # If no configured path worked, try common locations
       if available == 0:
           for fallback in [
               os.path.expanduser("~/.local/share/containers"),
               "/var/lib/containers",
               "/var/lib/docker",
           ]:
               if os.path.exists(fallback):
                   available = shutil.disk_usage(fallback).free
                   break
       if available == 0:
           return _check(
               "disk_space",
               passed=False,
               started=t0,
               severity="warn",
               error="Cannot determine container storage location",
               remediation="Ensure container runtime is properly configured.",
           )
       if available < min_bytes:
           return _check(
               "disk_space",
               passed=False,
               started=t0,
               severity="fatal",
               error=f"Insufficient disk space: {available // (1024**2)}MB available, "
                     f"{min_bytes // (1024**2)}MB required",
               remediation="Free disk space or use --image-path with a smaller tarball.",
           )
       return _check(
           "disk_space",
           passed=True,
           started=t0,
           detail=f"{available // (1024**3)}GB available",
       )
   ```

3. **Add checks to container phase in `run_preflight()`:**
   - After `checks.append(_check_git_present())`, insert:
     ```python
     checks.append(_check_ghcr_reachable())
     checks.append(_check_disk_space())
     ```

### Verification

```bash
# Syntax check
python -m py_compile src/agentalloy/install/subcommands/preflight.py

# Verify new functions exist
grep -n "def _check_ghcr_reachable\|def _check_disk_space" src/agentalloy/install/subcommands/preflight.py
# Should return both function definitions

# Run preflight module
python -c "from agentalloy.install.subcommands.preflight import run_preflight; print(run_preflight(phase='container'))"
```

### Rollback

Remove the two new functions and their calls.

---

## Task 7: Rename TestBuildImage to TestPullImage and Update Assertions

**Requirement:** REQ-5 (AC-5.1)
**Effort:** Medium
**Risk:** Medium

### Description

Rename `TestBuildImage` to `TestPullImage` in `tests/install/test_container_runtime.py` and update all assertions from `podman build` to `podman pull`. The tests currently assert build commands that no longer exist.

### Files and Line Numbers

- `tests/install/test_container_runtime.py`
  - Line 206: `class TestBuildImage:` -> `class TestPullImage:`
  - Line 207: Update docstring
  - Line 210: Update docstring
  - Lines 209-231: `test_constructs_correct_command` — change `podman build` to `podman pull`
  - Lines 233-245: `test_uses_correct_image_tag` — update assertions
  - Lines 247-259: `test_uses_correct_dockerfile` — remove dockerfile assertions (pull has no -f flag)
  - Lines 261-268: `test_returns_zero_on_success` — update docstring
  - Lines 270-282: `test_returns_nonzero_on_failure` — update error type
  - Lines 284-304: `test_writes_log_on_failure` — this test checks for a log file that doesn't exist in pull mode. **DELETE** this test.
  - Lines 306-316: `test_has_600s_timeout` — update docstring, timeout stays 600
  - Lines 318-325: `test_returns_nonzero_on_timeout` — update TimeoutExpired message

### Step-by-Step Instructions

1. **Rename class (line 206):**
   - Find: `class TestBuildImage:`
   - Replace with: `class TestPullImage:`

2. **Update docstring (lines 207-210):**
   - Find: `UT-3: _pull_image() constructs correct command`
   - Update to: `UT-3: _pull_image() pulls from GHCR using podman pull`

3. **Update `test_constructs_correct_command` (lines 209-231):**
   - Remove `context` parameter from test signature
   - Change `container_runtime._pull_image("podman", context)` to `container_runtime._pull_image("podman")`
   - Change expected command from `["podman", "build", "-t", "agentalloy:local", "-f", "Containerfile", str(context)]` to `["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]`
   - Remove `capture_output=True` from assert
   - Remove `-t agentalloy:local` and `-f Containerfile` and `context` from expected args

4. **Update `test_uses_correct_image_tag` (lines 233-245):**
   - Remove `context` parameter
   - Change `_pull_image("docker", context)` to `_pull_image("docker")`
   - Update assertion: verify command is `["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]` (or with docker runtime)
   - Remove `-t` tag assertions

5. **Delete `test_uses_correct_dockerfile` (lines 247-259):**
   - This test checks for `-f Containerfile` which doesn't exist in pull mode. Delete the entire test method.

6. **Update `test_returns_zero_on_success` (lines 261-268):**
   - Remove `context` parameter
   - Change `_pull_image("podman", context)` to `_pull_image("podman")`
   - Update docstring

7. **Update `test_returns_nonzero_on_failure` (lines 270-282):**
   - Remove `context` parameter
   - Change `CalledProcessError` to use `["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]`
   - Update docstring

8. **Delete `test_writes_log_on_failure` (lines 284-304):**
   - This test checks for `agentalloy-build.log` which doesn't exist in pull mode. Delete the entire test method.

9. **Update `test_has_600s_timeout` (lines 306-316):**
   - Remove `context` parameter
   - Change `_pull_image("podman", context)` to `_pull_image("podman")`
   - Update docstring
   - Timeout stays 600 (already correct)

10. **Update `test_returns_nonzero_on_timeout` (lines 318-325):**
    - Change `TimeoutExpired("podman build", 600)` to `TimeoutExpired(["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"], 600)`
    - Update docstring

### Verification

```bash
# Run the renamed test class
python -m pytest tests/install/test_container_runtime.py::TestPullImage -v

# All tests in the file should pass
python -m pytest tests/install/test_container_runtime.py -v
```

### Rollback

Restore from git:
```bash
git checkout HEAD -- tests/install/test_container_runtime.py
```

---

## Task 8: Remove TestLocateBuildContext and Add New Test Classes

**Requirement:** REQ-5 (AC-5.6, AC-5.3, AC-5.4)
**Effort:** Medium
**Risk:** Medium

### Description

1. Remove `TestLocateBuildContext` class (lines 79-198) — tests non-existent `_locate_build_context()` function
2. Add `TestOfflineLoad` class — tests offline image loading via tarball
3. Add `TestPullImageFailureScenarios` class — tests online pull failure scenarios

### Files and Line Numbers

- `tests/install/test_container_runtime.py`
  - Lines 79-198: `TestLocateBuildContext` — **DELETE** (120 lines)
  - After `TestPullImage` class (after line 325, or wherever it ends): Add new test classes

### Step-by-Step Instructions

1. **Delete `TestLocateBuildContext` (lines 79-198):**
   - Delete the entire class including its docstring and all 6 test methods

2. **Add `TestOfflineLoad` class after `TestPullImage`:**
   ```python
   class TestOfflineLoad:
       """UT-4: Tests for offline image loading via --image-path flag."""

       def test_load_from_tarball(self, tmp_path: Path):
           """Offline mode loads image from tarball via podman load."""
           tarball = tmp_path / "image.tar"
           tarball.write_bytes(b"fake-tarball")
           with patch("subprocess.run") as mock_run:
               mock_run.return_value = MagicMock(returncode=0)
               result = container_runtime._pull_image(
                   "podman", offline=True, tarball_path=tarball
               )
               assert result == 0
               mock_run.assert_called_once_with(
                   ["podman", "load", "-i", str(tarball)],
                   check=True,
                   capture_output=True,
                   timeout=300,
               )

       def test_offline_missing_tarball(self, tmp_path: Path):
           """Returns 1 when tarball does not exist."""
           missing = tmp_path / "nonexistent.tar"
           result = container_runtime._pull_image(
               "podman", offline=True, tarball_path=missing
           )
           assert result == 1

       def test_offline_load_failure(self, tmp_path: Path):
           """Returns non-zero on podman load failure."""
           tarball = tmp_path / "image.tar"
           tarball.write_bytes(b"fake-tarball")
           exc = subprocess.CalledProcessError(1, ["podman", "load"])
           exc.stderr = b"invalid image format"
           with patch("subprocess.run", side_effect=exc):
               result = container_runtime._pull_image(
                   "podman", offline=True, tarball_path=tarball
               )
               assert result == 1

       def test_offline_timeout(self, tmp_path: Path):
           """Returns 1 on load timeout."""
           tarball = tmp_path / "image.tar"
           tarball.write_bytes(b"fake-tarball")
           with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["podman", "load"], 300)):
               result = container_runtime._pull_image(
                   "podman", offline=True, tarball_path=tarball
               )
               assert result == 1
   ```

3. **Add `TestPullImageFailureScenarios` class:**
   ```python
   class TestPullImageFailureScenarios:
       """UT-5: Tests for online image pull failure scenarios."""

       def test_network_timeout(self):
           """Returns 1 when pull times out."""
           with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"], 600)):
               result = container_runtime._pull_image("podman")
               assert result == 1

       def test_image_not_found(self):
           """Returns non-zero when image does not exist on GHCR."""
           exc = subprocess.CalledProcessError(125, ["podman", "pull"])
           exc.stderr = b"manifest unknown"
           with patch("subprocess.run", side_effect=exc):
               result = container_runtime._pull_image("podman")
               assert result == 125

       def test_custom_image_ref(self):
           """Uses custom image_ref when provided."""
           with patch("subprocess.run") as mock_run:
               mock_run.return_value = MagicMock(returncode=0)
               container_runtime._pull_image("podman", image_ref="ghcr.io/nrmeyers/agentalloy@sha256:abc123")
               cmd = mock_run.call_args[0][0]
               assert cmd == ["podman", "pull", "ghcr.io/nrmeyers/agentalloy@sha256:abc123"]

       def test_default_image_is_ghcr(self):
           """Default image is ghcr.io/nrmeyers/agentalloy:latest."""
           with patch("subprocess.run") as mock_run:
               mock_run.return_value = MagicMock(returncode=0)
               container_runtime._pull_image("podman")
               cmd = mock_run.call_args[0][0]
               assert cmd == ["podman", "pull", "ghcr.io/nrmeyers/agentalloy:latest"]
   ```

### Verification

```bash
# Run all tests in the file
python -m pytest tests/install/test_container_runtime.py -v

# Verify no references to _locate_build_context remain
grep -n "_locate_build_context" tests/install/test_container_runtime.py
# Should return nothing
```

### Rollback

Restore from git:
```bash
git checkout HEAD -- tests/install/test_container_runtime.py
```

---

## Task 9: Update E2E Tests

**Requirement:** REQ-5 (AC-5.2)
**Effort:** Medium
**Risk:** Medium

### Description

Update `tests/test_container_e2e.py` to reflect pull-only behavior:
1. Remove compose.yaml/Containerfile setup from tests that no longer need it
2. Update `TestFullContainerSetup` tests to not require build context
3. Update `TestCrashRecovery.test_build_failure_aborts_setup` — rename to `test_pull_failure_aborts_setup`
4. Update `_make_compose_file()` and `_make_containerfile()` helpers — remove or deprecate

### Files and Line Numbers

- `tests/test_container_e2e.py`
  - Lines 29-40: `_make_compose_file()` — **DELETE**
  - Lines 43-47: `_make_containerfile()` — **DELETE**
  - Lines 288-289: `_make_compose_file(tmp_path)` and `_make_containerfile(tmp_path)` in TestFullContainerSetup — **DELETE**
  - Lines 307-310: `_make_compose_file()` and `_make_containerfile()` calls — **DELETE**
  - Lines 376-378: `_make_compose_file()` and `_make_containerfile()` calls — **DELETE**
  - Lines 401-403: `_make_compose_file()` and `_make_containerfile()` calls — **DELETE**
  - Lines 443-445: `_make_compose_file()` and `_make_containerfile()` calls — **DELETE**
  - Lines 615-618: `TestCrashRecovery.test_build_failure_aborts_setup` — rename to `test_pull_failure_aborts_setup`, remove compose/containerfile setup
  - Lines 629-632: `TestCrashRecovery.test_container_start_failure_aborts_setup` — remove compose/containerfile setup
  - Lines 643-646: `TestCrashRecovery.test_health_check_timeout_shows_warning` — remove compose/containerfile setup
  - Lines 677-680: `TestCrashRecovery.test_preflight_failure_aborts_before_subprocess_calls` — remove compose/containerfile setup

### Step-by-Step Instructions

1. **Delete helper functions:**
   - Delete `_make_compose_file()` (lines 29-40)
   - Delete `_make_containerfile()` (lines 43-47)

2. **Remove compose/containerfile setup from all tests:**
   - In each test method that calls `_make_compose_file(tmp_path)` and `_make_containerfile(tmp_path)`, delete those two lines
   - Tests affected:
     - `test_full_setup_returns_zero` (lines 288-289)
     - `test_full_setup_calls_container_runtime_in_correct_order` (lines 307-308)
     - `test_full_setup_records_state_on_success` (lines 376-377)
     - `test_full_setup_skips_native_prompts_in_non_interactive_mode` (lines 401-402)
     - `test_entrypoint_is_generated_with_packs` (lines 443-444)
     - `test_build_failure_aborts_setup` (lines 615-616)
     - `test_container_start_failure_aborts_setup` (lines 629-630)
     - `test_health_check_timeout_shows_warning` (lines 643-644)
     - `test_preflight_failure_aborts_before_subprocess_calls` (lines 677-678)

3. **Rename `test_build_failure_aborts_setup` to `test_pull_failure_aborts_setup`:**
   - Find: `def test_build_failure_aborts_setup(self):`
   - Replace with: `def test_pull_failure_aborts_setup(self):`
   - Update docstring: "When the image pull fails, setup exits with code 1."

4. **Update `TestModelPullBootstrap` tests:**
   - These tests already check entrypoint content, not build commands. No changes needed.

### Verification

```bash
# Run all E2E tests
python -m pytest tests/test_container_e2e.py -v

# Verify no references to compose.yaml or Containerfile remain in test helpers
grep -n "_make_compose_file\|_make_containerfile\|compose.yaml\|Containerfile" tests/test_container_e2e.py
# Should return nothing (except in entrypoint script content checks)
```

### Rollback

Restore from git:
```bash
git checkout HEAD -- tests/test_container_e2e.py
```

---

## Task 10: Update TestContainerFlow in test_simple_setup.py

**Requirement:** REQ-5 (AC-5.5)
**Effort:** Medium
**Risk:** Medium

### Description

Update `TestContainerFlow` class in `tests/test_simple_setup.py` to reflect pull-only behavior. Key changes:
1. Update `test_image_tag_path_absolute` — verify `image_tag` is now the GHCR URL
2. Update `test_image_tag_resolved_from_repo_root_not_cwd` — verify GHCR URL default
3. Update `test_image_tag_resolved_from_cwd_when_present` — verify GHCR URL default (cwd no longer matters for image tag)

### Files and Line Numbers

- `tests/test_simple_setup.py`
  - Line 1128: `assert st["image_tag"] == "agentalloy:local"` -> `assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"`
  - Line 1168: `assert st["image_tag"] == "agentalloy:local"` -> `assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"`
  - Lines 1170-1200: `test_image_tag_resolved_from_cwd_when_present` — update assertions

### Step-by-Step Instructions

1. **Update `test_image_tag_path_absolute` assertion (line 1128):**
   - Find: `assert st["image_tag"] == "agentalloy:local"`
   - Replace with: `assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"`

2. **Update `test_image_tag_resolved_from_repo_root_not_cwd` assertion (line 1168):**
   - Find: `assert st["image_tag"] == "agentalloy:local"`
   - Replace with: `assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"`

3. **Update `test_image_tag_resolved_from_cwd_when_present` (lines 1170-1200):**
   - This test creates compose.yaml and Containerfile in tmp_path, then checks that cwd assets win over parents[4]. Since the image tag is now always the GHCR URL regardless of cwd, update the test to verify the default GHCR URL.
   - Remove the compose.yaml/Containerfile creation lines
   - Update assertion to: `assert st["image_tag"] == "ghcr.io/nrmeyers/agentalloy:latest"`

### Verification

```bash
# Run TestContainerFlow tests
python -m pytest tests/test_simple_setup.py::TestContainerFlow -v

# Run all simple_setup tests
python -m pytest tests/test_simple_setup.py -v
```

### Rollback

Restore from git:
```bash
git checkout HEAD -- tests/test_simple_setup.py
```

---

## Task 11: Run Full Test Suite and Integration Verification

**Requirement:** All requirements
**Effort:** Small
**Risk:** Low

### Description

Run the full test suite to verify all tasks are complete and no regressions were introduced.

### Verification

```bash
cd /home/nmeyers/dev/agentalloy

# Run all tests
python -m pytest tests/ -v --tb=short

# Run only container-related tests
python -m pytest tests/install/test_container_runtime.py tests/test_container_e2e.py tests/test_simple_setup.py::TestContainerFlow -v --tb=short

# Verify no build_context references remain in source
grep -rn "build_context\|_build_image\|_locate_build_context\|_has_assets" src/agentalloy/install/subcommands/
# Should return nothing

# Verify no compose.yaml/Containerfile references in test helpers
grep -rn "_make_compose_file\|_make_containerfile" tests/test_container_e2e.py
# Should return nothing

# Verify preflight has no build-context checks
grep -n "_check_build_context\|_check_image_build_deps" src/agentalloy/install/subcommands/preflight.py
# Should return nothing

# Verify --build-context argument removed
grep -n "build-context" src/agentalloy/install/subcommands/preflight.py
# Should return nothing
```

---

## Task 12: Manual E2E Verification

**Requirement:** All requirements
**Effort:** Small
**Risk:** Low

### Description

Manual end-to-end verification of the migration:

1. **Online setup (requires network to ghcr.io):**
   ```bash
   agentalloy setup --deployment container --non-interactive
   ```

2. **Offline setup (requires pre-pulled tarball):**
   ```bash
   podman save ghcr.io/nrmeyers/agentalloy:latest > /tmp/agentalloy-image.tar
   agentalloy setup --deployment container --image-path /tmp/agentalloy-image.tar --non-interactive
   ```

3. **Verify container is running:**
   ```bash
   podman ps --filter name=agentalloy
   curl -s http://localhost:47950/readiness
   ```

4. **Verify preflight checks:**
   ```bash
   agentalloy preflight --phase container
   ```

---

## Summary of Changes

### Files Modified

| File | Changes | Effort |
|---|---|---|
| `src/agentalloy/install/subcommands/container_runtime.py` | Task 1: timeout 300->600, remediation messages. Task 2: post-load verification. | Small |
| `src/agentalloy/install/subcommands/simple_setup.py` | Task 3: remove dead build path (127 lines). Task 4: image_tag default. | Medium |
| `src/agentalloy/install/subcommands/preflight.py` | Task 5: remove build-context checks (96 lines). Task 6: add GHCR reachability + disk space checks. | Medium |
| `tests/install/test_container_runtime.py` | Task 7: rename TestBuildImage->TestPullImage, update assertions. Task 8: remove TestLocateBuildContext, add TestOfflineLoad + TestPullImageFailureScenarios. | Medium |
| `tests/test_container_e2e.py` | Task 9: remove compose/containerfile helpers, update all tests. | Medium |
| `tests/test_simple_setup.py` | Task 10: update TestContainerFlow image_tag assertions. | Medium |

### Dependencies

```
Task 1 (timeout) ──┐
                    ├──> Task 7 (rename tests) ──> Task 8 (new tests) ──> Task 11 (full suite)
Task 2 (offline) ──┤
                    ├──> Task 3 (dead build path) ──> Task 4 (image_tag) ──> Task 10 (simple_setup tests)
Task 5 (preflight) ──> Task 6 (new preflight) ──────────────────────────────┘
Task 9 (e2e tests) ──────────────────────────────────────────────────────────┘
```

### Risk Summary

- **High risk:** Task 3 (dead build path removal) — changes the control flow of `_run_container_flow()`. Must be tested thoroughly.
- **Medium risk:** Tasks 5, 7, 8, 9 (preflight and test changes) — could break existing test expectations.
- **Low risk:** Tasks 1, 2, 4, 6, 10, 11, 12 — incremental changes with clear verification.

### Acceptance Criteria Mapping

| Requirement | Task(s) | Status |
|---|---|---|
| REQ-1: CI builds (no changes) | N/A | Already working |
| REQ-2: Setup pulls pre-built image | Task 1, 2, 4 | |
| REQ-3: Offline mode | Task 2 | |
| REQ-4: Runtime uses pulled image | Task 3 | |
| REQ-5: Update tests | Task 7, 8, 9, 10 | |
| REQ-6: Remove build-context code | Task 3, 5 | |
| REQ-7: Update preflight checks | Task 5, 6 | |
