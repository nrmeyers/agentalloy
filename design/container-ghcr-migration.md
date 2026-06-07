# Technical Design: GHCR Migration — Replace Local Container Builds with Pre-Built Images

**Document Version:** 1.1 (Reviewed)
**Date:** 2026-06-07
**Status:** Draft
**Spec:** `specs/container-ghcr-migration.md`
**Review Notes:** Second-pass review (2026-06-07) corrected: (1) build path at simple_setup.py:1182-1202 is dead code requiring removal, not "no changes needed"; (2) preflight line numbers corrected; (3) disk space check added as implemented requirement; (4) test file references updated to match actual files; (5) `_get_bootstrap_progress()` noted as existing; (6) tarball tag mismatch edge case added.

---

## 1. Executive Summary

This design document describes the migration from local container image builds (`podman build`) to pulling pre-built images from GitHub Container Registry (GHCR). The change eliminates the build-context requirement (full repo checkout with Containerfile, pyproject.toml, uv.lock) and replaces it with a simple `podman pull ghcr.io/nrmeyers/agentalloy:latest`.

The CI workflow at `.github/workflows/container-build.yml` already builds and pushes multi-arch images to GHCR. This migration focuses on the **setup/runtime path** — the code that runs on the user's machine during `agentalloy setup --deployment container`.

**Impact:** 3 source files modified significantly, 4 test files modified, 1 preflight file modified. No new dependencies. Backward-compatible via `--image-path` offline flag. **Critical:** A dead build path at `simple_setup.py:1182-1202` must be removed — it calls `_build_image()` which doesn't exist and would cause a `NameError` at runtime.

---

## 2. Architecture Overview

### 2.1 Before (Current State)

```
User runs: agentalloy setup --deployment container
    |
    v
_run_container_flow() [simple_setup.py:949]
    |
    +-- preflight.run_preflight(phase="early")
    |
    +-- _detect_runtime_binary() -> "podman" | "docker"
    |
    +-- if cfg.image_path (offline):
    |       _pull_image(runtime, offline=True, tarball_path=...)
    |           |-- subprocess.run([runtime, "load", "-i", tarball_path])
    |
    +-- else:
    |       _pull_image(runtime)
    |           |-- subprocess.run([runtime, "pull", "ghcr.io/nrmeyers/agentalloy:latest"])
    |
    +-- [DEAD CODE: lines 1182-1202]
    |       _build_image(binary_path, build_ctx)  <-- NameError: _build_image doesn't exist
    |
    +-- _ensure_volume(runtime)
    +-- _ensure_ollama_dir()
    +-- _generate_entrypoint(packs) -> Path
    +-- _run_container(runtime, entrypoint, packs)
    +-- _wait_for_readiness(port)
```

**Note:** The build path at lines 1182-1202 is dead code — `_build_image()` does not exist in `container_runtime.py`. It would raise `NameError` at runtime. This block must be removed.

**Problems with current approach:**
- The dead build path (lines 1182-1202) causes confusion during code review
- Preflight checks at `preflight.py:555-608` validate build context assets that are no longer needed
- `SetupConfig.image_tag` defaults to `"agentalloy:local"` (local build tag) — should be GHCR URL
- Tests at `tests/install/test_container_runtime.py:79-198` test `_locate_build_context()` which doesn't exist (ImportError)

### 2.2 After (Target State)

```
User runs: agentalloy setup --deployment container
    |
    v
_run_container_flow() [simple_setup.py:949]
    |
    +-- preflight.run_preflight(phase="early")
    |
    +-- _detect_runtime_binary() -> "podman" | "docker"
    |
    +-- if cfg.image_path (offline):
    |       _pull_image(runtime, offline=True, tarball_path=...)
    |           |-- subprocess.run([runtime, "load", "-i", tarball_path])
    |
    +-- else:
    |       _pull_image(runtime)
    |           |-- subprocess.run([runtime, "pull", "ghcr.io/nrmeyers/agentalloy:latest"])
    |
    +-- _ensure_volume(runtime)
    +-- _ensure_ollama_dir()
    +-- _generate_entrypoint(packs) -> Path
    +-- _run_container(runtime, entrypoint, packs)
    +-- _wait_for_readiness(port)
```

**Improvements:**
- No build context needed — just network access to GHCR (or `--image-path` for offline)
- `_pull_image()` name is now accurate — it pulls from GHCR
- Preflight checks validate runtime + port only, not build assets
- `SetupConfig.image_tag` defaults to `"ghcr.io/nrmeyers/agentalloy:latest"` (for state recording)
- `_DEFAULT_IMAGE` in `container_runtime.py` is the actual image used for pull/run

**Important distinction:** `SetupConfig.image_tag` is used for state recording (saved to disk). The actual image pull/run uses `_DEFAULT_IMAGE` from `container_runtime.py`. Both should be kept in sync.

### 2.3 Data Flow Diagram

```
                    +------------------+
                    |  CI Workflow     |
                    | container-build  |
                    | .yml             |
                    +--------+---------+
                             |
                             | push to main
                             | docker build-push
                             v
                    +------------------+
                    |  GHCR            |
                    | ghcr.io/nrmeyers |
                    | /agentalloy      |
                    | :latest + :sha   |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
    +---------v---------+       +-----------v-----------+
    | Online Setup      |       | Offline Setup         |
    | agentalloy setup  |       | agentalloy setup      |
    | --deployment      |       | --deployment          |
    | container         |       | container             |
    +---------+---------+       | --image-path /tmp/i   |
              |                 +-----------+-----------+
              |                             |
              | podman pull                 | podman load -i
              v                             v
    +---------+---------+       +-----------+-----------+
    | Image on disk     |       | Image on disk         |
    | agentalloy:latest |       | agentalloy:latest     |
    +---------+---------+       +-----------+-----------+
              |                             |
              +-----------------------------+
                            |
              +-------------v-------------+
              | _run_container()          |
              | --replace -d --name       |
              |   agentalloy              |
              | -p 47950:47950            |
              | -v agentalloy-data:/app   |
              | -v ~/.ollama:/root/.oll   |
              | -v entrypoint.sh:ro       |
              +-------------+-------------+
                            |
              +-------------v-------------+
              | _wait_for_readiness()     |
              | Poll /readiness endpoint  |
              | Timeout: 1800s            |
              +-------------+-------------+
                            |
              +-------------v-------------+
              | Container running         |
              | :47950                    |
              +---------------------------+
```

