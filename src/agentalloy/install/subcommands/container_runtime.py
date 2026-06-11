"""``container_runtime`` — runtime detection and build context location.

Provides utilities for container deployment: detecting podman/docker on PATH
and locating the agentalloy build context (for building the container image).
"""

from __future__ import annotations

import contextlib
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print(*args: Any, **kwargs: Any) -> None:
    """Print to stdout with optional rich markup (same as simple_setup)."""
    print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


def _detect_runtime_binary() -> str | None:
    """Find a container runtime binary on PATH.

    Search order: podman (preferred), docker (fallback).

    Returns
    -------
    str | None
        ``"podman"`` if podman is found (regardless of docker),
        ``"docker"`` if only docker is found,
        ``None`` if neither is on PATH.
    """
    for candidate in ("podman", "docker"):
        if shutil.which(candidate) is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Image pull / load
# ---------------------------------------------------------------------------

_DEFAULT_IMAGE = "ghcr.io/nrmeyers/agentalloy:latest"

# Public alias for cross-module access (unprefixed consumers import this)
DEFAULT_IMAGE = _DEFAULT_IMAGE


def _pull_image(
    runtime: str,
    image_ref: str | None = None,
    offline: bool = False,
    tarball_path: Path | None = None,
) -> int:
    """Pull or load the agentalloy container image.

    In online mode (default): pulls from GHCR.
    In offline mode: loads from a local tarball (podman save / docker save output).

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).
    image_ref : str | None
        Image reference to pull. Defaults to ``ghcr.io/nrmeyers/agentalloy:latest``.
    offline : bool
        If True, load from tarball instead of pulling.
    tarball_path : Path | None
        Path to the image tarball (required when offline=True).

    Returns
    -------
    int
        Exit code (0 on success).
    """
    image = image_ref or _DEFAULT_IMAGE

    if offline:
        if tarball_path is None or not tarball_path.exists():
            _print(f"  [red]Offline mode: tarball not found at {tarball_path}[/red]")
            return 1
        _print(f"  [dim]-> Loading image from tarball: {tarball_path}[/dim]")
        try:
            subprocess.run(
                [runtime, "load", "-i", str(tarball_path)],
                check=True,
                capture_output=True,
                timeout=300,
            )
            _print("  [green]-> Image loaded from tarball[/green]")
            # Verify the expected image tag is present after load (handles tarball tag mismatch)
            result = subprocess.run(
                [runtime, "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Also check by ID for digest-based matching as fallback
            id_result = subprocess.run(
                [runtime, "images", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Must verify the SPECIFIC image we expected — the ID check alone is
            # too permissive (any image would satisfy it).  First try exact
            # reference match; only fall back to ID for digest-only images
            # where the tag format renders as "<none>:<none>".
            image_found = image in result.stdout
            if not image_found:
                # The images listing may contain many lines (all local images).
                # A digest-based load produces one or more "<none>:<none>" entries
                # (untagged images). Fall back to ID verification whenever ANY
                # line is "<none>:<none>" rather than requiring the entire output
                # to be that single value.
                has_untagged = any(
                    line.strip() == "<none>:<none>" for line in result.stdout.splitlines()
                )
                if has_untagged:
                    image_found = id_result.returncode == 0 and bool(id_result.stdout.strip())
            if not image_found:
                _print(f"  [red]Image {image} not found after load[/red]")
                return 1
            return 0
        except subprocess.CalledProcessError as exc:
            _print(f"  [red]Failed to load image from tarball (exit {exc.returncode})[/red]")
            _print(f"  stderr: {(exc.stderr or b'').decode(errors='replace')[:200]}")
            return exc.returncode
        except UnicodeDecodeError:
            _print("  [red]Failed to decode image load output[/red]")
            return 1
        except subprocess.TimeoutExpired:
            _print("  [red]Image load timed out after 300s[/red]")
            return 1
    else:
        _print(f"  [dim]-> Pulling {image}[/dim]")
        try:
            subprocess.run(
                [runtime, "pull", image],
                check=True,
                timeout=600,
            )
            _print("  [green]-> Image pulled successfully[/green]")
            return 0
        except subprocess.CalledProcessError as exc:
            _print(f"  [red]Failed to pull image (exit {exc.returncode})[/red]")
            _print(
                "  [dim]Remediation: Check network connectivity to ghcr.io, "
                "or use --image-path for offline mode.[/dim]"
            )
            return exc.returncode
        except subprocess.TimeoutExpired:
            _print("  [red]Image pull timed out after 600s[/red]")
            _print(
                "  [dim]Remediation: Check network connectivity, "
                "or use --image-path for offline mode.[/dim]"
            )
            return 1


# ---------------------------------------------------------------------------
# Volume management
# ---------------------------------------------------------------------------


def _ensure_volume(runtime: str) -> None:
    """Create the agentalloy data volume if it doesn't already exist.

    Idempotent — silently ignores the "volume already exists" error.

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).

    Raises
    ------
    subprocess.CalledProcessError
        If the volume creation fails for a reason other than "already exists".
    """
    try:
        subprocess.run(
            [runtime, "volume", "create", "agentalloy-data"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").lower()
        if "already exists" in stderr:
            return
        raise


# ---------------------------------------------------------------------------
# Ollama directory
# ---------------------------------------------------------------------------


def _ensure_ollama_dir() -> None:
    """Ensure ``~/.ollama`` exists.

    Creates the directory (with ``parents=True``) if it doesn't already
    exist.  Idempotent — no-op if the directory already exists.
    """
    Path.home().joinpath(".ollama").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Entrypoint generation
# ---------------------------------------------------------------------------


def _generate_entrypoint(packs: str) -> Path:
    """Generate an entrypoint wrapper script and return its temp file path.

    The entrypoint is a bash script that handles in-container bootstrap:

    1. Check if ``$APP_DIR/.bootstrap-complete`` exists — if so, skip to uvicorn.
    2. Check if Ollama is installed; download and install if needed.
    3. Start ``ollama serve`` in the background on ``127.0.0.1:11434``.
    4. Poll ``http://127.0.0.1:11434`` until Ollama is ready (30 s timeout).
    5. Check if the embedding model (``qwen3-embedding:0.6b``) is cached;
       pull it if not.
    6. Run migrations (``uv run python -m agentalloy.migrate``).
    7. If *packs* is non-empty, run ``uv run agentalloy install-packs --packs <packs>``
       for each pack. If *packs* is empty, run ``uv run agentalloy install-packs``
       (no --packs) to install always-on packs (core, documentation, engineering,
       performance).
    8. Create the ``$APP_DIR/.bootstrap-complete`` flag file.
    9. Writes ``.bootstrap-progress`` with ``phase="model_pull"`` before
       running ``ollama pull``, so the host-side readiness polling can
       surface model download progress to the user.
    10. Prints ``Model pull complete`` to stdout after the model download
        finishes — the host-side log streamer uses this as a transition
        marker to switch from log streaming to readiness polling.
    11. Trap SIGTERM for graceful shutdown (only if Ollama was started).
    12. Start uvicorn on ``0.0.0.0:47950``.

    Parameters
    ----------
    packs : str
        Comma-separated list of packs to install, or empty string.

    Returns
    -------
    Path
        Path to the generated entrypoint script (in the system temp directory).
    """
    script = _build_entrypoint_script(packs)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sh")  # noqa: SIM115 (file must persist for container mount)
    tmp.write(script.encode())
    tmp.close()
    entrypoint = Path(tmp.name)
    entrypoint.chmod(0o700)
    return entrypoint


def _build_entrypoint_script(packs: str) -> str:
    """Build the entrypoint wrapper script (checkpointed bootstrap + uvicorn).

    Compared to the original "bootstrap then exec uvicorn" pattern, this
    script:

    1. Creates ``.bootstrap-lock`` with an ISO timestamp at the start of a
       new bootstrap; removes it and creates ``.bootstrap-complete`` when
       done. The host-side ``/readiness`` endpoint reads these markers.
    2. Detects a stale lock (>2 h) left by a previous crashed container,
       wipes lock + checkpoints, and starts fresh.
    3. Iterates packs one-by-one, writes progress to ``.bootstrap-progress``
       (atomic temp + mv) before each pack, and appends a checkpoint line to
       ``.bootstrap-checkpoints`` after each pack succeeds.
    4. On restart, parses the checkpoint file and skips packs already
       recorded — partial bootstrap crashes resume from where they left off.
       A corrupted checkpoint file is treated as "no checkpoints" so the
       script never fails closed on a malformed line.
    5. Starts ``uvicorn`` **after** pack ingestion completes, avoiding the
       Kuzu file-lock conflict that occurred when uvicorn opened Ladybug
       before pack ingestion finished.
    """
    pack_list = [p for p in (packs or "").split(",") if p.strip()]
    has_packs = len(pack_list) > 0
    packs_total = len(pack_list)

    # Build the per-pack loop body as a shell array. We quote each element so
    # pack names with shell metacharacters (none in practice, but defense in
    # depth) can't break out of the array.
    pack_array_literal = " ".join(shlex.quote(p.strip()) for p in pack_list)

    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        "# App directory (configurable via APP_DIR env var, default /app)",
        "APP_DIR=${APP_DIR:-/app}",
        'LOCK="$APP_DIR/.bootstrap-lock"',
        'COMPLETE="$APP_DIR/.bootstrap-complete"',
        'PROGRESS="$APP_DIR/.bootstrap-progress"',
        'PROGRESS_TMP="$APP_DIR/.bootstrap-progress.tmp"',
        'CHECKPOINTS="$APP_DIR/.bootstrap-checkpoints"',
        'INSTALL_LOCK="$APP_DIR/.install-packs-lock"',
        "",
        "# --- Stale lock recovery -------------------------------------------",
        "# If the previous run crashed mid-bootstrap, the lock file persists",
        "# in the data volume. A lock older than 2h is considered stale.",
        'if [ -f "$LOCK" ] && [ ! -f "$COMPLETE" ]; then',
        '    LOCK_MTIME=$(stat -c %Y "$LOCK" 2>/dev/null || echo 0)',
        "    NOW=$(date +%s)",
        '    if [ "$LOCK_MTIME" -gt 0 ] && [ $((NOW - LOCK_MTIME)) -gt 7200 ]; then',
        '        echo ">> Stale bootstrap lock detected (>2h) - starting fresh"',
        '        rm -f "$LOCK" "$CHECKPOINTS" "$PROGRESS" "$PROGRESS_TMP"',
        "    fi",
        "fi",
        "",
        "# --- Checkpoint helpers --------------------------------------------",
        "# pack_already_done: 0 (true) if the pack name appears in checkpoints.",
        "# A corrupt checkpoint file simply yields no matches — treated as",
        '# "not done yet", so we re-run the pack rather than failing closed.',
        "pack_already_done() {",
        '    [ -f "$CHECKPOINTS" ] || return 1',
        '    grep -Fq "\\"pack\\": \\"$1\\"" "$CHECKPOINTS" 2>/dev/null',
        "}",
        "",
        "# write_progress <current_pack> <ingested> <total>",
        "# Atomic JSON write: stage to .tmp then mv onto target. Readers either",
        "# see the prior snapshot or the new one, never a torn write.",
        "write_progress() {",
        '    cat > "$PROGRESS_TMP" <<JSON',
        '{"current_pack": "$1", "packs_ingested": $2, "packs_total": $3, "updated_at": "$(date -Iseconds)"}',
        "JSON",
        '    mv "$PROGRESS_TMP" "$PROGRESS"',
        "}",
        "",
        "# --- Bootstrap decision -------------------------------------------",
        "BOOTSTRAP_NEEDED=true",
        'if [ -f "$COMPLETE" ]; then',
        "    BOOTSTRAP_NEEDED=false",
        '    echo ">> Bootstrap already complete - skipping to uvicorn"',
        "fi",
        "",
        'if [ "$BOOTSTRAP_NEEDED" = "true" ]; then',
        "    # Record bootstrap start. Content is the canonical timestamp;",
        "    # mtime is the fallback for stale-lock detection.",
        '    date -Iseconds > "$LOCK"',
        "",
        "    # Ollama installation",
        "    if ! command -v ollama &> /dev/null; then",
        '        echo ">> Installing Ollama..."',
        "        curl -fsSL https://ollama.ai/install.sh | sh",
        "    fi",
        "",
        '    echo ">> Starting Ollama..."',
        "    OLLAMA_HOST=127.0.0.1:11434 ollama serve &",
        "    OLLAMA_PID=$!",
        "",
        "    for i in $(seq 1 30); do",
        "        if curl -sf http://127.0.0.1:11434 > /dev/null 2>&1; then",
        '            echo ">> Ollama is ready"',
        "            break",
        "        fi",
        "        sleep 1",
        "    done",
        "",
        '    echo ">> Checking embedding model..."',
        "    if ! ollama list | grep -q qwen3-embedding; then",
        '        echo ">> Pulling qwen3-embedding:0.6b..."',
        '        cat > "$PROGRESS_TMP" <<JSON',
        '{"current_pack": "qwen3-embedding:0.6b", "packs_ingested": 0, "packs_total": 1, "phase": "model_pull", "model": "qwen3-embedding:0.6b", "status": "in_progress", "updated_at": "$(date -Iseconds)"}',
        "JSON",
        '        mv "$PROGRESS_TMP" "$PROGRESS"',
        "        ollama pull qwen3-embedding:0.6b",
        '        echo "Model pull complete"',
        "    fi",
        "",
        '    echo ">> Running migrations..."',
        "    uv run python -m agentalloy.migrate",
        "fi",
        "",
        "# --- SIGTERM trap (covers Ollama + uvicorn) -----------------------",
        "trap 'kill ${OLLAMA_PID:-} ${UVICORN_PID:-} 2>/dev/null; exit 0' SIGTERM",
        "",
        'if [ "$BOOTSTRAP_NEEDED" = "true" ]; then',
    ]

    if has_packs:
        lines.extend(
            [
                f"    PACK_LIST=({pack_array_literal})",
                f"    TOTAL={packs_total}",
                "    INGESTED=0",
                '    if [ -f "$CHECKPOINTS" ]; then',
                "        # Count previously-ingested packs (corrupt file ⇒ 0).",
                '        INGESTED=$(grep -c "pack_ingested" "$CHECKPOINTS" 2>/dev/null || echo 0)',
                "    fi",
                '    for pack in "${PACK_LIST[@]}"; do',
                '        if pack_already_done "$pack"; then',
                '            echo ">> Pack $pack already ingested - skipping"',
                "            continue",
                "        fi",
                '        write_progress "$pack" "$INGESTED" "$TOTAL"',
                '        echo ">> Installing pack: $pack"',
                "        # install-packs writes its own lock so a host-side",
                "        # `agentalloy install-packs` cannot collide mid-ingest.",
                '        touch "$INSTALL_LOCK"',
                '        uv run agentalloy install-packs --packs "$pack" --no-restart',
                '        rm -f "$INSTALL_LOCK"',
                '        printf \'{"step": "pack_ingested", "pack": "%s", "at": "%s"}\\n\' "$pack" "$(date -Iseconds)" >> "$CHECKPOINTS"',
                "        INGESTED=$((INGESTED + 1))",
                "    done",
                '    write_progress "" "$INGESTED" "$TOTAL"',
            ]
        )
    else:
        # No explicit packs — install always-on packs (core, documentation,
        # engineering, performance) so the container is functional.
        # `install-packs` with no --packs arg installs always-on packs in
        # non-TTY mode (see install_packs.py:400-401).
        lines.extend(
            [
                '    echo ">> No explicit packs specified — installing always-on packs"',
                "    uv run agentalloy install-packs --no-restart",
            ]
        )

    lines.extend(
        [
            "",
            "    # Mark bootstrap complete and clear the lock.",
            '    rm -f "$LOCK"',
            '    touch "$COMPLETE"',
            '    echo ">> Bootstrap complete"',
            "fi",
            "",
            "# Start uvicorn AFTER bootstrap completes to avoid Ladybug lock conflicts.",
            'echo ">> Starting uvicorn..."',
            "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &",
            "UVICORN_PID=$!",
            "",
            "# Block on uvicorn — its exit is the container's exit.",
            "wait $UVICORN_PID",
        ]
    )

    return "\n".join(lines) + "\n"


def _cleanup_temp_entrypoint(entrypoint: Path) -> None:
    """Remove the temporary entrypoint file.

    Idempotent — silently ignores missing files.

    Parameters
    ----------
    entrypoint : Path
        Path to the entrypoint script to remove.
    """
    if entrypoint.exists():
        entrypoint.unlink()


# ---------------------------------------------------------------------------
# Container run
# ---------------------------------------------------------------------------


def _run_container(
    runtime: str,
    entrypoint: Path,
    packs: str,
    image_ref: str | None = None,
) -> int:
    """Run the agentalloy container with volumes, env, and port mapping.

    Runs ``{runtime} run -d --name agentalloy`` with (``--replace`` for Podman only;
    Docker uses a preceding ``docker rm -f agentalloy`` instead) with:

    * Volume mounts: ``agentalloy-data:/app/data`` and ``~/.ollama:/root/.ollama``
    * Env vars: ``ENTRYPOINT``, ``LADYBUG_DB_PATH``,
      ``DUCKDB_PATH``, ``LOG_LEVEL``
    * Port mapping: ``-p 47950:47950``

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).
    entrypoint : Path
        Path to the generated entrypoint script (mounted as a volume).
    packs : str
        Comma-separated list of packs to install.
    image_ref : str | None
        Image reference to run. Defaults to ``ghcr.io/nrmeyers/agentalloy:latest``.

    Returns
    -------
    int
        Exit code from the runtime command.
    """
    env = {
        "ENTRYPOINT": str(entrypoint),
        "LADYBUG_DB_PATH": "/app/data/ladybug",
        "DUCKDB_PATH": "/app/data/skills.duck",
        "LOG_LEVEL": "info",
    }
    env_cmd: list[str] = []
    for k, v in env.items():
        env_cmd.extend(["-e", f"{k}={v}"])

    home = Path.home()
    image = image_ref or _DEFAULT_IMAGE

    # `--replace` is a Podman extension; Docker does not support it.
    # For Docker, explicitly remove any existing container before running.
    if runtime == "docker":
        subprocess.run(
            [runtime, "rm", "-f", "agentalloy"],
            check=False,
            capture_output=True,
        )

    cmd = [
        runtime,
        "run",
        *(["--replace"] if runtime != "docker" else []),
        "-d",
        # restart-on-boot parity with the retired compose path (which set
        # `restart: unless-stopped`); the single-container model is the
        # persistent deployment, so the container must survive a reboot.
        "--restart",
        "unless-stopped",
        "--name",
        "agentalloy",
        "-p",
        "47950:47950",
        "-v",
        "agentalloy-data:/app/data",
        "-v",
        f"{home}/.ollama:/root/.ollama",
        "-v",
        f"{entrypoint}:/app/entrypoint.sh:ro",
        *env_cmd,
        image,
        "/app/entrypoint.sh",
    ]

    try:
        subprocess.run(cmd, check=True, timeout=300)
        return 0
    except subprocess.CalledProcessError as exc:
        # Surface a port-reservation hint when the runtime reports a bind
        # failure — the most common cause is an Exited container from a prior
        # crashed install that still holds the rootlessport reservation even
        # though nothing actually listens on the host. stderr is NOT captured
        # by this subprocess.run (it streams to the user's terminal), so
        # exc.stderr is normally None — rely on the exit code: rootlessport
        # bind failures exit 126 (observed 2026-06-10). Keep the stderr check
        # for callers/tests that do capture it.
        stderr_text = ""
        if exc.stderr is not None:
            stderr_text = (
                exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            )
        bind_failure = (
            exc.returncode == 126
            or "address already in use" in stderr_text
            or "bind" in stderr_text
        )
        if bind_failure:
            _print(
                f"  [red]Port bind failed — another container may be publishing "
                f"port 47950.[/red]\n"
                f"  [dim]Run `{runtime} ps -a` and remove conflicting containers, "
                f"then re-run setup.[/dim]"
            )
        return exc.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]Container run timed out after 300s[/red]")
        return 1


# ---------------------------------------------------------------------------
# Readiness polling (fast-start + bootstrap state)
# ---------------------------------------------------------------------------


def _list_conflicting_containers(
    runtime: str,
    container_name: str = "agentalloy",
    port: int = 47950,
) -> list[tuple[str, str]]:
    """Return [(name, status), ...] for containers that would block a fresh start.

    Merges three detection strategies, deduped by container name:
      1. Name-exact match — any container (running or exited) named *container_name*.
      2. Port match — any container publishing *port* on the host side.

    All subprocess failures collapse to an empty contribution so the wizard
    proceeds without crashing if the runtime is unavailable.

    Parameters
    ----------
    runtime : str
        Container runtime binary path (e.g. ``"podman"`` or ``"docker"``).
    container_name : str
        Exact container name to search for. Default ``"agentalloy"``.
    port : int
        Host-side port to search for. Default ``47950``.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _record(name: str, status: str) -> None:
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            out.append((name, status.strip() or "unknown"))

    # Strategy 1: name-exact match (catches single-container GHCR installs
    # that carry no compose label — the bug scenario).
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, runtime from caller
            [
                runtime,
                "ps",
                "-a",
                "--filter",
                f"name=^{container_name}$",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "\t" not in line:
                    continue
                name, _, status = line.partition("\t")
                _record(name, status)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeDecodeError):
        pass

    # Strategy 2: port match — catches containers with a different name that
    # still hold the host-port reservation (e.g. a renamed install).
    try:
        result = subprocess.run(  # noqa: S603
            [
                runtime,
                "ps",
                "-a",
                "--filter",
                f"publish={port}",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "\t" not in line:
                    continue
                name, _, status = line.partition("\t")
                _record(name, status)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeDecodeError):
        pass

    return out


def _check_container_running(
    runtime: str,
    container_name: str = "agentalloy",
) -> bool:
    """Check whether the named container is currently running.

    Returns True if the container appears in ``{runtime} ps`` output,
    False otherwise (container not started, stopped, or crashed).
    """
    try:
        result = subprocess.run(
            [
                runtime,
                "ps",
                "--format",
                "{{.Names}}",
                "--filter",
                f"name={container_name}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return container_name in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeDecodeError):
        return False


def _tail_container_logs(
    runtime: str,
    container_name: str = "agentalloy",
    tail_lines: int = 10,
) -> str:
    """Return the last *tail_lines* lines of the container's stdout.

    Used to surface bootstrap progress to the user during readiness polling.
    Returns an empty string on any failure.
    """
    try:
        result = subprocess.run(
            [
                runtime,
                "logs",
                "--tail",
                str(tail_lines),
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def _container_state(runtime: str, container_name: str = "agentalloy") -> str:
    """Return the container's ``.State.Status`` (lowercase), or ``""`` on any failure.

    Failures (runtime missing, container not found, timeout) intentionally
    map to ``""`` so callers treat liveness as unknown rather than dead —
    the readiness timeout remains the backstop.
    """
    try:
        result = subprocess.run(
            [runtime, "inspect", "-f", "{{.State.Status}}", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip().lower()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _wait_for_readiness(
    port: int,
    timeout: int = 1800,
    *,
    runtime: str | None = None,
    container_name: str = "agentalloy",
    poll_interval: float = 30.0,
    on_progress: Any = None,
    stream_logs: bool = True,
) -> bool:
    """Poll ``/readiness`` until bootstrap completes or we time out.

    The endpoint reports one of:

    * ``ready``       — bootstrap done; return True.
    * ``warming_up``  — still bootstrapping; surface progress (if a callback
                        is supplied) and keep polling.
    * ``error``       — fatal (e.g. ``stale_lock``); return False.

    Uses a **first-success** model: connection errors before any successful
    ``/readiness`` response are expected during bootstrap and don't count
    toward failure. Only consecutive errors **after** the first successful
    response (meaning the container is alive but /readiness reports
    warming_up) are counted toward the 3-strike limit. During bootstrap the
    container's liveness is checked on every failed poll — an ``exited`` or
    ``dead`` container fails immediately with its log tail rather than
    burning the timeout — and log streaming + progress callbacks run from
    the first poll so the model pull and pack ingest are visible before
    uvicorn is up.

    ``timeout`` defaults to 1800 s (30 min) because full pack ingest +
    re-embed runs 15-25 min; callers pass shorter values for limited packs
    or re-installs.

    Parameters
    ----------
    port : int
        Port on which the container exposes ``/readiness``.
    timeout : int
        Max seconds to wait. Default 1800 (all-packs); pass 300 for
        limited packs or re-installs.
    runtime, container_name :
        Used to call ``_get_bootstrap_progress`` for the on_progress hook.
    poll_interval : float
        Seconds between polls. Default 30 s balances responsiveness with
        the cost of repeatedly spawning ``runtime exec`` for progress.
    on_progress : callable(dict) | None
        Optional callback invoked once per poll with the parsed readiness
        body (status + progress). Lets the caller render a live spinner.
    stream_logs : bool
        If True, stream container logs (including ``ollama pull`` output)
        to the user during the polling loop. Defaults to True.
    """
    import json as _json
    import time as _time
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/readiness"
    start = _time.monotonic()
    first_success = False  # True once we've seen the first 200 response
    consecutive_errors = 0
    last_tail: str = ""  # Last container logs seen (for diffing new lines)

    while True:
        elapsed = _time.monotonic() - start
        body: dict[str, Any] | None = None
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = _json.loads(resp.read().decode())
            # First successful connection — container is alive.
            first_success = True
            consecutive_errors = 0
        except (urllib.error.URLError, OSError, _json.JSONDecodeError):
            # Only count errors after we've seen the container alive at least once.
            # Before first success, connection errors are expected during bootstrap.
            if first_success:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return False

        # A container that exited during bootstrap (e.g. ``ollama pull``
        # network failure under ``set -e``) will never serve /readiness —
        # fail fast with its log tail instead of silently burning the whole
        # timeout. Unknown state (inspect failure) keeps polling; the
        # timeout remains the backstop.
        if body is None and runtime is not None:
            state = _container_state(runtime, container_name)
            if state in ("exited", "dead"):
                _print(
                    f"  [red]Container '{container_name}' {state} during bootstrap — "
                    "aborting readiness wait.[/red]"
                )
                tail = _tail_container_logs(runtime, container_name, tail_lines=15)
                if tail:
                    _print("  [dim]Last container log lines:[/dim]")
                    _print(tail.rstrip())
                _print(f"  [dim]Full logs: {runtime} logs {container_name}[/dim]")
                return False

        # Stream container logs (e.g. ``ollama pull`` output) so the user
        # can see what's happening inside the container during bootstrap —
        # including before uvicorn is up, when /readiness still refuses
        # connections but the entrypoint is already logging.
        if stream_logs and runtime is not None:
            new_tail = _tail_container_logs(runtime, container_name)
            if new_tail and new_tail != last_tail:
                new_lines = (
                    new_tail[len(last_tail) :] if new_tail.startswith(last_tail) else new_tail
                )
                _print(new_lines.rstrip())
                last_tail = new_tail

        status = body.get("status") if body is not None else None
        if on_progress is not None:
            # Caller wants progress updates. Best-effort enrichment from the
            # in-container progress file via runtime exec, in addition to
            # whatever /readiness reported. During bootstrap (no /readiness
            # yet) the progress file is the only signal — surface it with a
            # synthetic warming_up status so heartbeats render.
            extra: dict[str, Any] = {}
            if runtime is not None:
                extra = _get_bootstrap_progress(runtime, container_name)
            with contextlib.suppress(Exception):
                on_progress(
                    {
                        "status": status if status is not None else "warming_up",
                        "progress": (body.get("progress") or {}) if body is not None else {},
                        "extra": extra,
                        "elapsed": elapsed,
                    }
                )

        if status == "ready":
            return True
        if status == "error":
            return False

        if elapsed >= timeout:
            return False
        _time.sleep(poll_interval if body is not None else min(poll_interval, 5.0))


def _get_bootstrap_progress(runtime: str, container_name: str = "agentalloy") -> dict[str, Any]:
    """Return the parsed ``.bootstrap-progress`` JSON, or ``{}`` on any failure.

    Uses ``{runtime} exec <name> cat /app/.bootstrap-progress``. Every failure
    mode (container stopped, file missing, JSON malformed, runtime missing)
    collapses to an empty dict so the caller can fall back to elapsed-time
    display without branching on error kind.
    """
    import json as _json

    try:
        result = subprocess.run(
            [runtime, "exec", container_name, "cat", "/app/.bootstrap-progress"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}
    raw = result.stdout
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if not raw:
        return {}
    try:
        parsed = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
