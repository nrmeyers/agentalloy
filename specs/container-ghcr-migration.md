# Spec: GHCR Migration — Replace Local Container Builds with Pre-Built Images

## Summary

Replace the local container build path in `agentalloy setup --deployment container` with a pull-from-GHCR path. The CI workflow at `.github/workflows/container-build.yml` already builds and pushes multi-arch images to `ghcr.io/nrmeyers/agentalloy` tagged with `latest` (on main) and the git SHA. The migration eliminates the build-context requirement (full repo checkout needed for local `podman build`) and replaces it with a simple `podman pull`.

## Background / Current State

### Existing CI (already functional)
- **File:** `.github/workflows/container-build.yml` (lines 1-51)
- Builds multi-arch images (`linux/amd64`, `linux/arm64`) from `Containerfile`
- Tags: `latest` (default branch only) + git SHA
- Pushes to `ghcr.io/${{ github.repository }}` (i.e., `ghcr.io/nrmeyers/agentalloy`)
- Uses `docker/build-push-action@v5`, `docker/metadata-action@v5`

### Existing Container Runtime (partial)
- **File:** `src/agentalloy/install/subcommands/container_runtime.py` (624 lines)
- `_DEFAULT_IMAGE = "ghcr.io/nrmeyers/agentalloy:latest"` (line 54)
- `_pull_image()` function (lines 57-122): already pulls from GHCR in online mode, loads from tarball in offline mode
- `_run_container()` (lines 422-493): runs container with pulled image
- `_generate_entrypoint()` (lines 177-213): generates bootstrap script
- `_wait_for_readiness()` (lines 501-593): polls `/readiness` endpoint

### Current `_run_container_flow()` in simple_setup.py
- **File:** `src/agentalloy/install/subcommands/simple_setup.py`, function at line 949
- Lines 1024-1038: calls `_pull_image()` — but the surrounding code and tests treat this as a "build" step
- The function name `_pull_image` in `container_runtime.py` pulls from GHCR using `podman pull` (online) or `podman load -i` (offline). The GHCR migration means `_pull_image()` only pulls/loads — no local build context.

### Existing Offline Mode
- `SetupConfig.image_path` (line 85 in `simple_setup.py`): path to image tarball for offline mode
- `_pull_image()` already supports `offline=True` + `tarball_path` via `podman load`

## Requirements

### REQ-1: CI Builds and Pushes to GHCR (no changes needed)

**Requirement:** The existing CI workflow at `.github/workflows/container-build.yml` builds multi-arch images and pushes to GHCR with `latest` + SHA tags. This is already functional and requires no changes.

**Acceptance Criteria:**
- [ ] AC-1.1: On push to `main`, CI builds images for `linux/amd64` and `linux/arm64` and pushes to `ghcr.io/nrmeyers/agentalloy:latest`
- [ ] AC-1.2: CI tags images with git SHA (`type=sha` in metadata action)
- [ ] AC-1.3: `latest` tag is only applied on the default branch (`enable={{is_default_branch}}`)
- [ ] AC-1.4: Workflow is triggered by `push` to `main` and `workflow_dispatch`

**Exact file paths:**
- `.github/workflows/container-build.yml` (lines 1-51) — no changes needed

### REQ-2: Setup Pulls Pre-Built Image via GHCR

**Requirement:** Replace the local build step in `_run_container_flow()` with a GHCR pull. The `_pull_image()` function in `container_runtime.py` should pull from GHCR (no build context).

**Acceptance Criteria:**
- [ ] AC-2.1: `_pull_image()` in `container_runtime.py` pulls from GHCR using `podman pull <image_ref>` (no build context parameter)
- [ ] AC-2.2: Default image is `ghcr.io/nrmeyers/agentalloy:latest` (already set at line 54)
- [ ] AC-2.3: Pull timeout is 600 seconds (10 minutes) for large multi-arch images
- [ ] AC-2.4: On pull failure, user sees a clear error with remediation steps
- [ ] AC-2.5: The `_run_container_flow()` function no longer accepts or requires a build context parameter