---

## 3. Component Changes

### 3.1 REQ-1: CI Builds and Pushes to GHCR (No Changes)

**File:** `.github/workflows/container-build.yml` (lines 1-51)

No changes required. The CI workflow already:
- Triggers on push to `main` and `workflow_dispatch` (lines 3-6)
- Builds multi-arch images for `linux/amd4` and `linux/arm64` (line 49)
- Tags with `latest` (on default branch) and git SHA (lines 39-41)
- Pushes to `ghcr.io/${{ github.repository }}` (line 38)

**File:** `Containerfile` (lines 1-56)

No changes required. The Containerfile builds a self-contained image with all dependencies installed via `uv sync`.

### 3.2 REQ-2: Setup Pulls Pre-Built Image via GHCR

**File:** `src/agentalloy/install/subcommands/container_runtime.py`

#### 3.2.1 Function Signature (No Change)

**Actual (line 57-62):**
```python
def _pull_image(
    runtime: str,
    image_ref: str | None = None,
    offline: bool = False,
    tarball_path: Path | None = None,
) -> int:
```

The signature is already correct for pull-only mode. The `image_ref` parameter defaults to `None`, which resolves to `_DEFAULT_IMAGE` (line 84):
```python
image = image_ref or _DEFAULT_IMAGE
```

Where `_DEFAULT_IMAGE` (line 54) is:
```python
_DEFAULT_IMAGE = "ghcr.io/nrmeyers/agentalloy:latest"
```

**Verification:** The function already supports both online pull and offline load paths. No signature changes needed.

#### 3.2.2 Online Pull Timeout Increase

**Actual (lines 107-122):**
```python
else:
    _print(f"  [dim]-> Pulling {image}[/dim]")
    try:
        subprocess.run(
            [runtime, "pull", image],
            check=True,
            timeout=300,
        )
        _print("  [green]-> Image pulled successfully[/green]")
        return 0
    except subprocess.CalledProcessError as exc:
        _print(f"  [red]Failed to pull image (exit {exc.returncode})[/red]")
        return exc.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]Image pull timed out after 300s[/red]")
        return 1
```

**Change:** Increase timeout from 300s to 600s (10 minutes) for large multi-arch images.

```python
else:
    _print(f"  [dim]-> Pulling {image}[/dim]")
    try:
        subprocess.run(
            [runtime, "pull", image],
            check=True,
            timeout=600,  # Changed: 600s for large multi-arch images
        )
        _print("  [green]-> Image pulled successfully[/green]")
        return 0
    except subprocess.CalledProcessError as exc:
        _print(f"  [red]Failed to pull image (exit {exc.returncode})[/red]")
        _print("  [dim]Remediation: Check network connectivity to ghcr.io, "
               "or use --image-path for offline mode.[/dim]")
        return exc.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]Image pull timed out after 600s[/red]")
        _print("  [dim]Remediation: Check network connectivity, "
               "or use --image-path for offline mode.[/dim]")
        return 1
```

#### 3.2.3 Offline Load Timeout Increase

**Current (lines 91-106):**
```python
subprocess.run(
    [runtime, "load", "-i", str(tarball_path)],
    check=True,
    capture_output=True,
    timeout=300,
)
```

**Change:** Increase timeout from 300s to 300s (keep as-is for now — tarballs are typically fast to load). Document the large-tarball edge case.

### 3.3 REQ-3: Offline Mode via --image-path Flag (No Changes)

**File:** `src/agentalloy/install/subcommands/simple_setup.py`

- Line 85: `image_path: str = ""` in `SetupConfig` — keep as-is
- Lines 1026-1032: already handles `cfg.image_path` — keep as-is
- `container_runtime.py` lines 86-106: offline path already uses `podman load -i` — keep as-is

**Edge case handling (already implemented):**
- Tarball not found: checked at `container_runtime.py:87`
- Load failure: stderr captured and displayed at `container_runtime.py:100-102`
- Load timeout: handled at `container_runtime.py:104-106`

**Additional edge case for implementation:**
After `podman load`, verify the expected image tag is present. Add validation:

```python
# After successful load, verify the image is present
result = subprocess.run(
    [runtime, "images", "--format", "{{.Repository}}:{{.Tag}}", image],
    capture_output=True,
    text=True,
    timeout=10,
)
if image not in result.stdout:
    _print(f"  [red]Image {image} not found after load[/red]")
    return 1
```

**Edge case: tarball tag mismatch.** The image tag embedded in the tarball may differ from `_DEFAULT_IMAGE`. A more robust approach is to use `podman images --format '{{.ID}}'` to check by digest after load, since the tag can vary.

### 3.4 REQ-4: Runtime Uses Pulled Image, No Build Context

**File:** `src/agentalloy/install/subcommands/container_runtime.py`

