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
    9. Trap SIGTERM for graceful shutdown (only if Ollama was started).
    10. Start uvicorn on ``0.0.0.0:47950``.

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
        "        ollama pull qwen3-embedding:0.6b",
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

    Runs ``{runtime} run --replace -d --name agentalloy`` with:

    * Volume mounts: ``agentalloy-data:/app/data`` and ``~/.ollama:/root/.ollama``
    * Env vars: ``AGENTIALLOY_PACKS``, ``ENTRYPOINT``, ``LADYBUG_DB_PATH``,
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
        "AGENTIALLOY_PACKS": packs,
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
    cmd = [
        runtime,
        "run",
        "--replace",
        "-d",
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
        return exc.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]Container run timed out after 300s[/red]")
        return 1


# ---------------------------------------------------------------------------
# Readiness polling (fast-start + bootstrap state)
# ---------------------------------------------------------------------------


def _wait_for_readiness(
    port: int,
    timeout: int = 1800,
    *,
    runtime: str | None = None,
    container_name: str = "agentalloy",
    poll_interval: float = 30.0,
    on_progress: Any = None,
) -> bool:
    """Poll ``/readiness`` until bootstrap completes or we time out.

    The endpoint reports one of:

    * ``ready``       — bootstrap done; return True.
    * ``warming_up``  — still bootstrapping; surface progress (if a callback
                        is supplied) and keep polling.
    * ``error``       — fatal (e.g. ``stale_lock``); return False.

    Connection errors are treated as "container not yet up" for the first
    ~30 s, then as "container died" — we return False so the caller can
    surface the container's own logs instead of waiting out the full
    timeout. ``timeout`` defaults to 1800 s (30 min) because full pack
    ingest + re-embed runs 15-25 min; callers pass 300 s for limited packs.

    Parameters
    ----------
    port : int
        Port on which the container exposes ``/readiness``.
    timeout : int
        Max seconds to wait. Default 1800 (all-packs); pass 300 for
        limited packs.
    runtime, container_name :
        Used to call ``_get_bootstrap_progress`` for the on_progress hook.
    poll_interval : float
        Seconds between polls. Default 30 s balances responsiveness with
        the cost of repeatedly spawning ``runtime exec`` for progress.
    on_progress : callable(dict) | None
        Optional callback invoked once per poll with the parsed readiness
        body (status + progress). Lets the caller render a live spinner.
    """
    import json as _json
    import time as _time
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/readiness"
    start = _time.monotonic()
    grace_window = 30.0  # tolerate connection errors during initial start
    consecutive_errors = 0

    while True:
        elapsed = _time.monotonic() - start
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = _json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, _json.JSONDecodeError):
            # If we never saw a 200 and the grace window has passed, give up.
            if elapsed > grace_window:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return False
            if elapsed >= timeout:
                return False
            _time.sleep(min(poll_interval, 5.0))
            continue

        consecutive_errors = 0
        status = body.get("status")
        if on_progress is not None:
            # Caller wants progress updates. Best-effort enrichment from the
            # in-container progress file via runtime exec, in addition to
            # whatever /readiness reported.
            extra: dict[str, Any] = {}
            if runtime is not None:
                extra = _get_bootstrap_progress(runtime, container_name)
            with contextlib.suppress(Exception):
                on_progress(
                    {
                        "status": status,
                        "progress": body.get("progress") or {},
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
        _time.sleep(poll_interval)


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