**Exact file paths:**
- `src/agentalloy/install/subcommands/container_runtime.py`
  - Line 54: `_DEFAULT_IMAGE = "ghcr.io/nrmeyers/agentalloy:latest"` (keep as-is)
  - Lines 57-122: `_pull_image()` — refactor to remove build context, only pull
  - Remove any build-context-related parameters from the function signature
- `src/agentalloy/install/subcommands/simple_setup.py`
  - Line 949: `_run_container_flow()` — remove build context logic, only call `_pull_image()` without context
  - Lines 1024-1038: already calls `_pull_image()` — update to pass no build context

**Edge cases:**
- Network unavailable: user must use `--image-path` flag (REQ-3)
- GHCR rate limiting: unauthenticated pulls are limited to 60/hour. Authenticated pulls (via `ghcr` token) get 5,000/hour. Consider adding `--ghcr-token` flag for offline/CI scenarios.
- Image digest pinning: for reproducible builds, optionally support `ghcr.io/nrmeyers/agentalloy@sha256:<digest>`
- Registry mirror: support `CONTAINER_REGISTRY_MIRROR` env var for air-gapped environments

### REQ-3: Offline Mode via --image-path Flag

**Requirement:** Support offline deployment via a pre-pulled image tarball. The `--image-path` flag on `agentalloy setup` specifies the path to a `podman save` / `docker save` tarball.

**Acceptance Criteria:**
- [ ] AC-3.1: `agentalloy setup --deployment container --image-path /path/to/image.tar` loads the image via `podman load -i /path/to/image.tar`
- [ ] AC-3.2: The tarball path is validated (file exists, is readable) before attempting load
- [ ] AC-3.3: On load failure, user sees the exact error from `podman load`
- [ ] AC-3.4: The `image_path` field is already present in `SetupConfig` (line 85 in `simple_setup.py`)
- [ ] AC-3.5: Offline mode is mutually exclusive with online pull (if both specified, offline takes precedence with a warning)

**Exact file paths:**
- `src/agentalloy/install/subcommands/simple_setup.py`
  - Line 85: `image_path: str = ""` in `SetupConfig` (keep as-is)
  - Lines 1026-1032: already handles `cfg.image_path` — verify it calls `_pull_image(binary_path, offline=True, tarball_path=_Path(cfg.image_path))`
- `src/agentalloy/install/subcommands/container_runtime.py`
  - Lines 86-106: offline path already uses `podman load -i` (keep as-is)

**Edge cases:**
- Tarball contains multiple images: `podman load` loads all; the setup should verify the expected image tag is present after load
- Tarball format mismatch: `podman load` may reject Docker-format tarballs; support both `podman save` and `docker save` output
- Large tarballs (>2GB): show progress indicator; 5-minute timeout for load operation

### REQ-4: Runtime Uses Pulled Image, No Build Context

**Requirement:** The container runtime must not require the full repository checkout. After the image is pulled (or loaded from tarball), the container runs with the same volumes, env vars, and port mapping as before.

**Acceptance Criteria:**
- [ ] AC-4.1: No build context is needed for container setup — the repo source files are not copied into the container at setup time
- [ ] AC-4.2: The container image is self-contained (all deps installed via `uv sync` in the Containerfile build)
- [ ] AC-4.3: Volume mounts are unchanged: `agentalloy-data:/app/data` and `~/.ollama:/root/.ollama`
- [ ] AC-4.4: Port mapping unchanged: `47950:47950`
- [ ] AC-4.5: Entrypoint script generation unchanged (generated at setup time, mounted as `/app/entrypoint.sh:ro`)
- [ ] AC-4.6: Readiness polling unchanged (`_wait_for_readiness()` at lines 501-593)

**Exact file paths:**
- `src/agentalloy/install/subcommands/container_runtime.py`
  - Lines 130-155: `_ensure_volume()` — keep as-is
  - Lines 163-169: `_ensure_ollama_dir()` — keep as-is
  - Lines 177-213: `_generate_entrypoint()` — keep as-is
  - Lines 422-493: `_run_container()` — keep as-is (uses pulled image, no build context)
- `src/agentalloy/install/subcommands/simple_setup.py`
  - Lines 1040-1075: volume, entrypoint, run, readiness — keep as-is