All runtime functions remain unchanged:
- Lines 130-155: `_ensure_volume()` — keep as-is
- Lines 163-169: `_ensure_ollama_dir()` — keep as-is
- Lines 177-213: `_generate_entrypoint()` — keep as-is
- Lines 422-493: `_run_container()` — keep as-is (uses pulled image, no build context)
- Lines 501-593: `_wait_for_readiness()` — keep as-is
- Lines 596-624: `_get_bootstrap_progress()` — new function, keep as-is

**File:** `src/agentalloy/install/subcommands/simple_setup.py`

**CRITICAL: Build path still exists at lines 1182-1202.** The current `_run_container_flow()` function has TWO code paths:
- Lines 1024-1038: Pull/load image (GHCR or tarball) — the NEW pull-only path
- Lines 1182-1202: Build from Containerfile — the OLD build path that must be removed

The build path calls `_build_image(binary_path, build_ctx)` at line 1185. This function does not exist in `container_runtime.py` (it was likely removed during a prior refactor), so the build path is already dead code but should be explicitly removed. The fallback-to-build path is guarded by an earlier condition (lines 1080-1180) that checks for build assets; since those assets are no longer required, the entire build path block should be deleted.

- Lines 1040-1075: volume, entrypoint, run, readiness — keep as-is (these are the lines actually reached after the pull path)
- Lines 1076-1099: Additional config setup (port, mode, harness, deployment) — verify these are reachable
- Lines 1182-1202: **BUILD PATH — REMOVE THIS ENTIRE BLOCK**
- Lines 1204-1279: Readiness, state recording — keep as-is (these are reached after the build path, need to be connected to the pull path)

### 3.5 REQ-5: Update Tests

#### 3.5.1 `tests/install/test_container_runtime.py`

**Current (lines 206-326):** `TestBuildImage` class with 10 tests that assert `podman build` commands.

**Changes:**

1. **Rename class:** `TestBuildImage` -> `TestPullImage` (line 206)

2. **Update docstring:** Change from "build" assertions to "pull" assertions (lines 207-210)

3. **Update all test methods:**

   | Current (line) | Old Assertion | New Assertion |
   |---|---|---|
   | `test_constructs_correct_command` (209-231) | `podman build -t agentalloy:local -f Containerfile context` | `podman pull ghcr.io/nrmeyers/agentalloy:latest` |
   | `test_uses_correct_image_tag` (233-245) | `agentalloy:local` | `ghcr.io/nrmeyers/agentalloy:latest` |
   | `test_uses_correct_dockerfile` (247-259) | `-f Containerfile` | (no dockerfile flag — pull only) |
   | `test_returns_zero_on_success` (261-268) | `podman build` | `podman pull` |
   | `test_returns_nonzero_on_failure` (270-282) | `CalledProcessError` on build | `CalledProcessError` on pull |
   | `test_writes_log_on_failure` (284-304) | `agentalloy-build.log` | `agentalloy-pull.log` |
   | `test_has_600s_timeout` (306-316) | `timeout=600` | `timeout=600` (keep) |
   | `test_returns_nonzero_on_timeout` (318-325) | `TimeoutExpired("podman build", 600)` | `TimeoutExpired("podman pull", 600)` |

4. **Remove `TestLocateBuildContext` class** (lines 79-198): All tests for `_locate_build_context()` should be removed since the function will be removed.

5. **Add `TestOfflineLoad` class:**

```python
class TestOfflineLoad:
    """Tests for offline image loading via --image-path flag."""

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
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("podman load", 300)):
            result = container_runtime._pull_image(
                "podman", offline=True, tarball_path=tarball
            )
            assert result == 1
```

6. **Add `TestPullImageFailureScenarios` class:**

