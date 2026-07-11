"""``container_runtime`` — runtime detection and build context location.

Provides utilities for container deployment: detecting podman/docker on PATH
and locating the agentalloy build context (for building the container image).
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from agentalloy.install import env_forwarding
from agentalloy.install.ingest_secret import SECRET_ENV, resolve_ingest_secret

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

try:
    from rich.console import Console as _Console  # type: ignore[import-untyped]

    _console: _Console | None = _Console(force_terminal=True, soft_wrap=True)  # type: ignore[assignment]
except ImportError:
    _console = None  # type: ignore[assignment]


def _print(*args: Any, **kwargs: Any) -> None:
    """Print with Rich markup if available, plain stdout otherwise."""
    if _console is not None:
        _console.print(*args, **kwargs)  # type: ignore[union-attr, arg-type]
    else:
        print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Runtime detection
# ---------------------------------------------------------------------------


def _runtime_is_functional(binary: str) -> bool:
    """True if ``<binary> info`` succeeds — i.e. the runtime can actually reach
    its daemon/machine.

    Presence on PATH is not enough: on macOS the ``podman`` CLI is commonly
    installed (brew / leftover) without a running ``podman machine``, and
    ``docker`` is present while Docker Desktop is stopped. ``info`` connects to
    the backend, so it distinguishes a usable runtime from a dangling CLI.
    ``version`` is unsuitable — podman is daemonless and ``podman version``
    returns 0 even with no machine.
    """
    try:
        result = subprocess.run(
            [binary, "info"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
            # cwd="/" — immune to the rootless-podman bind-mount-teardown race
            # that can transiently break getcwd() for callers whose cwd is
            # under the bind-mounted projects root (issue #303).
            cwd="/",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _detect_functional_runtimes() -> list[str]:
    """Runtimes that are both present on PATH and functional, in preference
    order (podman first, docker fallback)."""
    return [
        candidate
        for candidate in ("podman", "docker")
        if shutil.which(candidate) is not None and _runtime_is_functional(candidate)
    ]


def _detect_runtime_binary() -> str | None:
    """Best single container runtime to use.

    Resolution order:

    1. The first *functional* runtime (``<rt> info`` succeeds), podman preferred.
    2. Otherwise the first runtime merely *present* on PATH, so downstream
       preflight can emit a meaningful error instead of "neither found".
    3. ``None`` when neither podman nor docker is on PATH.

    Used by non-interactive callers (uninstall, upgrade, preflight,
    install_packs). The interactive setup flow uses
    :func:`_detect_functional_runtimes` directly so it can offer a choice when
    more than one runtime works.
    """
    functional = _detect_functional_runtimes()
    if functional:
        return functional[0]
    present = [c for c in ("podman", "docker") if shutil.which(c) is not None]
    return present[0] if present else None


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
                cwd="/",
            )
            _print("  [green]-> Image loaded from tarball[/green]")
            # Verify the expected image tag is present after load (handles tarball tag mismatch)
            result = subprocess.run(
                [runtime, "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd="/",
            )
            # Also check by ID for digest-based matching as fallback
            id_result = subprocess.run(
                [runtime, "images", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd="/",
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
                timeout=1500,
                cwd="/",
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
            _print("  [red]Image pull timed out after 1500s[/red]")
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
            cwd="/",
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").lower()
        if "already exists" in stderr:
            return
        raise


# ---------------------------------------------------------------------------
# Entrypoint generation
# ---------------------------------------------------------------------------


def _generate_entrypoint(packs: str) -> Path:
    """Generate an entrypoint wrapper script and return its temp file path.

    The entrypoint is a bash script that handles in-container bootstrap:

    1. Check if ``$APP_DIR/.bootstrap-complete`` exists — if so, skip to uvicorn.
    2. Download both GGUF models (embed + reranker) into ``$APP_DIR/data/models``
       on first boot if missing (``curl`` from the verified Hugging Face URLs).
    3. Start the embed ``llama-server --embeddings`` on ``127.0.0.1:47951`` and
       the reranker ``llama-server`` (completions mode) on ``127.0.0.1:47952``,
       both in the background.
    4. Poll ``:47951/health`` and ``:47952/health`` until both are ready.
    5. Run migrations (``uv run python -m agentalloy.migrate``).
    6. If *packs* is non-empty, run ``uv run agentalloy install-packs --packs <packs>``
       for each pack. If *packs* is empty, run ``uv run agentalloy install-packs``
       (no --packs) to install always-on packs (core, documentation, engineering,
       performance).
    7. Create the ``$APP_DIR/.bootstrap-complete`` flag file.
    8. Writes ``.bootstrap-progress`` with ``phase="model_download"`` before
       fetching the GGUFs, so the host-side readiness polling can surface the
       download progress to the user.
    9. Prints ``Model download complete`` to stdout after the models finish
       downloading — the host-side log streamer uses this as a transition
       marker to switch from log streaming to readiness polling.
    10. Trap SIGTERM/SIGINT for graceful shutdown (kills both llama-server PIDs).
    11. Start uvicorn on ``0.0.0.0:47950``.

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
       DuckDB single-writer lock conflict that occurred when uvicorn opened
       the skill store before pack ingestion finished.
    6. Prebuilt-corpus seed: if the image carries ``/app/corpus-seed``
       (CI bakes a fully ingested + embedded corpus plus
       ``corpus-stamp.json``) and the data volume has no corpus yet, the
       seed is copied in and the per-pack ingest loop is skipped entirely.
       The llama-server models still download/start — query embedding and
       intent reranking need them at runtime.
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
        "# --- Prebuilt corpus seed ------------------------------------------",
        "# CI-built images carry a fully ingested + embedded corpus under",
        "# /app/corpus-seed (.github/workflows/container-build.yml). When it",
        "# is present and the data volume has no corpus yet, copy it in and",
        "# skip per-pack ingest + re-embed — first run drops from ~30 min of",
        "# CPU embedding to seconds. llama-server setup stays unconditional:",
        "# query embedding at compose time still needs the model at runtime.",
        'SEED_DIR="${SEED_DIR:-/app/corpus-seed}"',
        'VOL_STAMP="$APP_DIR/data/corpus-stamp.json"',
        "CORPUS_SEEDED=false",
        "",
        "# stamp_value <file> <key> - read a value from the flat corpus-stamp.json.",
        "stamp_value() {",
        r'    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$1" 2>/dev/null | head -1',
        "}",
        "",
        "# (Re-)seed the corpus from the image: on an empty volume (first run) or",
        "# when the image corpus differs (packs_hash / embedding_dim) so that",
        "# `agentalloy upgrade` self-heals from the fast prebuilt seed. Runs every",
        "# boot (not just bootstrap) so upgrades, which keep .bootstrap-complete,",
        "# still refresh.",
        "NEED_SEED=false",
        'if [ -f "$SEED_DIR/corpus-stamp.json" ]; then',
        '    if [ ! -f "$APP_DIR/data/agentalloy.duck" ]; then',
        "        NEED_SEED=true",
        '    elif [ ! -f "$VOL_STAMP" ]; then',
        "        # Corpus present but unstamped (e.g. a pre-stamp volume, or one",
        "        # whose stamp was lost): we can't verify it matches the image, so",
        "        # re-seed from the authoritative corpus rather than trust a",
        "        # partial always-on reconcile that leaves other packs stale.",
        "        NEED_SEED=true",
        '        echo ">> Volume corpus has no stamp - re-seeding from image to verify"',
        '    elif [ "$(stamp_value "$SEED_DIR/corpus-stamp.json" packs_hash)" != "$(stamp_value "$VOL_STAMP" packs_hash)" ] \\',
        '         || [ "$(stamp_value "$SEED_DIR/corpus-stamp.json" embedding_dim)" != "$(stamp_value "$VOL_STAMP" embedding_dim)" ]; then',
        "        NEED_SEED=true",
        '        echo ">> Image corpus differs from volume (upgrade) - re-seeding"',
        "    fi",
        "fi",
        "",
        'if [ "$NEED_SEED" = "true" ]; then',
        '    echo ">> Seeding prebuilt corpus from image (skipping pack ingest + re-embed)"',
        '    mkdir -p "$APP_DIR/data"',
        '    rm -rf "$APP_DIR/data/agentalloy.duck" "$APP_DIR/data/fragments.lance"',
        '    cp "$SEED_DIR/agentalloy.duck" "$APP_DIR/data/agentalloy.duck"',
        '    cp -a "$SEED_DIR/fragments.lance" "$APP_DIR/data/fragments.lance"',
        '    cp "$SEED_DIR/corpus-stamp.json" "$VOL_STAMP"',
        "    CORPUS_SEEDED=true",
        "    # Surface the seed to host-side readiness polling (same atomic",
        "    # tmp+mv pattern as the model_pull phase).",
        '    cat > "$PROGRESS_TMP" <<JSON',
        '{"phase": "corpus_seeded", "current_pack": "", "packs_ingested": 0, "packs_total": 0, "updated_at": "$(date -Iseconds)"}',
        "JSON",
        '    mv "$PROGRESS_TMP" "$PROGRESS"',
        "fi",
        "",
        "# --- llama.cpp model + server config -------------------------------",
        "# Two llama-server daemons back the runtime: an embed server on 47951",
        "# (--embeddings, query embedding at compose time) and a reranker server",
        "# on 47952 (completions mode, /v1/completions with logprobs for the",
        "# intent classifier). Both GGUFs are downloaded on first boot into the",
        "# data volume so they persist across restarts.",
        'MODELS_DIR="$APP_DIR/data/models"',
        'EMBED_GGUF="$MODELS_DIR/nomic-embed-text-v1.5.Q8_0.gguf"',
        'RERANK_GGUF="$MODELS_DIR/Qwen3-Reranker-0.6B-Q8_0.gguf"',
        'EMBED_URL="https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"',
        'RERANK_URL="https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/resolve/main/qwen3-reranker-0.6b-q8_0.gguf"',
        "",
        'if [ "$BOOTSTRAP_NEEDED" = "true" ]; then',
        "    # Record bootstrap start. Content is the canonical timestamp;",
        "    # mtime is the fallback for stale-lock detection.",
        '    date -Iseconds > "$LOCK"',
        "",
        '    mkdir -p "$MODELS_DIR"',
        '    if [ ! -f "$EMBED_GGUF" ] || [ ! -f "$RERANK_GGUF" ]; then',
        '        echo ">> Downloading llama.cpp GGUF models..."',
        '        cat > "$PROGRESS_TMP" <<JSON',
        '{"current_pack": "gguf-models", "packs_ingested": 0, "packs_total": 1, "phase": "model_download", "status": "in_progress", "updated_at": "$(date -Iseconds)"}',
        "JSON",
        '        mv "$PROGRESS_TMP" "$PROGRESS"',
        '        if [ ! -f "$EMBED_GGUF" ]; then',
        '            echo ">> Fetching embed model (nomic-embed-text-v1.5-Q8_0)..."',
        '            curl -fsSL -o "$EMBED_GGUF" "$EMBED_URL" \\',
        "                --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30",
        "        fi",
        '        if [ ! -f "$RERANK_GGUF" ]; then',
        '            echo ">> Fetching reranker model (Qwen3-Reranker-0.6B-Q8_0)..."',
        '            curl -fsSL -o "$RERANK_GGUF" "$RERANK_URL" \\',
        "                --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30",
        "        fi",
        '        echo "Model download complete"',
        "    fi",
        "fi",
        "",
        "# --- Start the llama-server daemons (every boot) -------------------",
        "# These are long-lived runtime daemons, not bootstrap-only steps: even",
        "# after .bootstrap-complete, query embedding + intent reranking need",
        "# them up. Start them before uvicorn so /readiness reflects a usable",
        "# service.",
        'echo ">> Starting embed llama-server on 47951..."',
        'llama-server --embeddings --pooling mean --ubatch-size 2048 --host 127.0.0.1 --port 47951 -m "$EMBED_GGUF" &',
        "EMBED_PID=$!",
        'echo ">> Starting reranker llama-server on 47952..."',
        "# CPU-optimal slot config (--parallel 1 -c 2048): the container llama build",
        "# is CPU-only, and fewer slots = MORE throughput on CPU (OpenMP contention",
        "# dominates multi-slot; measured warm single ~145ms vs ~600ms+ unpinned —",
        "# same data as install/presets/cpu.yaml + start_rerank_server).",
        'llama-server --parallel 1 -c 2048 --host 127.0.0.1 --port 47952 -m "$RERANK_GGUF" &',
        "RERANK_PID=$!",
        "",
        'echo ">> Waiting for llama-server health (47951 + 47952)..."',
        "for i in $(seq 1 120); do",
        "    EMBED_OK=false",
        "    RERANK_OK=false",
        "    curl -sf http://127.0.0.1:47951/health > /dev/null 2>&1 && EMBED_OK=true",
        "    curl -sf http://127.0.0.1:47952/health > /dev/null 2>&1 && RERANK_OK=true",
        '    if [ "$EMBED_OK" = "true" ] && [ "$RERANK_OK" = "true" ]; then',
        '        echo ">> llama-server ready (embed + reranker)"',
        "        break",
        "    fi",
        "    sleep 1",
        "done",
        "",
        'if [ "$BOOTSTRAP_NEEDED" = "true" ]; then',
        '    echo ">> Running migrations..."',
        "    uv run python -m agentalloy.migrate",
        "fi",
        "",
        "# --- SIGTERM/SIGINT trap (covers llama-servers + uvicorn) ----------",
        (
            "trap 'kill ${EMBED_PID:-} ${RERANK_PID:-} ${UVICORN_PID:-} "
            "2>/dev/null; exit 0' SIGTERM SIGINT"
        ),
        "",
        "# Pack ingest runs only when there is no corpus to start from: not seeded",
        "# this boot (CORPUS_SEEDED) AND no existing volume corpus (agentalloy.duck). A",
        "# reused/populated volume is left to the seed logic above, so we never run a",
        "# partial always-on reconcile over an already-full corpus.",
        'if [ "$BOOTSTRAP_NEEDED" = "true" ] && [ "$CORPUS_SEEDED" = "false" ] \\',
        '   && [ ! -f "$APP_DIR/data/agentalloy.duck" ]; then',
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
        # No explicit packs baked in — check the AGENTALLOY_PACKS env var
        # first (set this when running a locally built image without a corpus
        # seed). If the env var is non-empty, install those packs one-by-one
        # (same checkpointed loop as the has_packs path). If the env var is
        # also empty, fall back to always-on packs (core, documentation,
        # engineering, performance).
        # `install-packs` with no --packs arg installs always-on packs in
        # non-TTY mode (see install_packs.py:400-401).
        lines.extend(
            [
                '    if [ -n "${AGENTALLOY_PACKS:-}" ]; then',
                '        IFS="," read -ra PACK_LIST <<< "$AGENTALLOY_PACKS"',
                "        TOTAL=${#PACK_LIST[@]}",
                "        INGESTED=0",
                '        if [ -f "$CHECKPOINTS" ]; then',
                '            INGESTED=$(grep -c "pack_ingested" "$CHECKPOINTS" 2>/dev/null || echo 0)',
                "        fi",
                '        for pack in "${PACK_LIST[@]}"; do',
                "            pack=$(echo \"$pack\" | tr -d ' ')",
                '            [ -z "$pack" ] && continue',
                '            if pack_already_done "$pack"; then',
                '                echo ">> Pack $pack already ingested - skipping"',
                "                continue",
                "            fi",
                '            write_progress "$pack" "$INGESTED" "$TOTAL"',
                '            echo ">> Installing pack: $pack"',
                '            touch "$INSTALL_LOCK"',
                '            uv run agentalloy install-packs --packs "$pack" --no-restart',
                '            rm -f "$INSTALL_LOCK"',
                '            printf \'{"step": "pack_ingested", "pack": "%s", "at": "%s"}\\n\' "$pack" "$(date -Iseconds)" >> "$CHECKPOINTS"',
                "            INGESTED=$((INGESTED + 1))",
                "        done",
                '        write_progress "" "$INGESTED" "$TOTAL"',
                "    else",
                '        echo ">> No explicit packs — installing always-on packs"',
                "        uv run agentalloy install-packs --no-restart",
                "    fi",
            ]
        )

    lines.extend(
        [
            "fi",
            "",
            "# Mark bootstrap complete and clear the lock (covers both the",
            "# pack-ingest path and the seeded-corpus path).",
            'if [ "$BOOTSTRAP_NEEDED" = "true" ]; then',
            '    rm -f "$LOCK"',
            '    touch "$COMPLETE"',
            '    echo ">> Bootstrap complete"',
            "fi",
            "",
            "# Start uvicorn AFTER bootstrap completes to avoid DuckDB single-writer lock conflicts.",
            'echo ">> Starting uvicorn..."',
            # uvicorn accepts lowercase level names only; forwarded host .env
            # values may arrive uppercase (presets historically shipped "INFO"),
            # and an invalid value crash-loops the container at startup.
            "uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 "
            "--log-level \"$(echo \"${LOG_LEVEL:-info}\" | tr '[:upper:]' '[:lower:]')\" &",
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


def resolve_projects_root() -> Path:
    """Host directory bind-mounted into the container at the identical path.

    The proxy decodes each repo's ``/proj/<token>`` back to its real host path
    and reads ``.agentalloy/`` (phase + lifecycle config) from it. In container
    mode that path only exists inside the container if the host tree is mounted
    there — so this root is mounted ``rw`` at the same absolute path, letting the
    proxy read every repo's phase state and write phase transitions back.

    Resolution order: ``AGENTALLOY_PROJECTS_ROOT`` (when set to an absolute path),
    else ``~``. The result is realpath-normalised to match the proxy's own
    ``realpath`` of the decoded token.
    """
    env_root = os.environ.get("AGENTALLOY_PROJECTS_ROOT", "").strip()
    root = env_root if env_root and os.path.isabs(env_root) else str(Path.home())
    return Path(os.path.realpath(root))


def _run_container(
    runtime: str,
    packs: str,
    image_ref: str | None = None,
    projects_root: Path | None = None,
    port: int = 47950,
) -> int:
    """Run the agentalloy container with volumes, env, and port mapping.

    Runs ``{runtime} run -d --name agentalloy`` with (``--replace`` for Podman only;
    Docker uses a preceding ``docker rm -f agentalloy`` instead) with:

    * Volume mount: ``agentalloy-data:/app/data`` (the GGUF models persist
      under ``/app/data/models`` in this same volume).
    * Volume mount: the projects root (``resolve_projects_root()``) bind-mounted
      ``rw`` at the identical host path, so the proxy can read each repo's
      ``.agentalloy/`` phase state and write phase transitions back.
    * Env vars: the baked container spec (``AGENTALLOY_PACKS``,
      ``DUCKDB_PATH``, ``FRAGMENTS_LANCE_PATH``, ``TELEMETRY_DB_PATH``,
      ``AGENTALLOY_RUNTIME_STATE_DIR``, ``LOG_LEVEL``) plus every *intent*
      key present in the host ``.env``, forwarded through the audited
      allowlist in :mod:`agentalloy.install.env_forwarding`.
    * Port mapping: ``-p 47950:47950``

    The container runs the image's **baked** ``/app/entrypoint.sh`` (the
    ENTRYPOINT/CMD declared in the Containerfile) — we deliberately do NOT
    bind-mount a host-generated entrypoint. A host bind-mount source is deleted
    after install, which made ``{runtime} start agentalloy`` (and the declared
    ``--restart unless-stopped`` on reboot) fail: the missing mount source left
    ``/app/entrypoint.sh`` empty and the container exited immediately. The baked
    script reads the pack list from the ``AGENTALLOY_PACKS`` env var instead, so
    the container is fully self-contained and survives ``start`` / reboot.

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).
    packs : str
        Comma-separated list of packs to install. Passed to the baked entrypoint
        via the ``AGENTALLOY_PACKS`` env var.
    image_ref : str | None
        Image reference to run. Defaults to ``ghcr.io/nrmeyers/agentalloy:latest``.
    port : int
        Host-side port to publish. The container-internal side always stays
        ``47950`` (that's what the baked entrypoint binds to) — only the host
        side is configurable, per ``install-state.json["port"]``.

    Returns
    -------
    int
        Exit code from the runtime command.
    """
    # mint=True always yields a value; assert keeps the env dict str-typed and
    # guarantees the container never boots with an empty ingest secret.
    ingest_secret = resolve_ingest_secret(mint=True)
    assert ingest_secret, "mint_ingest_secret must return a non-empty secret"
    env = {
        "AGENTALLOY_PACKS": packs,
        "DUCKDB_PATH": "/app/data/agentalloy.duck",
        "FRAGMENTS_LANCE_PATH": "/app/data/fragments.lance",
        "TELEMETRY_DB_PATH": "/app/data/telemetry.duck",
        # Per-turn cadence state (announced/composed/banner-turns) lives on the
        # data volume, keyed by /proj token — writing it into the repo's
        # .agentalloy/ mid-session trips harness file-watchers (Claude Code
        # flags "a background process modified <file>" to the user).
        "AGENTALLOY_RUNTIME_STATE_DIR": "/app/data/runtime-state",
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "info").lower(),
        # Corpus-ingest auth (T3): the host mints the secret and injects it here
        # so the in-container service and the host CLI converge on one value
        # without the host reading inside the volume. Baked at create like the
        # topology keys above — a rotate needs a recreate.
        SECRET_ENV: ingest_secret,
    }
    # The host .env is the single source of truth for user intent (module
    # toggles, upstream config, tuning). Intent keys forward through the
    # audited allowlist; host-topology keys never do — the baked values above
    # always win because forwarded_env() can't return them. Values bind at
    # create: a later .env edit needs a recreate (doctor flags the drift).
    forwarded = env_forwarding.forwarded_env()
    env.update(forwarded)
    for warning in env_forwarding.loopback_upstream_warnings(forwarded):
        _print(f"  [yellow]{warning}[/yellow]")
    env_cmd: list[str] = []
    for k, v in env.items():
        env_cmd.extend(["-e", f"{k}={v}"])

    # Bind-mount the projects root at its identical host path so the proxy can
    # read each repo's `.agentalloy/` phase state (the decoded `/proj/<token>`
    # path) and write transitions back. Refuse to mount the whole root fs.
    root = projects_root or resolve_projects_root()
    projects_mount: list[str] = []
    if str(root) == "/":
        _print(
            "  [red]Projects root resolved to '/' — refusing to bind-mount the whole "
            "filesystem. Set AGENTALLOY_PROJECTS_ROOT to your code root (e.g. ~/dev); "
            "phase state will be unreadable until you do.[/red]"
        )
    else:
        projects_mount = ["-v", f"{root}:{root}:rw"]
        _print(
            f"  [dim]Mounting projects root {root} (rw) so the proxy can read "
            f".agentalloy/ phase state.[/dim]"
        )
        if root == Path(os.path.realpath(Path.home())):
            _print(
                "  [dim]Tip: set AGENTALLOY_PROJECTS_ROOT to narrow the exposed tree "
                "(e.g. ~/dev) instead of all of $HOME.[/dim]"
            )

    image = image_ref or _DEFAULT_IMAGE

    # `--replace` is a Podman extension; Docker does not support it.
    # For Docker, explicitly remove any existing container before running.
    # Compare on the basename: callers pass either the bare label ("docker") or
    # the resolved `shutil.which` path (e.g. "/usr/local/bin/docker" on macOS,
    # "/opt/homebrew/bin/docker" on Apple Silicon), and an exact-string check
    # against "docker" wrongly classifies the path as non-Docker → emits the
    # Podman-only `--replace` and `docker run` fails with "unknown flag".
    is_docker = Path(runtime).name == "docker"
    if is_docker:
        subprocess.run(
            [runtime, "rm", "-f", "agentalloy"],
            check=False,
            capture_output=True,
            cwd="/",
        )

    cmd = [
        runtime,
        "run",
        *([] if is_docker else ["--replace"]),
        "-d",
        # restart-on-boot parity with the retired compose path (which set
        # `restart: unless-stopped`); the single-container model is the
        # persistent deployment, so the container must survive a reboot.
        "--restart",
        "unless-stopped",
        "--name",
        "agentalloy",
        "-p",
        f"{port}:47950",
        "-v",
        "agentalloy-data:/app/data",
        *projects_mount,
        *env_cmd,
        image,
    ]

    try:
        subprocess.run(cmd, check=True, timeout=300, cwd="/")
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
                f"port {port}.[/red]\n"
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
            cwd="/",
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
            cwd="/",
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
            cwd="/",
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
            cwd="/",
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
            cwd="/",
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
        If True, stream container logs (including the GGUF model-download
        output) to the user during the polling loop. Defaults to True.
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
        except urllib.error.HTTPError as http_err:
            # A 503 from /readiness means the service is up but the corpus is
            # unusable (degraded mode). Parse the body so the caller can surface
            # the reason rather than silently counting it as a transient error.
            if http_err.code == 503:
                first_success = True
                consecutive_errors = 0
                try:
                    body = _json.loads(http_err.read().decode())
                except (_json.JSONDecodeError, OSError):
                    body = {"status": "error", "progress": {"error": "corpus_unavailable"}}
            else:
                if first_success:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        return False
        except (urllib.error.URLError, OSError, _json.JSONDecodeError):
            # Only count errors after we've seen the container alive at least once.
            # Before first success, connection errors are expected during bootstrap.
            if first_success:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return False

        # A container that exited during bootstrap (e.g. a GGUF download
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

        # Stream container logs (e.g. the GGUF download output) so the user
        # can see what's happening inside the container during bootstrap —
        # including before uvicorn is up, when /readiness still refuses
        # connections but the entrypoint is already logging.
        if stream_logs and runtime is not None:
            new_tail = _tail_container_logs(runtime, container_name)
            if new_tail and new_tail != last_tail:
                # _tail_container_logs returns a fixed `--tail N` sliding window,
                # not append-from-zero output, so a string-prefix diff re-prints
                # the whole window once logs exceed N lines. Diff by line
                # membership instead and emit only lines not already shown.
                prev_lines = set(last_tail.splitlines())
                fresh = [ln for ln in new_tail.splitlines() if ln not in prev_lines]
                if fresh:
                    _print("\n".join(fresh).rstrip())
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
            cwd="/",
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
