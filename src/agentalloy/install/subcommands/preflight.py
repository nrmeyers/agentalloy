"""``preflight`` subcommand — verify host prerequisites before any install step.

Two phases:

- ``early`` (default): host-agnostic checks that gate the rest of the
  install: Python version, ``uv`` on PATH, the ``agentalloy`` CLI on
  PATH (so the runbook LLM doesn't sail past a missing ``~/.local/bin``
  entry), XDG dirs writable, network reachable, default port free.
- ``runner``: runner-specific checks (llama-server, FastFlowLM). Run after
  ``recommend-models`` so we know which runner was selected.

Exit codes follow the project contract (see
``agentalloy.install.__main__``):

    0  all checks passed
    1  one or more user-correctable checks failed
    2  unexpected exception (re-raised by the dispatcher)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, write_result

SCHEMA_VERSION = 1

_DEFAULT_PORT = 47950
_PHASES = ("early", "runner", "container")

# llama-server (llama.cpp) is the sole inference runner. The embed server
# listens on 47951 (--embeddings mode); RUNTIME_EMBED_BASE_URL overrides it.
_LLAMA_EMBED_PORT = 47951
_DEFAULT_EMBED_BASE_URL = f"http://localhost:{_LLAMA_EMBED_PORT}"


def _check(
    name: str,
    *,
    passed: bool,
    started: float,
    detail: str | None = None,
    error: str | None = None,
    remediation: str | None = None,
    severity: str = "fatal",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": name,
        "passed": passed,
        "severity": severity,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if detail:
        out["detail"] = detail
    if error:
        out["error"] = error
    if remediation:
        out["remediation"] = remediation
    return out


# ---------------------------------------------------------------------------
# Early-phase checks
# ---------------------------------------------------------------------------


def _check_python_version() -> dict[str, Any]:
    t0 = time.monotonic()
    v = sys.version_info
    if v >= (3, 12):
        return _check(
            "python_version",
            passed=True,
            started=t0,
            detail=f"Python {v.major}.{v.minor}.{v.micro}",
        )
    return _check(
        "python_version",
        passed=False,
        started=t0,
        error=f"Python {v.major}.{v.minor}.{v.micro} < 3.12",
        remediation="Install Python 3.12+ (e.g. via mise, pyenv, or your OS package manager).",
    )


def _check_uv_present() -> dict[str, Any]:
    t0 = time.monotonic()
    binary = shutil.which("uv")
    if binary:
        return _check("uv_present", passed=True, started=t0, detail=f"uv at {binary}")
    return _check(
        "uv_present",
        passed=False,
        started=t0,
        error="uv not found on PATH",
        remediation=(
            "Install uv: see https://docs.astral.sh/uv/getting-started/installation/. "
            "Do not auto-execute the install script — confirm with the user first."
        ),
    )


def _path_entries() -> list[str]:
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def _check_cli_on_path() -> dict[str, Any]:
    """Verify ``agentalloy`` resolves and the canonical user-bin dir is on PATH.

    The runbook hard-stops here because every later step shells out to
    ``agentalloy ...`` from the user's repo cwd; a missing PATH entry
    silently routes through ``~/.local/bin/agentalloy`` only when the
    LLM happens to use the absolute path, which it shouldn't.
    """
    t0 = time.monotonic()
    binary = shutil.which("agentalloy")
    home = Path.home()
    expected = str(home / ".local" / "bin")
    on_path = expected in _path_entries()

    if binary and on_path:
        return _check(
            "cli_on_path",
            passed=True,
            started=t0,
            detail=f"agentalloy at {binary}; {expected} on PATH",
        )

    parts: list[str] = []
    if not binary:
        parts.append("`agentalloy` not resolvable on PATH")
    if not on_path:
        parts.append(f"{expected} not in PATH")
    error = "; ".join(parts)

    remediation = (
        f"Add this line to your shell profile (~/.bashrc, ~/.zshrc) and "
        f'restart the shell:\n\n    export PATH="{expected}:$PATH"\n\n'
        f"Then re-run `agentalloy preflight`. If `uv tool install --editable .` "
        f"has not been run yet, run it first."
    )
    return _check(
        "cli_on_path",
        passed=False,
        started=t0,
        error=error,
        remediation=remediation,
    )


def _check_xdg_dirs_writable() -> dict[str, Any]:
    t0 = time.monotonic()
    config_home = (
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "agentalloy"
    )
    data_home = (
        Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "agentalloy"
    )

    problems: list[str] = []
    for d in (config_home, data_home):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"cannot create {d}: {exc}")
            continue
        if not os.access(d, os.W_OK):
            problems.append(f"{d} not writable")

    if problems:
        return _check(
            "xdg_dirs_writable",
            passed=False,
            started=t0,
            error="; ".join(problems),
            remediation=(
                "Ensure your user owns these directories: "
                f"`chown -R $USER {config_home} {data_home}` "
                "and that the parent dirs exist."
            ),
        )
    return _check(
        "xdg_dirs_writable",
        passed=True,
        started=t0,
        detail=f"writable: {config_home}, {data_home}",
    )


def _check_network_reachable() -> dict[str, Any]:
    """HEAD https://github.com — required for model + (some) pack downloads."""
    t0 = time.monotonic()
    try:
        req = Request("https://github.com", method="HEAD")
        with urlopen(req, timeout=3) as resp:  # noqa: S310 — fixed URL
            status = getattr(resp, "status", 200)
    except (URLError, OSError) as exc:
        return _check(
            "network_reachable",
            passed=False,
            started=t0,
            severity="warn",
            error=f"HEAD https://github.com failed: {exc}",
            remediation=(
                "Check network / proxy settings. Offline installs can still "
                "complete with locally-cached models, but pack manifest "
                "downloads will fail."
            ),
        )
    return _check(
        "network_reachable",
        passed=True,
        started=t0,
        detail=f"github.com reachable (HTTP {status})",
    )