```python
class TestPullImageFailureScenarios:
    """Tests for online image pull failure scenarios."""

    def test_network_timeout(self):
        """Returns 1 when pull times out."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("podman pull", 600)):
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

#### 3.5.2 `tests/test_container_e2e.py`

**Actual file structure (767 lines):**
- Lines 1-19: Module docstring (IT-1 through IT-14)
- Lines 33-51: `_make_compose_file()` and `_make_containerfile()` helpers
- Lines 54-58: `_inject_preflight_mocks()` (empty pass)
- Lines 61-133: `_run_with_all_patches()` helper
- Lines 141-193: `TestFullContainerFlow` (IT-1)
- Lines 201-226: `TestRuntimeNotFound` (IT-2)
- Lines 234-265: `TestBuildContextNotFound` (IT-3)
- Lines 273-292: `TestImageBuildFailure` (IT-4) — rename to `TestImagePullFailure`
- Lines 300-325: `TestContainerStartFailure` (IT-5)
- Lines 333-377: `TestHealthCheckTimeout` (IT-6)
- Lines 385-445: `TestStateRecording` (IT-7)
- Lines 453-472: `TestEntrypointCleanup` (IT-8)
- Lines 480-500: `TestEntrypointContent` (IT-9)

**Changes:**

1. **Update `TestImageBuildFailure` class (lines 273-292):** Rename to `TestImagePullFailure` and update assertions. The test mocks `_pull_image` to return 1, which is already correct for pull mode.

2. **Update `TestBuildContextNotFound` class (lines 234-265):** This test checks for missing compose.yaml + Containerfile. Since build context is no longer required, this test should be **removed** — the runtime now checks for compose/Containerfile in `_run_container_flow()` at line 1080+ before entering the build path.

3. **Update `_run_with_all_patches()` (lines 61-133):** The mock for `_pull_image` at line 84 is already correct (`"agentalloy.install.subcommands.container_runtime._pull_image"`). No changes needed.

4. **Update `TestHealthCheckTimeout` (lines 333-377):** Already uses `_wait_for_readiness` mock — correct for pull mode.

5. **Keep `TestEntrypointContent` (lines 480-500):** No changes needed — entrypoint script is unchanged.

### 3.6 REQ-6: Remove Build-Context Code Paths

**Finding:** The build-context code was partially removed during a prior refactor. Key observations:
- `_locate_build_context()`, `_has_assets()`, and `_build_image()` do NOT exist in `container_runtime.py` (confirmed by grep)
- However, `tests/install/test_container_runtime.py` still has a `TestLocateBuildContext` class (lines 79-198) that tests `_locate_build_context()` — these tests will fail with ImportError and must be **deleted**
- **CRITICAL:** `simple_setup.py` lines 1182-1202 still contain a build path that calls `_build_image(binary_path, build_ctx)`. Since `_build_image` doesn't exist, this path is dead code (it would raise `NameError` at runtime), but it should be explicitly removed to avoid confusion.

**Changes:**

1. **Remove `TestLocateBuildContext` class** from `tests/install/test_container_runtime.py` (lines 79-198)

2. **Remove build path block from `simple_setup.py`** (lines 1182-1202): This block calls `_build_image()` which doesn't exist. Remove the entire block and connect the readiness/state-recording code (lines 1204+) to the pull path.

3. **Remove `TestBuildContextNotFound`** from `tests/test_container_e2e.py` (lines 234-265) — no longer relevant.

4. **Update `tests/test_container_integration.py`:**
   - Line 273: `TestImageBuildFailure` class — rename to `TestImagePullFailure`
   - The test already mocks `_pull_image` to return 1, which is correct for pull mode

5. **Update `tests/test_simple_setup.py`:**
   - Search for `build_context`, `build_image`, `_has_assets` references
   - The design doc references `TestContainerFlow` class (line 980-2050) — actual file is 1904 lines, so this class spans approximately lines 980-1904

---

### 3.7 REQ-7: Preflight Checks Updated for Pull-Only Mode

**File:** `src/agentalloy/install/subcommands/preflight.py`

#### 3.7.1 Function Locations (Verified)

| Function | Line | Status |
|---|---|---|
| `_check_port_free()` | 226 | Keep as-is |
| `_check_runtime_binary()` | 518 | **New function** — already pull-mode aware, no change needed |
| `_check_build_context()` | 555-608 | **Remove** — no longer needed |
| `_check_name_conflicts()` | 611 | Keep as-is |
| `_check_volume_exists()` | 662 | Keep as-is |
| `_check_image_build_deps()` | 705-746 | **Remove** — no longer needed |
| `run_preflight()` | 766 | **Update** — remove build-context checks |

#### 3.7.2 Container Phase Checks (run_preflight, lines 787-801)

**Actual container phase code (lines 787-801):**
```python
elif phase == "container":
    if runtime is None:
        for candidate in ("podman", "docker"):
            if shutil.which(candidate) is not None:
                runtime = candidate
                break

    checks.append(_check_runtime_binary(runtime))
    checks.append(_check_git_present())
    checks.append(_check_build_context(build_context))
    checks.append(_check_name_conflicts(runtime or "podman"))
    checks.append(_check_volume_exists(runtime or "podman"))
    checks.append(_check_port_free(port))
    checks.append(_check_image_build_deps(build_context))
```

**Changes:**

1. **Remove `_check_build_context(build_context)`** (line 797) — no longer needed since we don't build locally.

2. **Remove `_check_image_build_deps(build_context)`** (line 801) — no longer needed.

3. **Change `_check_git_present()` severity** from "warn" to "info" — git is not needed for pull-only mode. The check at lines 478-510 already has `severity="warn"`, which is appropriate — it warns that git is missing but doesn't block.

4. **Add network connectivity check for GHCR** (optional, severity="warn"):

```python
def _check_ghcr_reachable() -> dict[str, Any]:
    """Check network connectivity to ghcr.io (for image pulls)."""
    t0 = time.monotonic()
    try:
        req = Request("https://ghcr.io", method="HEAD")
        with urlopen(req, timeout=5) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
    except (URLError, OSError) as exc:
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

5. **Update the container phase to use the new checks:**

```python
elif phase == "container":
    if runtime is None:
        for candidate in ("podman", "docker"):
            if shutil.which(candidate) is not None:
                runtime = candidate
                break

    checks.append(_check_runtime_binary(runtime))
    checks.append(_check_git_present())
    checks.append(_check_name_conflicts(runtime or "podman"))
    checks.append(_check_volume_exists(runtime or "podman"))
    checks.append(_check_port_free(port))
    checks.append(_check_ghcr_reachable())
```

6. **Update `_PHASES` tuple** (line 41): Already includes `"container"` — no change needed.

7. **Update `add_parser`** (lines 872-907): Remove `--build-context` argument since it's no longer needed for container phase.

---

## 4. Error Handling Strategy

### 4.1 Error Classification

| Error Type | Source | Handling | User Message |
|---|---|---|---|
| Network unavailable | `_pull_image()` online mode | Return non-zero, show remediation | "Failed to pull image. Check network. Use --image-path for offline." |
| GHCR rate limit | `subprocess.CalledProcessError` (exit 125) | Return exit code | "Rate limited by GHCR. Use --ghcr-token or --image-path." |
| Image not found | `subprocess.CalledProcessError` (exit 125) | Return exit code | "Image not found on GHCR. Check image tag." |
| Tarball not found | `_pull_image()` offline mode | Return 1 | "Offline mode: tarball not found at {path}" |
| Tarball load failure | `subprocess.CalledProcessError` | Return exit code, show stderr | "Failed to load image from tarball (exit N): {stderr[:200]}" |
| Tarball load timeout | `subprocess.TimeoutExpired` | Return 1 | "Image load timed out after 300s" |
| Pull timeout | `subprocess.TimeoutExpired` | Return 1 | "Image pull timed out after 600s" |
| Runtime not found | `_detect_runtime_binary()` | Return 1 | "Neither podman nor docker found on PATH" |
| Port in use | `_check_port_free()` | Return 1 (warn severity) | "Port 47950 in use" |
| Container name conflict | `_check_name_conflicts()` | Return 1 | "Container 'agentalloy' already exists" |