**Edge cases:**
- Image tag mismatch: if the pulled image was built from a different Containerfile version, entrypoint script may be incompatible. Add a version check (e.g., image label `agentalloy.version`).
- Entrypoint script permissions: the temp entrypoint file must be `0o700` (already set at line 212).
- Container name collision: `--replace` flag handles this (already used at line 469).

### REQ-5: Update Tests

**Requirement:** Update all tests that assume local build to assume GHCR pull. Tests must verify the new pull-based flow.

**Acceptance Criteria:**
- [ ] AC-5.1: `tests/install/test_container_runtime.py` — update `TestBuildImage` tests to verify `_pull_image()` calls `podman pull` (not `podman build`)
- [ ] AC-5.2: `tests/test_container_e2e.py` — update E2E tests to mock `podman pull` instead of `podman build`
- [ ] AC-5.3: Add tests for offline mode (`--image-path`) — tarball load success, tarball load failure, missing tarball
- [ ] AC-5.4: Add tests for image pull failure scenarios — network timeout, auth failure, image not found
- [ ] AC-5.5: Add tests for GHCR image reference variants — `latest` tag, SHA-pinned digest, custom registry

**Exact file paths:**
- `tests/install/test_container_runtime.py`
  - Lines 206-326: `TestBuildImage` — rename to `TestPullImage`, update all assertions to expect `podman pull` commands
  - Add `TestOfflineLoad` class for offline mode tests
- `tests/test_container_e2e.py`
  - Lines 271-424: `TestFullContainerSetup` — update mocks to expect `podman pull` instead of `podman build`
  - Lines 426-500: `TestModelPullBootstrap` — keep as-is (model pull is in the entrypoint, not affected)
  - Add `TestOfflineSetup` class for offline e2e tests

### REQ-6: Remove Build-Context Code Paths

**Requirement:** Remove all code paths that locate and use a build context for local builds. This includes `_locate_build_context()`, `_has_assets()`, and any related functions.

**Acceptance Criteria:**
- [ ] AC-6.1: `_locate_build_context()` function is removed from `container_runtime.py`
- [ ] AC-6.2: `_has_assets()` function is removed from `container_runtime.py`
- [ ] AC-6.3: `_build_image()` function is removed from `container_runtime.py` (if it exists)
- [ ] AC-6.4: No imports or references to these functions remain in `simple_setup.py`
- [ ] AC-6.5: Tests that reference these functions are updated or removed

**Exact file paths:**
- `src/agentalloy/install/subcommands/container_runtime.py` — remove build-context functions
- `src/agentalloy/install/subcommands/simple_setup.py` — remove references to build context

**Edge cases:**
- Pre-flight checks: the preflight module may check for build assets. Update preflight to only check runtime availability (podman/docker on PATH), not build assets.

### REQ-7: Preflight Checks Updated for Pull-Only Mode

**Requirement:** Update preflight checks to validate runtime availability and network connectivity (for GHCR pull), not build context presence.

**Acceptance Criteria:**
- [ ] AC-7.1: Preflight checks for container mode verify: runtime binary (podman/docker) is on PATH
- [ ] AC-7.2: Preflight checks verify: port 47950 is not already in use
- [ ] AC-7.3: Preflight checks verify: network connectivity to `ghcr.io` (optional — fail silently if offline, fall back to `--image-path`)
- [ ] AC-7.4: Preflight checks no longer require `Containerfile`, `pyproject.toml`, or `uv.lock` to be present

**Exact file paths:**
- `src/agentalloy/install/subcommands/preflight.py` — update container-mode checks

## Migration Path

### Phase 1: Refactor `_pull_image()` (Week 1)
1. Update `container_runtime.py` `_pull_image()` to only pull from GHCR (remove build context parameter)
2. Update function signature: `_pull_image(runtime, image_ref=None, offline=False, tarball_path=None)`
3. Update docstring to reflect pull-only behavior
4. Update all callers in `simple_setup.py`

### Phase 2: Update `_run_container_flow()` (Week 1)
1. Remove build context logic from `_run_container_flow()` in `simple_setup.py`
2. Update the flow: detect runtime → pull image → ensure volume → generate entrypoint → run container → wait for readiness
3. Update user-facing messages (no more "building image" step)

