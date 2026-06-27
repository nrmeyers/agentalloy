"""``start-rerank-server`` subcommand — bring up the reranker backend.

Runs right after ``start-embed-server`` in the setup pipeline. Reads
``recommend-models.json`` to discover the reranker model, then launches a
**second** ``llama-server`` instance serving the reranker GGUF at port 47952
in COMPLETIONS mode (NOT ``--embeddings``) so it exposes ``/v1/completions``
with logprobs for the signal intent reranker.

llama-server (llama.cpp) is the sole inference runner. This is a dedicated
instance separate from the embed server (47951).

The step is idempotent: if port 47952 is already reachable it exits 0
immediately without spawning a second process.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "start-rerank-server"
# The reranker llama-server listens on 47952 (SIGNAL_INTENT_RERANK_URL).
LLAMA_RERANK_PORT = 47952
RERANK_HOST = "127.0.0.1"
# Seconds to wait for llama-server /health before giving up.
LLAMA_START_TIMEOUT = 120

# Hardware-conditional rerank slot config (per-target tuple of (--parallel, -c)).
# CPU and GPU have opposite optima:
#
# - GPU: more slots → more concurrent prefill. ``--parallel 2 -c 4096`` (2 slots ×
#   2048 tok each) captures ~94% of the ``--parallel 8`` throughput at HALF the
#   total KV memory and gives 2× per-slot context (2048 vs 1024 tok), eliminating
#   the long-task headroom worry. Measured Jun 2026 on Vulkan/RTX 3060.
#
# - CPU: more slots → OpenMP thread contention → WORSE throughput. ``--parallel 1
#   -c 2048`` gives the single inference all CPU threads; 8 sequential requests at
#   full thread count beat 8 parallel at 1–2 threads each by 1.5–3×. Measured Jun
#   2026 on Xeon W-2225 (4-thread inference): batch-of-8 ~1170ms at --parallel 1
#   vs ~2216ms at --parallel 8 — almost 2× speedup from FEWER slots.
#
# ``-c`` is the TOTAL KV cache that gets divided by ``--parallel``, so per-slot
# context = ``-c`` ÷ ``--parallel``.
_RERANK_LAUNCH_BY_TARGET: dict[str, tuple[int, int]] = {
    "cpu": (1, 2048),  # 1 slot, all threads — avoids contention
    "nvidia": (2, 4096),  # 2 slots × 2048 tok each — Pareto sweet spot on GPU
    "radeon": (2, 4096),
    "apple-silicon": (2, 4096),
}
# Safe fallback for unknown targets: GPU shape (closer to what most installs use).
_DEFAULT_RERANK_LAUNCH = (2, 4096)


def rerank_launch_args(target: str | None) -> tuple[int, int]:
    """Return (--parallel, -c) for the rerank llama-server launch on ``target``.

    Imported by ``enable_service._render_llama_rerank_unit`` so the production
    systemd unit's ExecStart matches whatever ``agentalloy start-rerank-server``
    would launch interactively — a single source of truth for the slot config.
    """
    return _RERANK_LAUNCH_BY_TARGET.get(target or "cpu", _DEFAULT_RERANK_LAUNCH)


# Per-hardware GPU-offload layer counts. CPU offloads nothing; GPU targets
# offload all layers. The value is passed to ``-ngl`` at server start.
_NGL_BY_TARGET: dict[str, int] = {
    "cpu": 0,
    "nvidia": 999,
    "radeon": 999,
    "apple-silicon": 999,
}
_DEFAULT_NGL = 0


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        STEP_NAME,
        help="Start the reranker llama-server (port 47952) after the embed server.",
    )
    p.add_argument(
        "--models",
        required=True,
        help="Path to the recommend-models JSON output file.",
    )
    p.add_argument(
        "--hardware-target",
        default="cpu",
        help="Hardware target (cpu/nvidia/radeon/apple-silicon) — selects -ngl.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(LLAMA_START_TIMEOUT),
        help=f"Seconds to wait for llama-server /health (default: {LLAMA_START_TIMEOUT}).",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _resolve_rerank_model(models_json: dict[str, Any]) -> str:
    options: list[dict[str, Any]] = models_json.get("options", [])
    selected = next((o for o in options if o.get("default")), options[0] if options else {})
    return selected.get("rerank_model", "")


def _run(args: argparse.Namespace) -> int:
    models_path = Path(args.models)
    if not models_path.exists():
        print(f"ERROR: {models_path} not found — run pull-models first.", file=sys.stderr)
        return 1

    try:
        models_json: dict[str, Any] = json.loads(models_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Could not read {models_path}: {exc}", file=sys.stderr)
        return 1

    model = _resolve_rerank_model(models_json)
    if not model:
        print("ERROR: No rerank_model found in recommend-models output.", file=sys.stderr)
        return 1

    hardware_target = getattr(args, "hardware_target", "cpu") or "cpu"
    ngl = _NGL_BY_TARGET.get(hardware_target, _DEFAULT_NGL)

    # Idempotency: already listening?
    if _port_open(RERANK_HOST, LLAMA_RERANK_PORT):
        print(
            f"start-rerank-server: reranker endpoint already reachable on port "
            f"{LLAMA_RERANK_PORT} — skipping.",
            file=sys.stderr,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "already_running",
            "runner": "llama-server",
            "port": LLAMA_RERANK_PORT,
        }
        _save(result)
        write_result(result, args, human_fn=_render_rerank_server)
        return 0

    return _start_llama_server(model, ngl, args.timeout, args)


def _render_rerank_server(result: dict[str, Any]) -> None:
    """Render rerank server result in human-readable format."""
    action = result.get("action", "unknown")
    runner = result.get("runner", "unknown")
    port = result.get("port", 0)
    model = result.get("model", "")

    action_colors = {
        "already_running": "green",
        "started": "green",
    }
    color = action_colors.get(action, "dim")

    print_rich("\n  [bold]Reranker Server[/bold]\n")
    print_rich(f"  Status: [{color}]{action}[/{color}]")
    print_rich(f"  Runner: {runner}")
    print_rich(f"  Port: {port}")
    if model:
        print_rich(f"  Model: {model}")

    log_path = result.get("log_path")
    if log_path:
        print_rich(f"  Log: {log_path}")

    print_rich()


def _start_llama_server(model: str, ngl: int, timeout: float, args: argparse.Namespace) -> int:
    model_path = install_state.user_data_dir() / "models" / model
    if not model_path.exists():
        print(
            f"ERROR: GGUF not found at {model_path}",
            file=sys.stderr,
        )
        print("FIX:   Re-run `agentalloy install pull-models` to download it.", file=sys.stderr)
        return 1

    # COMPLETIONS mode — NO --embeddings. The reranker scores candidates via
    # /v1/completions logprobs, so the server must run as a completions server.
    hardware_target = getattr(args, "hardware_target", "cpu") or "cpu"
    parallel, ctx = rerank_launch_args(hardware_target)
    cmd = [
        "llama-server",
        "--port",
        str(LLAMA_RERANK_PORT),
        "-ngl",
        str(ngl),
        "-m",
        str(model_path),
        # Concurrent decode slots + total KV context, hardware-conditional. See
        # rerank_launch_args() — GPU and CPU have opposite optima and the helper
        # is the single source of truth (also imported by enable_service.py).
        "--parallel",
        str(parallel),
        "-c",
        str(ctx),
    ]
    log_path = install_state.user_data_dir() / "logs" / "rerank-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"start-rerank-server: launching llama-server on port {LLAMA_RERANK_PORT} "
        f"(ngl={ngl}, log={log_path})",
        file=sys.stderr,
    )
    try:
        with log_path.open("ab") as log_fh:
            subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
    except FileNotFoundError:
        print("ERROR: llama-server not found in PATH.", file=sys.stderr)
        print(
            "FIX:   Re-run `agentalloy install pull-models` to build it, "
            "or add ~/.local/bin to PATH.",
            file=sys.stderr,
        )
        return 1

    print(
        f"start-rerank-server: waiting up to {timeout:.0f}s for /health …",
        file=sys.stderr,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ready(RERANK_HOST, LLAMA_RERANK_PORT):
            break
        time.sleep(2)
    else:
        print(
            f"ERROR: llama-server did not start within {timeout:.0f}s. "
            f"Check {log_path} for details.",
            file=sys.stderr,
        )
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            tail = lines[-20:] if len(lines) > 20 else lines
            if tail:
                print(f"\n--- last {len(tail)} lines of {log_path} ---", file=sys.stderr)
                print("\n".join(tail), file=sys.stderr)
                print("--- end log ---", file=sys.stderr)
        except OSError:
            pass
        return 1

    print(f"start-rerank-server: llama-server ready on port {LLAMA_RERANK_PORT}", file=sys.stderr)
    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "started",
        "runner": "llama-server",
        "model": model,
        "model_path": str(model_path),
        "port": LLAMA_RERANK_PORT,
        "ngl": ngl,
        "log_path": str(log_path),
    }
    _save(result)
    write_result(result, args, human_fn=_render_rerank_server)
    return 0


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _health_ready(host: str, port: int) -> bool:
    # llama-server binds its listen socket before the GGUF finishes loading, so a
    # bare TCP connect reports "ready" while the model is still warming. Gate on
    # /health (200 only) so the next pipeline step doesn't hit a loading model.
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _save(result: dict[str, Any]) -> None:
    install_state.save_output_file(result, f"{STEP_NAME}.json")


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