### 4.2 Error Propagation

```
_pull_image()
  |
  +-- returns 0 on success
  +-- returns 1 on generic failure (timeout, missing tarball)
  +-- returns specific exit code from CalledProcessError on pull/load failure
  |
  v
_run_container_flow()
  |
  +-- checks pull_rc != 0, returns pull_rc with user-friendly message
  +-- continues on success
  |
  v
_run_container() / _wait_for_readiness()
  |
  +-- returns 0 on success
  +-- returns non-zero on failure
```

### 4.3 GHCR Rate Limiting

Unauthenticated pulls are limited to 60/hour. Mitigation strategies:

1. **Default:** Use unauthenticated pulls (fine for individual users)
2. **Optional flag:** `--ghcr-token <token>` for authenticated pulls (5,000/hour)
3. **Credential file:** Check `~/.docker/config.json` or `~/.config/containers/auth.json` for existing credentials
4. **User message:** On rate limit error (exit 125), suggest using `--ghcr-token` or `--image-path`

### 4.4 Disk Space Check (New)

Pulling a multi-arch image (~500MB-1GB) requires sufficient disk space. Add a pre-pull disk space check:

```python
def _check_disk_space(runtime: str, min_bytes: int = 2 * 1024**3) -> dict[str, Any]:
    """Check available disk space before pulling/loading an image.

    Checks the disk space of the directory where the container runtime
    stores images (typically /var/lib/containers for podman,
    /var/lib/docker for docker).
    """
    import shutil
    # Try to find the container runtime's data directory
    # For podman: check $XDG_RUNTIME_DIR/containers or /var/lib/containers
    # For docker: check /var/lib/docker
    paths_to_check = [
        os.environ.get("CONTAINER_STORAGE", ""),
    ]
    if runtime == "podman":
        paths_to_check.extend([
            os.path.expanduser("~/.local/share/containers"),
            "/var/lib/containers",
        ])
    elif runtime == "docker":
        paths_to_check.append("/var/lib/docker")

    available = 0
    for path in paths_to_check:
        if path and os.path.exists(path):
            available = shutil.disk_usage(path).free
            break

    if available == 0:
        return _check(
            "disk_space",
            passed=False,
            severity="warn",
            error="Cannot determine container storage location",
            remediation="Ensure container runtime is properly configured.",
        )

    if available < min_bytes:
        return _check(
            "disk_space",
            passed=False,
            severity="fatal",
            error=f"Insufficient disk space: {available // (1024**2)}MB available, "
                  f"{min_bytes // (1024**2)}MB required",
            remediation="Free disk space or use --image-path with a smaller tarball.",
        )

    return _check(
        "disk_space",
        passed=True,
        detail=f"{available // (1024**3)}GB available",
    )
```

**Integration:** Call this check in `run_preflight()` during the container phase, before attempting to pull. If disk space is insufficient, fail with a clear error message.

---

## 5. Migration Path

### 5.1 Phase 1: Refactor `_pull_image()` (Week 1, Day 1-2)

**Goal:** Ensure `_pull_image()` correctly pulls from GHCR with increased timeout.

**Steps:**
1. Update timeout in `container_runtime.py` line 113: `timeout=300` -> `timeout=600`
2. Update timeout in `container_runtime.py` line 104: `300s` -> `600s` in error message
3. Add remediation text to error messages (lines 118, 121)

**Verification:** Run `pytest tests/install/test_container_runtime.py::TestPullImage`

### 5.2 Phase 2: Update `_run_container_flow()` (Week 1, Day 2-3)

**Goal:** Remove the dead build path and ensure the pull path flows directly to readiness.

**Current state:** `_run_container_flow()` has TWO code paths:
- Lines 1024-1038: Pull/load image (GHCR or tarball) — the NEW pull-only path (correct)
- Lines 1076-1180: Additional config setup (port, mode, harness, deployment) — verify reachable
- Lines 1182-1202: **DEAD BUILD PATH** — calls `_build_image()` which doesn't exist (NameError)
- Lines 1204-1279: Readiness, state recording — reached only via build path currently

**Steps:**
1. Remove the entire build path block (lines 1182-1202) from `simple_setup.py`
2. Ensure the readiness code (lines 1204+) is connected to the pull path — it may need to be at the same indentation level as the pull path, not nested under the build path
3. Verify lines 1076-1099 (port, mode, harness, deployment config) are reachable from the pull path
4. Update user-facing messages: "Pulling pre-built image from GHCR" is already correct (line 1034)

**Verification:** Run `pytest tests/test_container_integration.py` to verify no build-related assertions remain.

### 5.3 Phase 3: Update Tests (Week 1, Day 3-5)

**Goal:** All tests pass with pull-only behavior.

**Steps:**
1. Rename `TestBuildImage` -> `TestPullImage` in `tests/install/test_container_runtime.py`
2. Update all assertions from `podman build` to `podman pull`
3. Remove `TestLocateBuildContext` class
4. Add `TestOfflineLoad` class
5. Add `TestPullImageFailureScenarios` class
6. Update `tests/test_container_e2e.py` to remove compose.yaml/Containerfile setup
7. Update `tests/test_container_integration.py` build tests to pull tests

