"""``pull-models`` subcommand.

Idempotent model pulls for the runners selected by ``recommend-models``.

Auto-pull runners (``ollama``, ``fastflowlm``): invokes the CLI pull
command and reports duration + size.

Manual-pull runners (``lmstudio``, ``vllm``, ``mlx``): emits human-readable
instructions for the runbook to surface.  No subprocess call.

Reads the ``recommend-models`` JSON output (either from a file path or
from ``install-state.json``) to determine which models and runners to
pull.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "pull-models"

# Runners that support CLI-driven auto-pull.
_AUTO_PULL_RUNNERS: dict[str, tuple[str, list[str]]] = {
    # runner_name -> (binary, [pull, <model> is appended])
    "ollama": ("ollama", ["pull"]),
    "fastflowlm": ("flm", ["pull"]),
}

# Manual-pull runners with instruction templates.
_MANUAL_INSTRUCTIONS: dict[str, str] = {
    "lmstudio": ("Open LM Studio, search for '{model}', click Download. Confirm when complete."),
    "vllm": (
        "vLLM loads models on `vllm serve`. Ensure '{model}' is "
        "accessible (Hugging Face login if gated). No explicit pull needed."
    ),
    "mlx": (
        "Run `mlx_lm.convert --hf-path {model}` to convert for MLX, "
        "or download the MLX-format weights from Hugging Face."
    ),
}


# Ollama default daemon port for the main API. The embed endpoint lives on
# a separate port (RUNTIME_EMBED_BASE_URL, typically 11436). For `ollama
# pull` we only need the main API.
_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434


def _ollama_daemon_running(timeout: float = 1.0) -> bool:
    """Return True if the Ollama API is reachable on the default port.

    Cheap probe — opens a TCP socket and closes it. Used before
    ``ollama pull`` so we can auto-start the daemon when it's down
    instead of letting the pull fail with the cryptic ``could not
    connect to ollama server`` message.
    """
    import socket as _socket

    try:
        with _socket.create_connection((_OLLAMA_HOST, _OLLAMA_PORT), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def _ensure_ollama_running() -> tuple[bool, str | None]:
    """Probe the Ollama daemon; spawn ``ollama serve`` if down.

    Returns ``(ok, error)``. ``ok`` is True when the daemon is reachable
    after this call (already-running or just-spawned). ``error`` is a
    human-readable message when ``ok`` is False — e.g. ``ollama`` binary
    not on PATH, or the daemon didn't come up within the deadline.

    Mirrors ``start_embed_server._start_ollama`` so behavior is
    consistent across the install pipeline. The status line on spawn
    is always printed (a single line per install run) — pull_models'
    ``quiet`` mode applies to the structured JSON output, not transient
    progress notes.
    """
    if _ollama_daemon_running():
        return True, None

    binary = shutil.which("ollama")
    if not binary:
        return False, (
            "ollama binary not found in PATH. Install Ollama from "
            "https://ollama.com/download and re-run setup."
        )

    print("  ollama daemon not running; starting it now ...", file=sys.stderr)

    # Spawn `ollama serve` detached so it survives this process. ollama is
    # already-running-tolerant — a second `serve` just exits.
    log_path = install_state.user_data_dir() / "logs" / "ollama.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as log_fh:
            proc = subprocess.Popen(  # noqa: S603 — binary path is from shutil.which
                [binary, "serve"],
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
    except OSError as exc:
        return False, f"failed to spawn `ollama serve`: {exc}"

    # Record the spawned PID so `uninstall` can stop *this* ollama process
    # (instead of `pkill -f` killing any ollama the user has running for
    # other apps). Best-effort: a state-write failure must not block install.
    try:
        _st = install_state.load_state()
        _st["spawned_ollama_pid"] = proc.pid
        install_state.save_state(_st)
    except Exception:  # noqa: BLE001
        pass

    # Wait for the daemon to come up. 15s is generous for a local
    # spawn (ollama typically binds in under a second).
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _ollama_daemon_running():
            return True, None
        time.sleep(0.5)

    return False, (
        f"ollama daemon did not come up within 15s. "
        f"Check {log_path} for startup errors, or run `ollama serve` manually."
    )


# ---------------------------------------------------------------------------
# Runner presence checks
# ---------------------------------------------------------------------------


def _is_model_present_ollama(model: str) -> bool:
    """Check if a model is already pulled in Ollama."""
    binary = shutil.which("ollama")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        # ollama list output: NAME  ID  SIZE  MODIFIED
        # Model names may include tags; match on the name prefix.
        for line in result.stdout.splitlines()[1:]:  # skip header
            name = line.split()[0] if line.strip() else ""
            # Exact match or match without :latest tag
            if name == model or name == f"{model}:latest":
                return True
            # Also match if model has a tag and the listed name matches
            if ":" in model and name.startswith(model.split(":")[0]):
                listed_tag = name.split(":")[-1] if ":" in name else "latest"
                requested_tag = model.split(":")[-1]
                if listed_tag == requested_tag:
                    return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return False


def _is_model_present_fastflowlm(model: str) -> bool:
    """Check if a model is loaded in FastFlowLM."""
    binary = shutil.which("flm")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        return model in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return False


def _is_model_present_llama_server(model: str) -> bool:
    """Check if a GGUF model file exists on disk for llama-server."""
    model_path = install_state.user_data_dir() / "models" / model
    return model_path.exists()


_PRESENCE_CHECKS: dict[str, Any] = {
    "ollama": _is_model_present_ollama,
    "fastflowlm": _is_model_present_fastflowlm,
    "llama-server": _is_model_present_llama_server,
}


# ---------------------------------------------------------------------------
# llama-server: build and GGUF download helpers
# ---------------------------------------------------------------------------

# Permanent directories — survive reinstalls and allow incremental updates.
_LLAMA_CPP_BUILD_ROOT = install_state.user_data_dir() / "build" / "llama.cpp"
_LLAMA_SERVER_BIN_DIR = Path.home() / ".local" / "bin"

# Hugging Face GGUF download map: model filename → raw download URL.
# HF repo: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF
_GGUF_URL_MAP: dict[str, str] = {
    "Qwen3-Embedding-0.6B-Q8_0.gguf": (
        "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF"
        "/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf"
    ),
}


def _check_build_prereqs() -> list[str]:
    """Return a list of missing build prerequisites (empty = all present)."""
    missing: list[str] = []
    for tool in ("git", "cmake"):
        if not shutil.which(tool):
            missing.append(tool)
    # Need a C++ compiler: try c++ / g++ / clang++
    if not any(shutil.which(cc) for cc in ("c++", "g++", "clang++")):
        missing.append("C++ compiler (g++, clang++, or c++)")
    return missing


def _build_llama_server() -> dict[str, Any]:
    """Clone llama.cpp and build llama-server, installing to ~/.local/bin.

    The source tree is kept in a permanent directory so incremental ``git
    pull`` + rebuild is cheap on subsequent calls.

    Returns a result dict with keys: success, binary_path, error, duration_ms.
    """
    # Fast path: binary already on PATH.
    existing = shutil.which("llama-server")
    if existing:
        return {"success": True, "binary_path": existing, "error": None, "duration_ms": 0}

    # Check prereqs before doing anything expensive.
    missing = _check_build_prereqs()
    if missing:
        tools = ", ".join(missing)
        return {
            "success": False,
            "binary_path": None,
            "error": f"Missing build prerequisites: {tools}",
            "hint": (
                f"Install the following tools then re-run pull-models: {tools}. "
                "On Debian/Ubuntu: `sudo apt install git cmake build-essential`. "
                "On macOS: `xcode-select --install && brew install cmake`."
            ),
        }

    _LLAMA_CPP_BUILD_ROOT.parent.mkdir(parents=True, exist_ok=True)
    _LLAMA_SERVER_BIN_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    print(
        f"  llama-server: building from source in {_LLAMA_CPP_BUILD_ROOT} ...",
        file=sys.stderr,
    )

    try:
        # 1. Clone or update the source tree.
        if not _LLAMA_CPP_BUILD_ROOT.exists():
            print("  llama-server: cloning llama.cpp (this may take a minute) ...", file=sys.stderr)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/ggerganov/llama.cpp",
                    str(_LLAMA_CPP_BUILD_ROOT),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
        else:
            print("  llama-server: updating existing source tree ...", file=sys.stderr)
            subprocess.run(
                ["git", "-C", str(_LLAMA_CPP_BUILD_ROOT), "pull", "--ff-only"],
                check=True,
                capture_output=True,
                timeout=120,
            )

        # 2. CMake configure.
        build_dir = _LLAMA_CPP_BUILD_ROOT / "build"
        build_dir.mkdir(exist_ok=True)
        print("  llama-server: cmake configure ...", file=sys.stderr)
        subprocess.run(
            [
                "cmake",
                "-S",
                str(_LLAMA_CPP_BUILD_ROOT),
                "-B",
                str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DLLAMA_BUILD_SERVER=ON",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        # 3. CMake build (-j uses all available cores).
        print("  llama-server: cmake build (this may take several minutes) ...", file=sys.stderr)
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--config", "Release", "-j"],
            check=True,
            capture_output=True,
            timeout=900,  # 15 min upper bound
        )

        # 4. Locate the built binary.
        candidate = build_dir / "bin" / "llama-server"
        if not candidate.exists():
            return {
                "success": False,
                "binary_path": None,
                "error": f"Build completed but binary not found at expected path: {candidate}",
            }

        # 5. Install to ~/.local/bin.
        dest = _LLAMA_SERVER_BIN_DIR / "llama-server"
        shutil.copy2(str(candidate), str(dest))
        dest.chmod(0o755)

        duration_ms = int((time.monotonic() - t0) * 1000)
        print(
            f"  llama-server: installed to {dest} ({duration_ms} ms)",
            file=sys.stderr,
        )

        # Warn if the install dir is not on PATH.
        if str(_LLAMA_SERVER_BIN_DIR) not in os.environ.get("PATH", ""):
            print(
                f"  WARNING: {_LLAMA_SERVER_BIN_DIR} is not in your PATH. "
                "Add it to your shell profile so `llama-server` can be found at runtime.",
                file=sys.stderr,
            )

        return {
            "success": True,
            "binary_path": str(dest),
            "error": None,
            "duration_ms": duration_ms,
        }

    except subprocess.CalledProcessError as exc:
        stderr_snippet = (exc.stderr or b"").decode(errors="replace").strip()[-500:]
        return {
            "success": False,
            "binary_path": None,
            "error": f"Build failed (exit {exc.returncode}): {stderr_snippet}",
            "hint": (
                "Check build output above. You can also build manually:\n"
                "  git clone https://github.com/ggerganov/llama.cpp\n"
                "  cd llama.cpp && cmake -B build -DLLAMA_BUILD_SERVER=ON\n"
                "  cmake --build build --config Release -j\n"
                "  cp build/bin/llama-server ~/.local/bin/"
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "binary_path": None,
            "error": "Build timed out",
            "hint": "Run the build manually in a terminal to see what's blocking.",
        }
    except OSError as exc:
        return {"success": False, "binary_path": None, "error": str(exc)}


def _download_gguf(model_name: str) -> dict[str, Any]:
    """Download a GGUF model from Hugging Face into the persistent models dir.

    Shows a simple byte-counter progress indicator on stderr.
    Returns a result dict with keys: success, path, error, duration_ms.
    """
    url = _GGUF_URL_MAP.get(model_name)
    if not url:
        return {
            "success": False,
            "error": (
                f"No Hugging Face download URL defined for model '{model_name}'. "
                "Add it to _GGUF_URL_MAP or download the GGUF manually."
            ),
        }

    dest_dir = install_state.user_data_dir() / "models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / model_name

    print(
        f"  llama-server: downloading {model_name} from Hugging Face ...",
        file=sys.stderr,
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 1 << 20  # 1 MiB chunks
            with open(dest_path, "wb") as out_f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    out_f.write(buf)
                    downloaded += len(buf)
                    if total:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1 << 20)
                        total_mb = total / (1 << 20)
                        print(
                            f"\r  llama-server: {mb:.1f}/{total_mb:.1f} MiB ({pct}%)",
                            end="",
                            file=sys.stderr,
                        )
            print("", file=sys.stderr)  # newline after progress

        duration_ms = int((time.monotonic() - t0) * 1000)
        return {"success": True, "path": str(dest_path), "error": None, "duration_ms": duration_ms}

    except Exception as exc:  # noqa: BLE001
        # Remove a partial download so a retry starts clean.
        with contextlib.suppress(OSError):
            dest_path.unlink()
        return {"success": False, "error": str(exc)}


def _handle_llama_server(
    model: str, interactive: bool
) -> tuple[
    list[dict[str, Any]],  # auto_pulled entries
    list[dict[str, Any]],  # error entries
]:
    """Orchestrate binary build + GGUF download for llama-server.

    Returns (auto_pulled, errors) tuples to be merged into the main lists.
    """
    auto_pulled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # ---- 1. Binary ----------------------------------------------------------
    if not shutil.which("llama-server"):
        if interactive:
            try:
                choice = (
                    input("  llama-server binary not found. Build from source? [y/N]: ")
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                choice = "n"
        else:
            # Non-interactive: skip the build; surface an actionable error.
            choice = "n"

        if choice != "y":
            errors.append(
                {
                    "runner": "llama-server",
                    "model": model,
                    "success": False,
                    "error": "llama-server binary not found and build was skipped.",
                    "hint": (
                        "To build manually:\n"
                        "  git clone https://github.com/ggerganov/llama.cpp\n"
                        "  cd llama.cpp && cmake -B build -DLLAMA_BUILD_SERVER=ON\n"
                        "  cmake --build build --config Release -j\n"
                        "  cp build/bin/llama-server ~/.local/bin/\n"
                        "Then re-run `pull-models`."
                    ),
                }
            )
            return auto_pulled, errors

        build_result = _build_llama_server()
        if not build_result["success"]:
            errors.append(
                {
                    "runner": "llama-server",
                    "model": model,
                    "success": False,
                    "error": build_result.get("error", "unknown build error"),
                    "hint": build_result.get("hint"),
                }
            )
            return auto_pulled, errors

    # ---- 2. GGUF model file -------------------------------------------------
    if _is_model_present_llama_server(model):
        # Already downloaded from a previous run.
        return auto_pulled, errors

    download_result = _download_gguf(model)
    if not download_result["success"]:
        errors.append(
            {
                "runner": "llama-server",
                "model": model,
                "success": False,
                "error": download_result.get("error", "unknown download error"),
            }
        )
        return auto_pulled, errors

    auto_pulled.append(
        {
            "runner": "llama-server",
            "model": model,
            "duration_ms": download_result.get("duration_ms", 0),
            "path": download_result.get("path"),
        }
    )
    return auto_pulled, errors


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _collect_model_runner_pairs(option: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract the (model, runner) pair from a recommend-models option."""
    model = option.get("embed_model", "")
    runner = option.get("embed_runner", "")
    if model and runner:
        return [(model, runner)]
    return []