### Phase 3: Update Tests (Week 2)
1. Update `tests/install/test_container_runtime.py` — `TestBuildImage` → `TestPullImage`
2. Update `tests/test_container_e2e.py` — mock `podman pull` instead of `podman build`
3. Add offline mode tests
4. Add failure scenario tests

### Phase 4: Remove Build-Context Code (Week 2)
1. Remove `_locate_build_context()`, `_has_assets()`, `_build_image()` from `container_runtime.py`
2. Remove build-context references from `simple_setup.py`
3. Update preflight checks in `preflight.py`
4. Run full test suite to confirm no regressions

### Phase 5: Documentation (Week 3)
1. Update `INSTALL.md` — remove local build instructions
2. Update `README.md` — update container setup examples
3. Update `docs/design/` docs that reference local build
4. Add migration guide for existing users (if applicable)

## Operational Contract (Unchanged)

Direct runtime equivalents for day-2 operations remain the same:

| Operation | Podman | Docker |
|-----------|--------|--------|
| Logs | `podman logs -f agentalloy` | `docker logs -f agentalloy` |
| Restart | `podman restart agentalloy` | `docker restart agentalloy` |
| Inspect | `podman inspect agentalloy` | `docker inspect agentalloy` |
| Exec | `podman exec -it agentalloy sh` | `docker exec -it agentalloy sh` |
| Stop | `podman stop agentalloy` | `docker stop agentalloy` |
| Remove | `podman rm -f agentalloy` | `docker rm -f agentalloy` |
| Pull image | `podman pull ghcr.io/nrmeyers/agentalloy:latest` | `docker pull ghcr.io/nrmeyers/agentalloy:latest` |
| Load tarball | `podman load -i image.tar` | `docker load -i image.tar` |

## Volume Layout (Unchanged)

```
/app/
├── data/                          # Named volume: agentalloy-data
│   ├── ladybug                    # Kuzu database (file-level lock)
│   ├── skills.duck                # DuckDB vector store
│   └── skills/                    # Packed skill definitions
└── .ollama/                       # Bind mount: ~/.ollama (host)
    └── models/
        ├── download/
        └── manifests/
```

## Open Questions

1. **Image versioning strategy:** Should we pin to a specific SHA for production installs? The CI already generates SHA tags. Consider adding a `--image-tag` flag to `agentalloy setup` to select `latest` vs a specific SHA.

2. **GHCR authentication:** Unauthenticated pulls are rate-limited (60/hour). For organizations with many installs, consider:
   - Adding a `--ghcr-token` flag for authenticated pulls
   - Checking `~/.docker/config.json` or `~/.config/containers/auth.json` for existing credentials
   - Documenting the rate limit and how to increase it

3. **Air-gapped deployments:** The `--image-path` flag handles this. Consider adding a `--mirror-url` flag for internal registry mirrors.

4. **Image integrity:** Consider adding image signature verification (cosign/distroless) for security-conscious deployments.

5. **Backward compatibility:** Existing containers built from local images will continue to work. No migration path is needed for existing installs.

6. **Containerfile changes:** The Containerfile at `Containerfile` (lines 1-56) builds the image. If the Containerfile changes (e.g., new dependencies), a new CI build must be triggered before the updated image is available for pull. Document this in the release process.

## Constraints

- Container deployment is CPU-only (GPU passthrough only works with native install) — this is unchanged from the existing spec at `docs/specs/direct-container-runtime-setup-spec.md` (lines 23-248).
- Container must work with both Podman (preferred) and Docker.
- The image is self-contained: all dependencies are installed during the CI build step.
- Existing native installs are not affected.
- The Containerfile (`Containerfile`, lines 1-56) is only used by CI, not by the setup flow.

## Assumptions

1. The GHCR registry URL (`ghcr.io/nrmeyers/agentalloy`) is correct and the repository exists.
2. The CI workflow `.github/workflows/container-build.yml` is running and producing valid images on every push to `main`.
3. The `ghcr.io/nrmeyers/agentalloy:latest` tag is always available (CI runs on every push to `main`).
4. The entrypoint script generated by `_generate_entrypoint()` is compatible with the image version (same code tree).
5. Users have network access to `ghcr.io` during setup (or use `--image-path` for offline).