def _check_port_free(port: int) -> dict[str, Any]:
    t0 = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return _check(
            "port_free",
            passed=False,
            started=t0,
            severity="warn",
            error=f"port {port} in use: {exc}",
            remediation=(
                f"Either stop the process bound to {port}, or pass "
                f"`--port <n>` to `agentalloy write-env` later."
            ),
        )
    finally:
        sock.close()
    return _check("port_free", passed=True, started=t0, detail=f"port {port} free")


# ---------------------------------------------------------------------------
# Runner-phase checks
# ---------------------------------------------------------------------------


def _try_brew_install(package: str, *, cask: bool = False) -> tuple[bool, str | None]:
    """Run `brew install [--cask] <package>` on macOS. Returns (ok, error).

    No-op on non-macOS, when brew isn't on PATH, or when the user has not
    opted in via ``AGENTALLOY_PREFLIGHT_AUTO_INSTALL=1``. The opt-in gate
    avoids executing a package manager during a "check" without explicit
    consent — matching the rest of preflight's instructions-only default.
    """
    if sys.platform != "darwin":
        return False, "not macOS"
    if not shutil.which("brew"):
        return False, "brew not on PATH"
    if os.environ.get("AGENTALLOY_PREFLIGHT_AUTO_INSTALL") != "1":
        return False, ("auto-install disabled (set AGENTALLOY_PREFLIGHT_AUTO_INSTALL=1 to opt in)")
    cmd = ["brew", "install"]
    if cask:
        cmd.append("--cask")
    cmd.append(package)
    print(f"  preflight: running `{' '.join(cmd)}` ...", file=sys.stderr)
    # Route brew's stdout to stderr so it doesn't corrupt our --json output.
    try:
        subprocess.run(cmd, check=True, timeout=600, stdout=sys.stderr)
    except subprocess.CalledProcessError as exc:
        return False, f"brew install failed (exit {exc.returncode})"
    except subprocess.TimeoutExpired:
        return False, "brew install timed out after 600s"
    except OSError as exc:
        return False, f"brew install failed to spawn: {exc}"
    return True, None