# Strict model-name pattern. Allowed characters cover the canonical
# `name:tag` form used by ollama/fastflowlm (letters, digits, `_`, `-`,
# `.`, `:`, `/` for org/repo namespaces). Rejects anything that could
# look like a CLI option (leading `-`) or carry shell metacharacters
# even though we run with shell=False — option-injection (e.g. model
# name `--insecure-tls`) is still effective via argv.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}$")


def _auto_pull(runner: str, model: str) -> dict[str, Any]:
    """Run the pull command for an auto-pull runner. Returns result dict."""
    binary_name, pull_args = _AUTO_PULL_RUNNERS[runner]
    if not _MODEL_NAME_RE.match(model):
        return {
            "runner": runner,
            "model": model,
            "success": False,
            "error": f"Refusing to pull model name with disallowed characters: {model!r}",
            "hint": "Model names must match [A-Za-z0-9][A-Za-z0-9._:/-]{0,127}.",
        }
    binary = shutil.which(binary_name)
    if not binary:
        hint = f"Install {binary_name} first."
        if runner == "ollama":
            hint = (
                "Install Ollama first (do NOT auto-execute — ask the user). "
                "Linux: `curl -fsSL https://ollama.com/install.sh | sh`. "
                "macOS: `brew install ollama` or https://ollama.com/download/mac. "
                "Other platforms: https://ollama.com/download. "
                "After install, ensure `ollama serve` is running, then re-run pull-models."
            )
        return {
            "runner": runner,
            "model": model,
            "success": False,
            "error": f"{binary_name} not found in PATH",
            "hint": hint,
        }

    # Ollama needs its daemon running for `pull`. If down, start it
    # automatically — otherwise the pull dies with a cryptic
    # "could not connect to ollama server" that masks a fixable state.
    if runner == "ollama":
        ok, err = _ensure_ollama_running()
        if not ok:
            return {
                "runner": runner,
                "model": model,
                "success": False,
                "error": err or "ollama daemon unavailable",
                "hint": "Start `ollama serve` manually and re-run pull-models.",
            }

    # `--` separator prevents argv option-injection if model name slipped
    # through the regex (defense in depth) or future regex relaxations
    # admit a leading-`-` form.
    cmd = [binary, *pull_args, "--", model]
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min for large model downloads
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        if result.returncode != 0:
            return {
                "runner": runner,
                "model": model,
                "success": False,
                "error": result.stderr.strip() or f"exit code {result.returncode}",
                "duration_ms": duration_ms,
            }
        return {
            "runner": runner,
            "model": model,
            "success": True,
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {
            "runner": runner,
            "model": model,
            "success": False,
            "error": "Pull timed out after 600 seconds",
        }
    except OSError as exc:
        return {
            "runner": runner,
            "model": model,
            "success": False,
            "error": str(exc),
        }


def pull_models(
    models_json: dict[str, Any],
    root: Path | None = None,
    runner_override: str | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Pull models based on recommend-models output.

    ``runner_override`` selects a specific runner from the options list,
    bypassing the ``default`` flag. Use when the agent captured the user's
    runner choice after ``recommend-models`` already ran non-interactively.

    Returns contract-shaped JSON with auto_pulled, manual_steps_required,
    and skipped_already_present arrays.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()

    # Idempotency: skip if already done
    st = install_state.load_state(root)
    if install_state.is_step_completed(st, STEP_NAME):
        prev = install_state.get_step_output(st, STEP_NAME)
        prev_data: dict[str, Any] = prev.get("output", {}) if prev else {}
        if not quiet:
            auto_pulled: list[dict[str, Any]] = prev_data.get("auto_pulled", [])
            skipped: list[dict[str, Any]] = prev_data.get("skipped_already_present", [])
            if auto_pulled and not skipped:
                print(f"  Models already pulled: {len(auto_pulled)}", file=sys.stderr)
            if skipped:
                print(f"  Already present: {len(skipped)}", file=sys.stderr)
        # Return cached result so main() can route through write_result
        return prev_data

    # Extract the option to use: explicit runner override > default flag > first.
    options = models_json.get("options", [])
    if not options:
        print("ERROR: No model options in recommend-models output", file=sys.stderr)
        raise SystemExit(1)

    if runner_override:
        option = next(
            (o for o in options if o.get("embed_runner") == runner_override),
            None,
        )
        if option is None:
            available = [o.get("embed_runner") for o in options]
            print(
                f"ERROR: Runner '{runner_override}' not found in recommend-models options.",
                file=sys.stderr,
            )
            print(f"CAUSE: Available runners: {available}", file=sys.stderr)
            print(
                "FIX:   Pass one of the above runners, or omit --runner to use the default.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    else:
        option = next((o for o in options if o.get("default")), options[0])
    pairs = _collect_model_runner_pairs(option)

    auto_pulled: list[dict[str, Any]] = []
    manual_steps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # Detect interactivity once for the whole pull loop.
    interactive = sys.stdin.isatty()

    for model, runner in pairs:
        # Check presence — includes GGUF file check for llama-server.
        presence_fn = _PRESENCE_CHECKS.get(runner)
        if presence_fn and presence_fn(model):
            skipped.append({"runner": runner, "model": model})
            continue

        if runner == "llama-server":
            # llama-server has its own build + download flow.
            ls_pulled, ls_errors = _handle_llama_server(model, interactive)
            auto_pulled.extend(ls_pulled)
            errors.extend(ls_errors)
        elif runner in _AUTO_PULL_RUNNERS:
            result = _auto_pull(runner, model)
            if result.get("success"):
                auto_pulled.append(
                    {
                        "runner": runner,
                        "model": model,
                        "duration_ms": result.get("duration_ms", 0),
                    }
                )
            else:
                errors.append(result)
        elif runner in _MANUAL_INSTRUCTIONS:
            manual_steps.append(
                {
                    "runner": runner,
                    "model": model,
                    "instruction": _MANUAL_INSTRUCTIONS[runner].format(model=model),
                }
            )
        else:
            print(f"WARNING: Unknown runner '{runner}' for model '{model}'", file=sys.stderr)
            manual_steps.append(
                {
                    "runner": runner,
                    "model": model,
                    "instruction": f"Manually install model '{model}' using runner '{runner}'.",
                }
            )

    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "auto_pulled": auto_pulled,
        "manual_steps_required": manual_steps,
        "skipped_already_present": skipped,
    }

    if errors:
        output["errors"] = errors
        # Print errors but don't fail — let the runbook decide
        for err in errors:
            print(
                f"ERROR: Failed to pull {err.get('model', '?')} via {err.get('runner', '?')}: {err.get('error', 'unknown')}",
                file=sys.stderr,
            )
        # Don't record completion when any pull failed — otherwise the
        # idempotency check will permanently skip this step on rerun and
        # the user has no path to retry without `reset-step pull-models`.
        return output

    # Record step (only when every pull either succeeded or was already present)
    st = install_state.record_step(st, STEP_NAME, extra={"output": output})
    # Track which models were pulled for uninstall reference
    st["models_pulled"] = [f"{p['runner']}:{p['model']}" for p in auto_pulled] + [
        f"{s['runner']}:{s['model']}" for s in skipped
    ]
    install_state.save_state(st, root)

    return output


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "pull-models",
        help="Idempotent model pulls for selected runners.",
    )
    p.add_argument(
        "--models",
        required=True,
        help="Path to the recommend-models JSON output file.",
    )
    p.add_argument(
        "--runner",
        default=None,
        help=(
            "Override the runner selected by recommend-models "
            "(e.g. ollama, llama-server). Use when the agent captured "
            "the user's choice after recommend-models ran non-interactively."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render pull models result in human-readable format."""
    auto_pulled = result.get("auto_pulled", [])
    manual_steps = result.get("manual_steps_required", [])
    skipped = result.get("skipped_already_present", [])
    errors = result.get("errors", [])

    print_rich("\n  [bold]Pull Models[/bold]\n")

    if auto_pulled:
        print_rich("  [green]Pulled:[/green]")
        for p in auto_pulled:
            print_rich(f"    {p.get('runner', '?')}:{p.get('model', '?')}")

    if skipped:
        print_rich("  [dim]Already present:[/dim]")
        for s in skipped:
            print_rich(f"    {s.get('runner', '?')}:{s.get('model', '?')}")

    if manual_steps:
        print_rich("  [yellow]Manual steps required:[/yellow]")
        for m in manual_steps:
            print_rich(f"    {m.get('runner', '?')}:{m.get('model', '?')}")
            print_rich(f"      {m.get('instruction', '')}")

    if errors:
        print_rich("  [red]Errors:[/red]")
        for e in errors:
            print_rich(f"    {e.get('runner', '?')}:{e.get('model', '?')} — {e.get('error', '')}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    models_path = Path(args.models)
    if not models_path.exists():
        print(f"ERROR: Models file not found: {models_path}", file=sys.stderr)
        print("CAUSE: The recommend-models output file is missing.", file=sys.stderr)
        print("FIX:   Run `recommend-models` first, or pass the correct path.", file=sys.stderr)
        return 1

    models_json = json.loads(models_path.read_text())
    result = pull_models(
        models_json,
        runner_override=getattr(args, "runner", None),
        quiet=getattr(args, "quiet", False),
    )
    write_result(result, args, human_fn=_render_human)

    # Non-zero exit if there were pull errors
    if result.get("errors"):
        return 1

    # Distinguish "no work needed" from "did real work" so the caller
    # (simple_setup) can render a "skipping" line instead of a
    # generic "Done". This mirrors EXIT_NOOP semantics used elsewhere
    # in the install pipeline (seed_corpus, etc.).
    pulled: list[Any] = result.get("auto_pulled") or []
    skipped: list[Any] = result.get("skipped_already_present") or []
    manual: list[Any] = result.get("manual_steps_required") or []
    if not pulled and not manual and skipped:
        return 4
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
