"""``start-embed-server`` subcommand — bring up the embedding backend.

Runs between ``pull-models`` and ``install-packs`` in the setup pipeline.
Reads ``recommend-models.json`` to discover the embed runner and model, then:

- **llama-server**: launches ``llama-server --embeddings --port 11436
  --ubatch-size 2048 -m <gguf_path>`` as a background process and polls
  ``/health`` until it responds (or times out).
- **ollama**: ensures ``ollama serve`` is running via a best-effort check +
  ``ollama serve &``; ollama is already-running-tolerant.
- **lm-studio / other**: prints a manual instruction and exits 0 (the user
  must start it themselves; we can't automate a GUI app).

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
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "start-embed-server"
# Default ports per runner — used by the presets for RUNTIME_EMBED_BASE_URL.
# llama-server listens on the same port; ollama uses its default :11434.
OLLAMA_EMBED_PORT = 11434
LM_STUDIO_EMBED_PORT = 1234
LLAMA_EMBED_PORT = 11434
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
        help="Start the embedding backend (llama-server/ollama) before pack install.",
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
    runner: str = selected.get("embed_runner", "")
    model: str = selected.get("embed_model", "")

    if not runner:
        print("ERROR: No embed_runner found in recommend-models output.", file=sys.stderr)
        return 1

    # Port depends on runner — use runner-default port.
    if runner == "ollama":
        embed_port = OLLAMA_EMBED_PORT
    elif runner == "lm-studio":
        embed_port = LM_STUDIO_EMBED_PORT
    else:
        embed_port = LLAMA_EMBED_PORT

    # Idempotency: already listening?
    if _port_open(EMBED_HOST, embed_port):
        print(
            f"start-embed-server: embed endpoint already reachable on port {embed_port} — skipping.",
            file=sys.stderr,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "already_running",
            "runner": runner,
            "port": embed_port,
        }
        _save(result)
        write_result(result, args, human_fn=_render_embed_server)
        return 0

    if runner == "llama-server":
        return _start_llama_server(model, args.timeout, args)
    if runner == "ollama":
        return _start_ollama(model, args)
    # lm-studio and other GUI-based runners — can't automate
    return _manual_instruction(runner, model, args)


def _render_embed_server(result: dict[str, Any]) -> None:
    """Render embed server result in human-readable format."""
    action = result.get("action", "unknown")
    runner = result.get("runner", "unknown")
    port = result.get("port", 0)
    model = result.get("model", "")

    action_colors = {
        "already_running": "green",
        "started": "green",
        "manual_required": "yellow",
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

    instruction = result.get("instruction")
    if instruction:
        print_rich(f"  Instruction: {instruction}")

    print_rich()


def _start_llama_server(model: str, timeout: float, args: argparse.Namespace) -> int:
    model_path = install_state.user_data_dir() / "models" / model
    if not model_path.exists():
        print(
            f"ERROR: GGUF not found at {model_path}",
            file=sys.stderr,
        )
        print("FIX:   Re-run `agentalloy install pull-models` to download it.", file=sys.stderr)
        return 1

    cmd = [
        "llama-server",
        "--embeddings",
        "--port",
        str(LLAMA_EMBED_PORT),
        "--ubatch-size",
        str(LLAMA_UBATCH_SIZE),
        "-m",
        str(model_path),
    ]
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
        if _port_open(EMBED_HOST, LLAMA_EMBED_PORT):
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


def _start_ollama(model: str, args: argparse.Namespace) -> int:
    """Ensure ollama serve is running. ollama is idempotent — safe to call twice."""
    import shutil

    if not shutil.which("ollama"):
        print("ERROR: ollama not found in PATH.", file=sys.stderr)
        return 1

    # Spawn ollama serve; it exits immediately if already running.
    log_path = install_state.user_data_dir() / "logs" / "embed-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_fh:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )

    # Ollama defaults to :11434. Poll for it.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _port_open(EMBED_HOST, OLLAMA_EMBED_PORT):
            break
        time.sleep(2)
    else:
        print(
            f"ERROR: Ollama not reachable on port "
            f"{OLLAMA_EMBED_PORT} after 30s. "
            "Ensure ollama is running (e.g. `ollama serve`).",
            file=sys.stderr,
        )
        return 1

    # Verify the model was actually loaded via the Ollama API.
    # The port being open doesn't guarantee the requested model is available.
    if model:
        import urllib.request

        model_check_url = f"http://{EMBED_HOST}:{OLLAMA_EMBED_PORT}/api/tags"
        model_ok = False
        for _attempt in range(3):
            try:
                req = urllib.request.Request(model_check_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read())
                        loaded_names: list[str] = [
                            m.get("name", "") for m in data.get("models", [])
                        ]
                        if any(model in name for name in loaded_names):
                            model_ok = True
                        break
            except Exception:
                time.sleep(2)
        if not model_ok:
            print(
                f"WARN: Ollama is running on port {OLLAMA_EMBED_PORT} but model "
                f"'{model}' was not found in loaded models. "
                f"Run `ollama pull {model}` to download it.",
                file=sys.stderr,
            )
            # Don't fail — the model may be pulled separately; just warn.

    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "started",
        "runner": "ollama",
        "model": model,
        "port": OLLAMA_EMBED_PORT,
    }
    _save(result)
    write_result(result, args, human_fn=_render_embed_server)
    return 0


def _manual_instruction(runner: str, model: str, args: argparse.Namespace) -> int:
    port = LM_STUDIO_EMBED_PORT if runner == "lm-studio" else LLAMA_EMBED_PORT
    instructions = {
        "lm-studio": (
            f"Start LM Studio and load model '{model}', then enable the local server "
            f"on port {port} under Settings → Local Server."
        ),
    }
    msg = instructions.get(runner, f"Start your '{runner}' embedding server on port {port}.")
    print(f"start-embed-server: manual step required for runner '{runner}':", file=sys.stderr)
    print(f"  {msg}", file=sys.stderr)
    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "manual_required",
        "runner": runner,
        "model": model,
        "port": port,
        "instruction": msg,
    }
    _save(result)
    write_result(result, args, human_fn=_render_embed_server)
    # Exit 0 — setup can continue; if the server isn't up, install-packs will
    # fail with a clear connection-refused error.
    return 0


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _save(result: dict[str, Any]) -> None:
    install_state.save_output_file(result, f"{STEP_NAME}.json")


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