def _check_llama_server_present() -> dict[str, Any]:
    t0 = time.monotonic()
    binary = shutil.which("llama-server")
    if binary:
        return _check(
            "llama_server_present", passed=True, started=t0, detail=f"llama-server at {binary}"
        )

    # macOS auto-install via Homebrew (llama.cpp formula ships llama-server).
    if sys.platform == "darwin" and shutil.which("brew"):
        ok, err = _try_brew_install("llama.cpp")
        if ok:
            binary = shutil.which("llama-server")
            if binary:
                return _check(
                    "llama_server_present",
                    passed=True,
                    started=t0,
                    detail=f"llama-server at {binary} (installed via brew)",
                )
            error = (
                "brew install llama.cpp succeeded but `llama-server` is "
                "still not on PATH; check `brew --prefix llama.cpp`"
            )
        else:
            error = f"brew install llama.cpp failed: {err or 'unknown error'}"
        return _check(
            "llama_server_present",
            passed=False,
            started=t0,
            error=error,
            remediation=(
                "Install llama.cpp manually: `brew install llama.cpp`, then re-run preflight."
            ),
        )

    return _check(
        "llama_server_present",
        passed=False,
        started=t0,
        error="llama-server not found on PATH",
        remediation=(
            "Install llama-server (llama.cpp):\n"
            "  macOS:   brew install llama.cpp\n"
            "  Other:   build from source — see "
            "https://github.com/ggerganov/llama.cpp/blob/master/examples/server/README.md\n"
            "Do not auto-execute — confirm with the user first."
        ),
    )


def _embed_base_url() -> str:
    """Resolve the embed server base URL from .env (fallback to the default).

    Honors ``RUNTIME_EMBED_BASE_URL`` written by ``write-env``; defaults to
    the llama-server embed port (47951) when no env file is present.
    """
    env = install_state.parse_env_file(install_state.env_path())
    return env.get("RUNTIME_EMBED_BASE_URL", _DEFAULT_EMBED_BASE_URL).rstrip("/")


def _check_llama_server_reachable() -> dict[str, Any]:
    """Probe the embed llama-server's /health endpoint.

    llama-server exposes ``/health`` (returns 200 + ``{"status": "ok"}`` once
    the model is loaded). Unlike Ollama it has no ``/api/tags``.
    """
    t0 = time.monotonic()
    base = _embed_base_url()
    url = f"{base}/health"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=2) as resp:
            _ = resp.read(1)
    except (URLError, OSError) as exc:
        return _check(
            "llama_server_reachable",
            passed=False,
            started=t0,
            error=f"GET {url} failed: {exc}",
            remediation=(
                f"Start the embed llama-server: `llama-server --embeddings --port "
                f"{_LLAMA_EMBED_PORT}`, or ensure it's running. Re-run preflight once "
                f"`curl -s {url}` returns 200."
            ),
        )
    return _check("llama_server_reachable", passed=True, started=t0, detail=f"GET {url} ok")


def _check_fastflowlm_present() -> dict[str, Any]:
    t0 = time.monotonic()
    binary = shutil.which("flm")
    if binary:
        return _check("fastflowlm_present", passed=True, started=t0, detail=f"flm at {binary}")
    return _check(
        "fastflowlm_present",
        passed=False,
        started=t0,
        error="flm not found on PATH",
        remediation="Install FastFlowLM and ensure `flm` is on PATH.",
    )


# ---------------------------------------------------------------------------
# Container-phase checks
# ---------------------------------------------------------------------------


def _check_runtime_binary(runtime: str | None) -> dict[str, Any]:
    """Check that a container runtime (podman or docker) is available.

    Parameters
    ----------
    runtime : str | None
        The runtime binary name (e.g. ``"podman"`` or ``"docker"``),
        or ``None`` if neither was detected.

    Returns
    -------
    dict
        Check result with ``passed``, ``detail``/``error``, and ``remediation``.
    """
    t0 = time.monotonic()
    if runtime is None:
        return _check(
            "runtime_binary",
            passed=False,
            started=t0,
            error="Neither `podman` nor `docker` found on PATH",
            remediation=(
                "Install Podman or Docker, and ensure its daemon/machine is running:\n"
                "  Podman:  brew install podman (macOS) / sudo apt install podman (Linux),"
                " then `podman machine start`\n"
                "  Docker:  https://docs.docker.com/get-docker/ (start Docker Desktop /"
                " `sudo systemctl start docker`)"
            ),
        )
    binary = shutil.which(runtime)
    if binary is None:
        return _check(
            "runtime_binary",
            passed=False,
            started=t0,
            error=f"``{runtime}`` not found on PATH",
            remediation=(
                f"Install {runtime} and ensure it is on PATH and its daemon/machine "
                f"is running:\\n  Verify:  {runtime} info"
            ),
        )
    # Presence on PATH is not enough: the daemon/machine must be reachable.
    # A podman CLI with no running machine (or docker with Desktop stopped) is
    # on PATH but unusable — fail here so the remediation actually matches.
    from agentalloy.install.subcommands import container_runtime

    if not container_runtime._runtime_is_functional(runtime):
        return _check(
            "runtime_binary",
            passed=False,
            started=t0,
            error=f"``{runtime}`` is installed but not responding",
            remediation=(
                f"Start the {runtime} daemon/machine, then re-run:\\n"
                "  Podman:  podman machine start\\n"
                "  Docker:  start Docker Desktop (macOS) or `sudo systemctl start docker`"
                f" (Linux)\\n  Verify:  {runtime} info"
            ),
        )
    return _check(
        "runtime_binary",
        passed=True,
        started=t0,
        detail=f"{runtime} at {binary}",
    )