**Verification:** Run full test suite: `pytest tests/`

### 5.4 Phase 4: Remove Build-Context Code (Week 2, Day 1-2)

**Goal:** Remove all build-context references from source and preflight.

**Steps:**
1. Remove `--build-context` argument from `preflight.py` `add_parser()` (line 902-905)
2. Remove `_check_build_context()` function from `preflight.py` (lines 555-608)
3. Remove `_check_image_build_deps()` function from `preflight.py` (lines 705-746)
4. Remove `build_context` parameter from `run_preflight()` (line 766)
5. Update container phase in `run_preflight()` (lines 787-801) to remove build-context checks
6. Add `_check_ghcr_reachable()` to container phase checks (optional, severity="warn")
7. Remove `build_context` parameter from `_run_container_flow()` preflight call (line 957) — note: `run_preflight` is called without `build_context` at line 957, so no change needed
8. **Remove dead build path from `simple_setup.py`** (lines 1182-1202) — this is the critical change that Phase 2 identified
9. Update `SetupConfig.image_tag` default from `"agentalloy:local"` to `"ghcr.io/nrmeyers/agentalloy:latest"` (line 82)

**Verification:** Run full test suite: `pytest tests/`

### 5.5 Phase 5: Documentation (Week 2, Day 3-5)

**Goal:** Update all user-facing documentation.

**Steps:**
1. Update `INSTALL.md` — remove local build instructions
2. Update `README.md` — update container setup examples
3. Update any `docs/design/` docs that reference local build
4. Add migration guide for existing users (if applicable)

---

## 6. Test Strategy

### 6.1 Unit Tests

| File | Class | Tests | Status |
|---|---|---|---|
| `tests/install/test_container_runtime.py` | `TestDetectRuntimeBinary` | 5 tests | No change |
| `tests/install/test_container_runtime.py` | `TestPullImage` | 8 tests (renamed from TestBuildImage) | **Update** |
| `tests/install/test_container_runtime.py` | `TestOfflineLoad` | 4 tests (new) | **Add** |
| `tests/install/test_container_runtime.py` | `TestPullImageFailureScenarios` | 4 tests (new) | **Add** |
| `tests/install/test_container_runtime.py` | `TestLocateBuildContext` | 6 tests | **Remove** |
| `tests/install/test_preflight.py` | `TestCheckRuntimeBinary` | 3 tests | No change (already pull-mode aware) |
| `tests/install/test_preflight.py` | `TestCheckBuildContext` | 5 tests | **Remove** (build context no longer checked) |
| `tests/install/test_preflight.py` | `TestCheckImageBuildDeps` | 3 tests | **Remove** (build deps no longer checked) |
| `tests/install/test_preflight.py` | `TestCheckDiskSpace` | 3 tests (new) | **Add** |

### 6.2 Integration Tests

| File | Class | Tests | Status |
|---|---|---|---|
| `tests/test_container_e2e.py` | `TestFullContainerFlow` | 1 test (IT-1) | No change (already mocks pull) |
| `tests/test_container_e2e.py` | `TestRuntimeNotFound` | 1 test (IT-2) | No change |
| `tests/test_container_e2e.py` | `TestBuildContextNotFound` | 1 test (IT-3) | **Remove** (no build context) |
| `tests/test_container_e2e.py` | `TestImagePullFailure` | 1 test (IT-4, renamed from TestImageBuildFailure) | **Rename** |
| `tests/test_container_e2e.py` | `TestContainerStartFailure` | 1 test (IT-5) | No change |
| `tests/test_container_e2e.py` | `TestHealthCheckTimeout` | 1 test (IT-6) | No change |
| `tests/test_container_e2e.py` | `TestStateRecording` | 1 test (IT-7) | No change |
| `tests/test_container_e2e.py` | `TestEntrypointCleanup` | 1 test (IT-8) | No change |
| `tests/test_container_e2e.py` | `TestEntrypointContent` | 1 test (IT-9) | No change |
| `tests/test_container_integration.py` | `TestImageBuildFailure` | 1 test | **Rename to TestImagePullFailure** |
| `tests/test_simple_setup.py` | `TestContainerFlow` | ~20 tests | **Update** — remove build-context assertions |

### 6.3 E2E Tests

Manual E2E test procedure:

```bash
# Online setup (requires network to ghcr.io)
agentalloy setup --deployment container --non-interactive

# Offline setup (requires pre-pulled tarball)
podman save ghcr.io/nrmeyers/agentalloy:latest > /tmp/agentalloy-image.tar
agentalloy setup --deployment container --image-path /tmp/agentalloy-image.tar --non-interactive

# Verify container is running
podman ps --filter name=agentalloy
curl -s http://localhost:47950/readiness
```

### 6.4 Test Matrix

| Scenario | Online | Offline | Expected Result |
|---|---|---|---|
| Normal setup | Yes | No | Image pulled, container runs |
| No network | No | No | Error: "Failed to pull image" |
| No network + tarball | No | Yes | Image loaded, container runs |
| Missing tarball | No | Yes (bad path) | Error: "tarball not found" |
| Invalid tarball | No | Yes (bad format) | Error: "Failed to load image" |
| GHCR rate limited | No | No | Error: "Rate limited" |
| Image not found | No | No | Error: "Image not found" |
| Port in use | Yes | Yes | Error: "Port 47950 in use" |

---

## 7. Risk Assessment and Mitigation

