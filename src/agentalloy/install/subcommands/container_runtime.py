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


def _has_assets(d: Path) -> bool:
    """Return True if *d* looks like a valid agentalloy build context.

    Checks for the presence of both a compose file and a build file
    (Containerfile or Dockerfile).
    """
    default_compose = "compose.yaml"
    has_build_file = (d / "Containerfile").exists() or (d / "Dockerfile").exists()
    return (d / default_compose).exists() and has_build_file


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
# Build context location
# ---------------------------------------------------------------------------


def _locate_build_context() -> Path | None:
    """Locate the agentalloy build context for container image builds.

    Search order:

    1. **cwd** — if the user ran setup from inside the clone.
    2. **parents[4] of __file__** — editable install (points at repo root).
    3. **auto-clone** — clone into ``~/.cache/agentalloy/repo`` if git is
       available (for users who installed via ``uv tool install agentalloy``).

    Returns
    -------
    Path | None
        Path to ``compose.yaml`` in the found context, or ``None`` if all
        strategies fail.
    """
    default_compose = "compose.yaml"

    def _ensure_cached_repo() -> Path | None:
        """Clone (or refresh) the agentalloy repo into ~/.cache/agentalloy/repo.

        Returns the cache dir on success, None on failure. Uses --depth=1 so the
        clone is fast (~few MB). On refresh, hard-resets to origin/main so any
        local edits or stale state in the cache don't break the build context.
        """
        cache_dir = Path.home() / ".cache" / "agentalloy" / "repo"
        if shutil.which("git") is None:
            _print(
                "  [red]git not found on PATH — cannot clone the agentalloy repo "
                "for the build context.[/red]"
            )
            return None
        repo_url = "https://github.com/nrmeyers/agentalloy.git"
        # If the cache dir exists but isn't a valid git checkout (no .git/
        # — possibly a partial clone, leftover files, or a manually-placed
        # directory), `git clone <url> <dest>` would fail with "destination
        # path already exists and is not an empty directory". Nuke it so
        # the clone branch below can recreate cleanly.
        if cache_dir.exists() and not (cache_dir / ".git").exists():
            _print(
                f"  [yellow]-> Cache dir {cache_dir} exists but isn't a git "
                "checkout; recreating.[/yellow]"
            )
            try:
                shutil.rmtree(cache_dir)
            except OSError as exc:
                _print(f"  [red]Could not remove stale cache dir: {exc}[/red]")
                return None
        try:
            if (cache_dir / ".git").exists():
                _print(f"  [dim]-> Refreshing cached repo at {cache_dir}[/dim]")
                subprocess.run(
                    ["git", "-C", str(cache_dir), "fetch", "--depth=1", "origin", "main"],
                    check=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "-C", str(cache_dir), "reset", "--hard", "origin/main"],
                    check=True,
                    timeout=60,
                )
            else:
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                _print(f"  [dim]-> Cloning {repo_url} into {cache_dir}[/dim]")
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--depth=1",
                        "--branch=main",
                        repo_url,
                        str(cache_dir),
                    ],
                    check=True,
                    timeout=180,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            _print(f"  [red]git clone/fetch failed: {exc}[/red]")
            return None
        if not _has_assets(cache_dir):
            _print(
                f"  [red]Cached repo at {cache_dir} is missing {default_compose} "
                "or Containerfile after clone.[/red]"
            )
            return None
        return cache_dir

    # Strategy 1: cwd
    cwd = Path.cwd()
    if _has_assets(cwd):
        return cwd / default_compose

    # Strategy 2: parents[4] of __file__ (editable install)
    module_file = Path(__file__).resolve()
    editable_root = module_file.parents[4]
    if _has_assets(editable_root):
        return editable_root / default_compose

    # Strategy 3: auto-clone
    cached = _ensure_cached_repo()
    if cached is not None:
        return cached / default_compose

    return None


# ---------------------------------------------------------------------------
# Image build
# ---------------------------------------------------------------------------


def _build_image(runtime: str, context: Path) -> int:
    """Build the agentalloy container image.

    Runs ``{runtime} build -t agentalloy:local -f Containerfile <context>``
    with a 600-second timeout.  Build output is captured and written to a
    log file on failure for debugging.  Returns the exit code (0 on success).

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).
    context : Path
        Path to the build context directory.

    Returns
    -------
    int
        Exit code from the runtime command.
    """
    log_path = Path(tempfile.gettempdir()) / "agentalloy-build.log"
    try:
        subprocess.run(
            [
                runtime,
                "build",
                "-t",
                "agentalloy:local",
                "-f",
                "Containerfile",
                str(context),
            ],
            check=True,
            timeout=600,
            capture_output=True,
        )
        return 0
    except subprocess.CalledProcessError as exc:
        # Write captured build output to log file for debugging
        log_path.write_text(
            f"=== agentalloy build failed (exit {exc.returncode}) ===\n"
            f"Command: {shlex.join(exc.cmd)}\n\n"
            f"--- stdout ---\n{(exc.output or b'').decode(errors='replace')}\n"
            f"--- stderr ---\n{(exc.stderr or b'').decode(errors='replace')}\n"
        )
        _print(f"  [red]Build failed (exit {exc.returncode}) — log: {log_path}[/red]")
        return exc.returncode
    except subprocess.TimeoutExpired:
        _print("  [red]Build timed out after 600s[/red]")
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


def _run_container(runtime: str, entrypoint: Path, packs: str) -> int:
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
        "agentalloy:local",
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