def _check_ghcr_reachable() -> dict[str, Any]:
    """Check network connectivity to ghcr.io (for image pulls)."""
    t0 = time.monotonic()
    try:
        req = Request("https://ghcr.io", method="HEAD")
        with urlopen(req, timeout=5) as resp:
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


def _check_disk_space(min_bytes: int = 2 * 1024**3) -> dict[str, Any]:
    """Check available disk space before pulling/loading an image."""
    t0 = time.monotonic()
    paths_to_check = [
        os.environ.get("CONTAINER_STORAGE", ""),
    ]
    available = 0
    for path in paths_to_check:
        if path and os.path.exists(path):
            available = shutil.disk_usage(path).free
            break
    # No configured path worked — try common container storage locations
    if available == 0:
        for fallback in [
            os.path.expanduser("~/.local/share/containers"),
            "/var/lib/containers",
            "/var/lib/docker",
        ]:
            if os.path.exists(fallback):
                available = shutil.disk_usage(fallback).free
                break
    # Still nothing — fall back to root filesystem
    if available == 0:
        available = shutil.disk_usage("/").free
    if available < min_bytes:
        return _check(
            "disk_space",
            passed=False,
            started=t0,
            severity="fatal",
            error=f"Insufficient disk space: {available // (1024**2)}MB available, "
            f"{min_bytes // (1024**2)}MB required",
            remediation="Free disk space or use --image-path with a smaller tarball.",
        )
    return _check(
        "disk_space",
        passed=True,
        started=t0,
        detail=f"{available // (1024**3)}GB available",
    )