### 7.1 Risk Matrix

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| GHCR outage | Low | High | Offline mode via `--image-path` |
| GHCR rate limiting | Medium | Medium | Authenticated pulls, rate limit user messaging |
| Network failure during pull | Medium | Medium | 600s timeout, clear error message, offline fallback |
| Image tag mismatch | Low | Medium | Version check via image label (future enhancement) |
| Breaking existing installs | Low | Low | Existing containers unaffected; new installs use GHCR |
| Test regressions | Medium | Medium | Comprehensive test suite, CI gate |
| Large image pull timeout | Low | Low | 600s timeout (10 min) for multi-arch images |
| Disk space for pulled image | Medium | Medium | Pre-pull disk check added (Section 4.4) — verifies 2GB+ available before pull |

### 7.2 Detailed Risk Analysis

**GHCR Rate Limiting (Medium/Medium):**
- Unauthenticated: 60 pulls/hour from same IP
- Authenticated: 5,000 pulls/hour
- Mitigation: Add `--ghcr-token` flag for CI/organizational use. Check for existing Docker auth config.

**Image Pull Timeout (Low/Low):**
- Multi-arch image (amd64 + arm64) may be ~1-2GB
- At 10 MB/s, pull takes ~170 seconds
- 600s timeout provides 3.5x headroom
- Mitigation: Show progress indicator during pull (future enhancement)

**Disk Space (Medium/Medium):**
- Image size ~500MB-1GB (multi-arch: amd64 + arm64)
- Tarball size similar
- **Implementation:** Pre-pull disk space check in `run_preflight()` (Section 4.4). Checks 2GB+ available before pull.
- **Edge case:** Nested containers or CI runners with limited disk. The check uses `shutil.disk_usage()` on the runtime's storage directory.

**Backward Compatibility (Low/Low):**
- Existing containers built from local images continue to work
- New `agentalloy setup --deployment container` uses GHCR
- No migration path needed for existing installs
- `--image-path` flag works for both old and new images

---

## 8. Operational Runbook

### 8.1 Monitoring

| Metric | Source | Alert Threshold |
|---|---|---|
| Container health | `curl -s http://localhost:47950/health` | Non-200 for >5 min |
| Readiness | `curl -s http://localhost:47950/readiness` | Not "ready" for >30 min |
| Container uptime | `podman inspect agentalloy --format '{{.State.StartedAt}}'` | Down >15 min |
| Disk space | `df -h /var/lib/containers` | <10% free |
| Image age | `podman images --format '{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}'` | >7 days old (stale image) |

### 8.2 Common Operations

| Operation | Podman | Docker |
|---|---|---|
| View logs | `podman logs -f agentalloy` | `docker logs -f agentalloy` |
| Restart | `podman restart agentalloy` | `docker restart agentalloy` |
| Inspect | `podman inspect agentalloy` | `docker inspect agentalloy` |
| Exec into container | `podman exec -it agentalloy sh` | `docker exec -it agentalloy sh` |
| Stop | `podman stop agentalloy` | `docker stop agentalloy` |
| Remove | `podman rm -f agentalloy` | `docker rm -f agentalloy` |
| Pull new image | `podman pull ghcr.io/nrmeyers/agentalloy:latest` | `docker pull ghcr.io/nrmeyers/agentalloy:latest` |
| Load tarball | `podman load -i image.tar` | `docker load -i image.tar` |
| List images | `podman images` | `docker images` |

### 8.3 Rollback Procedure

**Scenario:** After migration, existing container from local build needs rollback.

```bash
# Step 1: Stop current container
podman stop agentalloy
podman rm -f agentalloy

# Step 2: Pull previous image (if still available locally)
podman images | grep agentalloy

# Step 3: If previous image is gone, rebuild locally
podman build -t agentalloy:local -f Containerfile .

# Step 4: Re-run setup with local image
agentalloy setup --deployment container --image-tag agentalloy:local --non-interactive
```

**Scenario:** GHCR is down, need to deploy from tarball.

```bash
# Step 1: Get tarball from another machine or previous pull
scp user@other-host:/tmp/agentalloy-image.tar /tmp/

# Step 2: Deploy from tarball
agentalloy setup --deployment container --image-path /tmp/agentalloy-image.tar --non-interactive
```

### 8.4 Troubleshooting

**Problem:** "Failed to pull image (exit 125)"
- **Cause:** GHCR rate limit or authentication issue
- **Fix:** Use `--ghcr-token` flag or `--image-path` for offline mode

**Problem:** "Image pull timed out after 600s"
- **Cause:** Slow network or large image
- **Fix:** Check network, try offline mode, or increase timeout (future)

**Problem:** "Container failed to become ready"
- **Cause:** Container started but API not ready
- **Fix:** Check logs: `podman logs agentalloy`, check health: `curl http://localhost:47950/health`

**Problem:** "Neither podman nor docker found on PATH"
- **Cause:** Container runtime not installed
- **Fix:** `sudo apt install podman` (Linux) or `brew install podman` (macOS)

---

## 9. Files Modified Summary

| File | Lines Changed | Description |
|---|---|---|
| `src/agentalloy/install/subcommands/container_runtime.py` | 57-122, 596-624 | Increase pull timeout 300->600s, add remediation messages; `_get_bootstrap_progress()` already exists |
| `src/agentalloy/install/subcommands/simple_setup.py` | 82, 1024-1038, 1182-1202 | Update `image_tag` default, verify pull path; **REMOVE** dead build path at 1182-1202 |
| `src/agentalloy/install/subcommands/preflight.py` | 41, 518, 555-608, 705-746, 766, 787-801, 872-905 | Remove build-context checks, add GHCR reachability check, add disk space check |
| `tests/install/test_container_runtime.py` | 79-198, 206-326 | Remove TestLocateBuildContext, rename TestBuildImage->TestPullImage, add offline/failure tests |
| `tests/test_container_e2e.py` | 234-265, 273-292 | Remove TestBuildContextNotFound, rename TestImageBuildFailure->TestImagePullFailure |
| `tests/test_container_integration.py` | 273-292 | Rename TestImageBuildFailure -> TestImagePullFailure |
| `tests/test_simple_setup.py` | ~980-1904 | Remove build-context references in TestContainerFlow |

