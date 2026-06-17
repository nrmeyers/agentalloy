"""``start-embed-server`` subcommand — bring up the embedding backend.

Runs between ``pull-models`` and ``install-packs`` in the setup pipeline.
Reads ``recommend-models.json`` to discover the embed model, then launches
``llama-server --embeddings --port 47951 --ubatch-size 2048 -m <gguf_path>``
as a background process and polls ``/health`` until it responds (or times
out).

llama-server (llama.cpp) is the sole inference runner.

The step is idempotent: if the embed endpoint is already reachable it exits 0
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
STEP_NAME = "start-embed-server"
# The embed llama-server listens on 47951 (RUNTIME_EMBED_BASE_URL in presets).
LLAMA_EMBED_PORT = 47951
EMBED_HOST = "127.0.0.1"
# llama-server batch size — keeps throughput high for pack ingest without
# requiring a fat context window.
LLAMA_UBATCH_SIZE = 2048
# Seconds to wait for llama-server /health before giving up.
LLAMA_START_TIMEOUT = 120


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        STEP_NAME,
        help="Start the embedding llama-server (port 47951) before pack install.",
    )
    p.add_argument(
        "--models",
        required=True,
        help="Path to the recommend-models JSON output file.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(LLAMA_START_TIMEOUT),
        help=f"Seconds to wait for llama-server /health (default: {LLAMA_START_TIMEOUT}).",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


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

    # Resolve the selected option (same logic as pull_models).
    options: list[dict[str, Any]] = models_json.get("options", [])
    selected = next((o for o in options if o.get("default")), options[0] if options else {})
    model: str = selected.get("embed_model", "")

    if not model:
        print("ERROR: No embed_model found in recommend-models output.", file=sys.stderr)
        return 1

    # Idempotency: already listening?
    if _port_open(EMBED_HOST, LLAMA_EMBED_PORT):
        print(
            f"start-embed-server: embed endpoint already reachable on port "
            f"{LLAMA_EMBED_PORT} — skipping.",
            file=sys.stderr,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "already_running",
            "runner": "llama-server",
            "port": LLAMA_EMBED_PORT,
        }
        _save(result)
        write_result(result, args, human_fn=_render_embed_server)
        return 0

    # Hardware target (from the recommend-models preset) selects GPU offload.
    hardware = str(models_json.get("preset") or "cpu").strip().lower()
    return _start_llama_server(model, args.timeout, args, hardware)


def _render_embed_server(result: dict[str, Any]) -> None:
    """Render embed server result in human-readable format."""
    action = result.get("action", "unknown")
    runner = result.get("runner", "unknown")
    port = result.get("port", 0)
    model = result.get("model", "")

    action_colors = {
        "already_running": "green",
        "started": "green",
    }
    color = action_colors.get(action, "dim")

    print_rich("\n  [bold]Embed Server[/bold]\n")
    print_rich(f"  Status: [{color}]{action}[/{color}]")
    print_rich(f"  Runner: {runner}")
    print_rich(f"  Port: {port}")
    if model:
        print_rich(f"  Model: {model}")

    log_path = result.get("log_path")
    if log_path:
        print_rich(f"  Log: {log_path}")

    print_rich()


def _start_llama_server(
    model: str, timeout: float, args: argparse.Namespace, hardware: str = "cpu"
) -> int:
    model_path = install_state.user_data_dir() / "models" / model
    if not model_path.exists():
        print(
            f"ERROR: GGUF not found at {model_path}",
            file=sys.stderr,
        )
        print("FIX:   Re-run `agentalloy install pull-models` to download it.", file=sys.stderr)
        return 1

    # GPU offload: -ngl from the hardware target (0 on CPU). Requires a GPU-capable
    # llama-server build (provisioned by pull-models for nvidia/radeon/apple-silicon).
    from agentalloy.install.subcommands.start_rerank_server import _DEFAULT_NGL, _NGL_BY_TARGET

    ngl = _NGL_BY_TARGET.get(hardware, _DEFAULT_NGL)
    cmd = [
        "llama-server",
        "--embeddings",
        "--pooling",
        "mean",
        "--port",
        str(LLAMA_EMBED_PORT),
        "--ubatch-size",
        str(LLAMA_UBATCH_SIZE),
    ]
    if ngl > 0:
        cmd += ["-ngl", str(ngl)]
    cmd += ["-m", str(model_path)]
    log_path = install_state.user_data_dir() / "logs" / "embed-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"start-embed-server: launching llama-server on port {LLAMA_EMBED_PORT} "
        f"(ubatch={LLAMA_UBATCH_SIZE}, log={log_path})",
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
        f"start-embed-server: waiting up to {timeout:.0f}s for /health …",
        file=sys.stderr,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ready(EMBED_HOST, LLAMA_EMBED_PORT):
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

    print(f"start-embed-server: llama-server ready on port {LLAMA_EMBED_PORT}", file=sys.stderr)
    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "started",
        "runner": "llama-server",
        "model": model,
        "model_path": str(model_path),
        "port": LLAMA_EMBED_PORT,
        "ubatch_size": LLAMA_UBATCH_SIZE,
        "log_path": str(log_path),
    }
    _save(result)
    write_result(result, args, human_fn=_render_embed_server)
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