def _check_name_conflicts(runtime: str) -> dict[str, Any]:
    """Check for an existing ``agentalloy`` container.

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).

    Returns
    -------
    dict
        Check result. Fails if a container named ``agentalloy`` is found.
    """
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [runtime, "ps", "--all", "--filter", "name=agentalloy", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        container_id = (result.stdout or "").strip()
        if container_id:
            return _check(
                "name_conflicts",
                passed=False,
                started=t0,
                error=f"Container 'agentalloy' already exists (id={container_id})",
                remediation=(
                    "Stop and remove the existing container:\n"
                    f"  {runtime} rm -f agentalloy\n"
                    "Then re-run preflight."
                ),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _check(
            "name_conflicts",
            passed=False,
            started=t0,
            severity="warn",
            error=f"Failed to check for existing container: {exc}",
        )

    return _check(
        "name_conflicts",
        passed=True,
        started=t0,
        detail="No existing 'agentalloy' container found",
    )


def _check_volume_exists(runtime: str) -> dict[str, Any]:
    """Check if the ``agentalloy-data`` volume exists.

    This is informational — the volume will be created during setup if
    it doesn't exist. Preflight passes regardless.

    Parameters
    ----------
    runtime : str
        Container runtime binary (e.g. ``"podman"`` or ``"docker"``).

    Returns
    -------
    dict
        Check result (always passes).
    """
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [runtime, "volume", "inspect", "agentalloy-data"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return _check(
                "volume_exists",
                passed=True,
                started=t0,
                detail="Volume 'agentalloy-data' already exists",
            )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Volume doesn't exist — that's fine, it will be created during setup.
    return _check(
        "volume_exists",
        passed=True,
        started=t0,
        detail="Volume 'agentalloy-data' does not exist yet (will be created during setup)",
    )


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------


def _runner_from_models_output() -> str | None:
    """Read the chosen runner from recommend-models.json, if present."""
    fp = install_state.outputs_dir() / "recommend-models.json"
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("selected_runner") or data.get("embed_runner") or data.get("runner")


def run_preflight(
    *,
    phase: str = "early",
    runner: str | None = None,
    port: int = _DEFAULT_PORT,
    runtime: str | None = None,
) -> dict[str, Any]:
    if phase not in _PHASES:
        raise ValueError(f"invalid phase {phase!r}; must be one of {_PHASES}")

    t0 = time.monotonic()
    checks: list[dict[str, Any]] = []

    if phase == "early":
        checks.append(_check_python_version())
        checks.append(_check_uv_present())
        checks.append(_check_cli_on_path())
        checks.append(_check_xdg_dirs_writable())
        checks.append(_check_network_reachable())
        checks.append(_check_port_free(port))
    elif phase == "container":
        # Detect runtime if not provided. Use the shared functional-aware
        # detector so a present-but-non-functional podman (e.g. no machine on
        # macOS) does not mask a working docker.
        if runtime is None:
            from agentalloy.install.subcommands import container_runtime

            runtime = container_runtime._detect_runtime_binary()

        checks.append(_check_runtime_binary(runtime))
        checks.append(_check_ghcr_reachable())
        checks.append(_check_disk_space())
        # Conflict/volume checks are meaningless without a runtime; when none was
        # found _check_runtime_binary already records the fatal.
        if runtime is not None:
            checks.append(_check_name_conflicts(runtime))
            checks.append(_check_volume_exists(runtime))
        checks.append(_check_port_free(port))
    else:  # runner
        chosen = runner or _runner_from_models_output()
        if chosen is None:
            checks.append(
                _check(
                    "runner_selected",
                    passed=False,
                    started=t0,
                    error=(
                        "no runner specified and recommend-models output "
                        "not found at "
                        f"{install_state.outputs_dir() / 'recommend-models.json'}"
                    ),
                    remediation=(
                        "Run `agentalloy recommend-models` first, or pass "
                        "`--runner <llama-server|fastflowlm>` explicitly."
                    ),
                )
            )
        elif chosen == "llama-server":
            checks.append(_check_llama_server_present())
            # _check_llama_server_reachable omitted: llama-server is started by
            # start-embed-server (later in the pipeline), not pre-running like ollama.
        elif chosen == "fastflowlm":
            checks.append(_check_fastflowlm_present())
        else:
            checks.append(
                _check(
                    "runner_supported",
                    passed=True,
                    started=t0,
                    severity="warn",
                    detail=(
                        f"runner {chosen!r} has no preflight coverage "
                        "(manual-pull runner: LM Studio, MLX, vLLM)"
                    ),
                )
            )

    fatal_failed = [c for c in checks if not c["passed"] and c.get("severity", "fatal") == "fatal"]
    warn_failed = [c for c in checks if not c["passed"] and c.get("severity") == "warn"]

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "preflight" if not fatal_failed else "preflight_failed",
        "phase": phase,
        "runner": runner if phase == "runner" else None,
        "all_checks_passed": not fatal_failed and not warn_failed,
        "fatal_failures": [c["name"] for c in fatal_failed],
        "warn_failures": [c["name"] for c in warn_failed],
        "checks": checks,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "preflight",
        help=(
            "Verify host prerequisites (uv, PATH, network, runner) before "
            "running install steps. Run this first, every time."
        ),
    )
    p.add_argument(
        "--phase",
        choices=_PHASES,
        default="early",
        help="Which phase of checks to run (default: early).",
    )
    p.add_argument(
        "--runner",
        default=None,
        help=(
            "Runner-phase only: which runner to validate. If omitted, read "
            "from recommend-models.json output."
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Port to test for availability (default: {_DEFAULT_PORT}).",
    )

    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render preflight check results in human-readable format."""
    from agentalloy.install.output import render_checklist

    phase = result.get("phase", "early")
    render_checklist(result, title=f"Preflight ({phase})")

    # Print a warning if there were non-fatal issues
    warns = [c for c in result["checks"] if not c["passed"] and c.get("severity") == "warn"]
    if warns:
        from agentalloy.install.output import print_rich

        print_rich()
        print_rich(f"  [yellow]{len(warns)} warning(s) — non-fatal, install can proceed.[/yellow]")


def _run(args: argparse.Namespace) -> int:
    result = run_preflight(
        phase=args.phase,
        runner=args.runner,
        port=args.port,
        runtime=None,
    )
    install_state.save_output_file(result, f"preflight-{args.phase}.json")
    write_result(result, args, human_fn=_render_human)

    # Return non-zero if any fatal checks failed
    fatal = [
        c for c in result["checks"] if not c["passed"] and c.get("severity", "fatal") == "fatal"
    ]
    return 1 if fatal else 0