---

## 10. Open Questions

1. **Image versioning:** Should we support `--image-tag` flag to select `latest` vs specific SHA?
2. **GHCR authentication:** Consider `--ghcr-token` flag for authenticated pulls (5,000/hour vs 60/hour).
3. **Registry mirror:** Consider `CONTAINER_REGISTRY_MIRROR` env var for air-gapped environments.
4. **Image integrity:** Consider signature verification (cosign) for security-conscious deployments.
5. **Progress indicator:** Show pull progress (bytes transferred) for large images.
6. **Tarball tag mismatch:** When loading from tarball, the embedded image tag may differ from `_DEFAULT_IMAGE`. Consider verifying by digest after load.
7. **Dead build path:** The build path at `simple_setup.py:1182-1202` is dead code (calls non-existent `_build_image`). It should be removed — see Phase 2 of migration.

---

## 11. Acceptance Criteria Checklist

### REQ-1: CI Builds and Pushes to GHCR
- [ ] AC-1.1: CI builds multi-arch images and pushes to GHCR (already working)
- [ ] AC-1.2: CI tags images with git SHA (already working)
- [ ] AC-1.3: `latest` tag only on default branch (already working)
- [ ] AC-1.4: Workflow triggered by push and workflow_dispatch (already working)

### REQ-2: Setup Pulls Pre-Built Image via GHCR
- [ ] AC-2.1: `_pull_image()` pulls from GHCR using `podman pull` (timeout 600s)
- [ ] AC-2.2: Default image is `ghcr.io/nrmeyers/agentalloy:latest`
- [ ] AC-2.3: Pull timeout is 600 seconds
- [ ] AC-2.4: Pull failure shows clear error with remediation steps
- [ ] AC-2.5: `_run_container_flow()` no longer accepts build context parameter

### REQ-3: Offline Mode via --image-path Flag
- [ ] AC-3.1: `--image-path` loads image via `podman load -i`
- [ ] AC-3.2: Tarball path validated before load
- [ ] AC-3.3: Load failure shows exact error from `podman load`
- [ ] AC-3.4: `image_path` field in SetupConfig (already present)
- [ ] AC-3.5: Offline takes precedence over online with warning

### REQ-4: Runtime Uses Pulled Image, No Build Context
- [ ] AC-4.1: No build context needed for setup
- [ ] AC-4.2: Image is self-contained
- [ ] AC-4.3: Volume mounts unchanged
- [ ] AC-4.4: Port mapping unchanged
- [ ] AC-4.5: Entrypoint script generation unchanged
- [ ] AC-4.6: Readiness polling unchanged
- [ ] AC-4.7: Dead build path (lines 1182-1202 in simple_setup.py) removed

### REQ-5: Update Tests
- [ ] AC-5.1: `TestBuildImage` renamed to `TestPullImage` with pull assertions
- [ ] AC-5.2: E2E tests mock `podman pull` instead of `podman build`
- [ ] AC-5.3: Offline mode tests added
- [ ] AC-5.4: Pull failure scenario tests added
- [ ] AC-5.5: Image reference variant tests added
- [ ] AC-5.6: `TestLocateBuildContext` removed from test_container_runtime.py
- [ ] AC-5.7: `TestBuildContextNotFound` removed from test_container_e2e.py

### REQ-6: Remove Build-Context Code Paths
- [ ] AC-6.1: `_locate_build_context()` removed (already absent from source)
- [ ] AC-6.2: `_has_assets()` removed (already absent from source)
- [ ] AC-6.3: `_build_image()` removed (already absent from source)
- [ ] AC-6.4: No references in `simple_setup.py`
- [ ] AC-6.5: Tests referencing these functions removed
- [ ] AC-6.6: Dead build path block (lines 1182-1202) removed from simple_setup.py

### REQ-7: Preflight Checks Updated
- [ ] AC-7.1: Preflight verifies runtime binary on PATH
- [ ] AC-7.2: Preflight verifies port 47950 not in use
- [ ] AC-7.3: Preflight optionally verifies GHCR connectivity
- [ ] AC-7.4: Preflight no longer requires Containerfile/pyproject.toml/uv.lock
- [ ] AC-7.5: Preflight verifies disk space before pull (new)

---

## Appendix A: Function Signature Reference

### `_pull_image()`

```python
def _pull_image(
    runtime: str,
    image_ref: str | None = None,
    offline: bool = False,
    tarball_path: Path | None = None,
) -> int:
```

**Parameters:**
- `runtime`: Container runtime binary ("podman" or "docker")
- `image_ref`: Image reference to pull. Defaults to `_DEFAULT_IMAGE` (GHCR)
- `offline`: If True, load from tarball instead of pulling
- `tarball_path`: Path to image tarball (required when offline=True)

**Returns:** Exit code (0 on success, non-zero on failure)

**Side effects:**
- Online: `podman pull <image_ref>` or `docker pull <image_ref>`
- Offline: `podman load -i <tarball_path>` or `docker load -i <tarball_path>`

### `_run_container_flow()`

```python
def _run_container_flow(cfg: SetupConfig, t0: float) -> int:
```

**Flow:**
1. Preflight check (early phase)
2. Detect runtime binary (podman/docker)
3. Pull or load image (GHCR or tarball)
4. Ensure data volume and ollama directory
5. Generate entrypoint script
6. Run container
7. Wait for readiness

**Returns:** Exit code (0 on success)
