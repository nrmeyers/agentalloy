"""``enable-service`` subcommand.

Registers AgentAlloy as a persistent background service so it starts
automatically without requiring ``agentalloy serve`` each session.

Two modes
---------
native
    Linux: writes three systemd user units and enables + starts them
    (no root required):
      - agentalloy.service        — the FastAPI service
      - agentalloy-embed.service  — embed llama-server (47951, --embeddings)
      - agentalloy-rerank.service — reranker llama-server (47952, completions)

    macOS: writes three launchd LaunchAgent plists and loads them:
      - ai.agentalloy.plist        — the FastAPI service
      - ai.agentalloy.embed.plist  — embed llama-server (47951, --embeddings)
      - ai.agentalloy.rerank.plist — reranker llama-server (47952, completions)
    All three use RunAtLoad + KeepAlive so they auto-start at login and
    restart on crash.

    Windows: not implemented (v1.1).

manual
    No-op: prints the ``agentalloy serve`` command and exits. Records the
    choice in state so subsequent steps know the mode.

The persistent *container* deployment is no longer enabled here: the
single-container GHCR model (``setup --deployment container``) runs the
container detached with ``--restart unless-stopped``, so it already persists
across reboots without a compose service.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.sax.saxutils
from pathlib import Path
from typing import Any

from agentalloy.install import runtime_artifacts
from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result
from agentalloy.install.subcommands.start_rerank_server import (
    _RERANK_CTX,
    _RERANK_PARALLEL,
)

logger = __import__("logging").getLogger(__name__)

SCHEMA_VERSION = 1

# How long to poll /health after starting the container stack.
_HEALTH_TIMEOUT_S = 30
_HEALTH_POLL_S = 2


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_os() -> str:
    """Return 'linux', 'macos', or 'windows'."""
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    return "linux"


def _native_available() -> bool:
    """True if a supported native service manager is present."""
    os_name = _detect_os()
    if os_name == "linux":
        return shutil.which("systemctl") is not None
    if os_name == "macos":
        return shutil.which("launchctl") is not None
    return False


def _poll_health(port: int) -> bool:
    """Poll /health until ok/degraded or timeout. Returns True on success."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
                status = data.get("status")
                if status == "ok":
                    return True
                if status == "degraded":
                    print(
                        "NOTE: Service is degraded (model still warming up). "
                        "It will become fully available shortly.",
                        file=sys.stderr,
                    )
                    return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(_HEALTH_POLL_S)
    return False


# ---------------------------------------------------------------------------
# Native: Linux systemd user unit
# ---------------------------------------------------------------------------


def _systemd_unit_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    unit_dir = config_home / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    return unit_dir / "agentalloy.service"


def _sanitize_env_for_systemd(env_path: Path) -> Path:
    """Write a systemd-compatible env file (no export, no quotes, no shell expansion).

    systemd's EnvironmentFile parser is strict: bare KEY=VALUE only.
    Returns path to the sanitized file (written next to the original).
    """
    sanitized_path = env_path.parent / "agentalloy.env"
    lines: list[str] = []
    for raw in (env_path.read_text() if env_path.exists() else "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            lines.append(f"{key}={val}")
    install_state._atomic_write(sanitized_path, "\n".join(lines) + "\n")  # pyright: ignore[reportPrivateUsage]
    return sanitized_path


def _render_systemd_unit(uv_bin: str, repo_root: Path, port: int, env_path: Path) -> str:
    uvicorn_bin = repo_root / ".venv" / "bin" / "uvicorn"
    if uvicorn_bin.exists():
        exec_start = f"{uvicorn_bin} agentalloy.app:app --host 127.0.0.1 --port {port}"
        extra_env = f"Environment=VIRTUAL_ENV={repo_root / '.venv'}\n"
    else:
        exec_start = f"{uv_bin} run uvicorn agentalloy.app:app --host 127.0.0.1 --port {port}"
        extra_env = f"Environment=HOME={Path.home()}\n"
    return (
        "[Unit]\n"
        "Description=AgentAlloy skill composition service\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={env_path}\n"
        f"{extra_env}"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={repo_root}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


# llama-server (llama.cpp) is the sole inference runner. Two dedicated
# instances back the runtime: the embed server (47951, --embeddings mode) and
# the reranker server (47952, completions mode for /v1/completions logprobs).
_LLAMA_EMBED_PORT = 47951
_LLAMA_RERANK_PORT = 47952

# Foreign-safe cmdline matchers keyed by port. Sourced from
# runtime_artifacts.RUNTIME_PORTS (the single source of truth) so the matchers
# passed to _reclaim_port can never drift from the reaping logic in
# runtime_artifacts. port int -> tuple of cmdline substrings.
_PORT_MATCH = dict(runtime_artifacts.RUNTIME_PORTS)


def _llama_unit_path(name: str) -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    unit_dir = config_home / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    return unit_dir / name


def _render_llama_embed_unit(llama_bin: str, model_path: Path, ngl: int = 0) -> str:
    ngl_flag = f" -ngl {ngl}" if ngl > 0 else ""
    return (
        "[Unit]\n"
        "Description=AgentAlloy embedding server (llama-server)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={llama_bin} --embeddings --pooling mean --port {_LLAMA_EMBED_PORT} "
        f"--ubatch-size 2048{ngl_flag} -m {model_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _render_llama_rerank_unit(llama_bin: str, model_path: Path, ngl: int = 0) -> str:
    # Completions mode — NO --embeddings — so /v1/completions logprobs are served.
    # --parallel/-c match start_rerank_server's CLI launcher so an `enable-service`
    # install matches what `agentalloy start-rerank-server` would launch; without
    # them llama.cpp auto-picks n_parallel=4 and Stage B oversubscribes the slots
    # (compose fans out up to LM_ASSIST_MAX_CANDIDATES=8 docs per request).
    # ExecStartPost warms the KV-cache graph by sending one /v1/completions before
    # any real Stage B traffic — eliminates the first-request fallback after a
    # cold restart (rerank cold prompt-eval ~1.2s > per-req timeout 1.35s).
    ngl_flag = f" -ngl {ngl}" if ngl > 0 else ""
    agentalloy_bin = shutil.which("agentalloy") or "agentalloy"
    return (
        "[Unit]\n"
        "Description=AgentAlloy reranker server (llama-server)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={llama_bin} --port {_LLAMA_RERANK_PORT}{ngl_flag} -m {model_path}"
        f" --parallel {_RERANK_PARALLEL} -c {_RERANK_CTX}\n"
        # `-` prefix: a warmup error must NEVER mark the unit failed (the breaker
        # at request time is the real safety net; warmup is best-effort).
        f"ExecStartPost=-{agentalloy_bin} rerank-warmup\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _ngl_for_target(target: str | None) -> int:
    """GPU-offload layer count for a hardware target (0 = CPU, no offload).

    Shares start_rerank_server's mapping so persistent units match the
    setup-time launch flags.
    """
    from agentalloy.install.subcommands.start_rerank_server import (
        _DEFAULT_NGL,
        _NGL_BY_TARGET,
    )

    return _NGL_BY_TARGET.get(target or "cpu", _DEFAULT_NGL)


def _resolve_preset(st: dict[str, Any]) -> str | None:
    """Resolve the hardware preset that selects ``-ngl`` for the persistent
    embed/reranker llama-servers.

    The preset (cpu/apple-silicon/nvidia/radeon) is written to
    ``recommend-models.json`` by ``recommend-models`` and read from there by the
    setup-time launchers (``start-embed-server`` / ``start-rerank-server``). It
    is NOT persisted to install state, so reading ``st.get("preset")`` always
    resolved to ``None`` — and the persistent LaunchAgents/systemd units were
    registered without ``-ngl`` (CPU-only) on every GPU host, contradicting the
    setup-time launch and the renderers' documented offload intent.

    Resolve from recommend-models.json first (the same source the launchers
    use); fall back to install state, then ``None`` (→ CPU, the safe default).
    """
    models_fp = install_state.outputs_dir() / "recommend-models.json"
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(models_fp.read_text())
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError):
        data = {}
    preset = data.get("preset")
    if isinstance(preset, str) and preset.strip():
        return preset.strip().lower()
    return st.get("preset")


def _model_path(model_file: str) -> Path:
    return install_state.user_data_dir() / "models" / model_file


def _reclaim_port(unit_name: str, port: int, match: list[str]) -> None:
    """Stop *unit_name*, then kill any stale matching process still on *port*.

    Run just before ``enable --now`` so that an orphaned process left behind by
    ``uv tool install --force`` (which a crash-looping unit can't displace) does
    not block the new unit from binding. Stopping the unit first means we never
    needlessly kill a *healthy* unit-managed process; the reclaim then clears
    only an orphan whose ``/proc`` cmdline matches our own signature.
    """
    from agentalloy.install.server_proc import reclaim_stale_port

    subprocess.run(
        ["systemctl", "--user", "stop", unit_name],
        check=False,
        capture_output=True,
        text=True,
    )
    pid = reclaim_stale_port(port, match)
    if pid is not None:
        logger.info(
            "reclaimed stale holder pid=%d on port %d before enabling %s", pid, port, unit_name
        )


def _write_llama_units(target: str | None = None) -> list[str]:
    """Write + enable the embed (47951) and reranker (47952) llama-server units.

    Returns the list of unit paths written (empty if llama-server is absent).
    Best-effort: a single unit's enable failure is logged, not fatal — the
    agentalloy.service unit is the only hard requirement. ``target`` is the
    hardware preset (cpu/nvidia/radeon/apple-silicon) — it selects ``-ngl`` so
    the persistent units offload to the GPU like the setup-time launch did.
    """
    llama_bin = shutil.which("llama-server")
    if not llama_bin:
        logger.warning("llama-server not on PATH; skipping embed/reranker units")
        return []

    ngl = _ngl_for_target(target)
    written: list[str] = []
    units = [
        (
            "agentalloy-embed.service",
            _render_llama_embed_unit(
                llama_bin, _model_path("nomic-embed-text-v1.5.Q8_0.gguf"), ngl
            ),
        ),
        (
            "agentalloy-rerank.service",
            _render_llama_rerank_unit(llama_bin, _model_path("Qwen3-Reranker-0.6B-Q8_0.gguf"), ngl),
        ),
    ]
    # Pass 1: write all unit files BEFORE enabling any of them.
    for unit_name, content in units:
        unit_path = _llama_unit_path(unit_name)
        install_state._atomic_write(unit_path, content)  # pyright: ignore[reportPrivateUsage]
        written.append(str(unit_path))
    # systemd does not see freshly written unit files until a daemon-reload, so
    # reload here — between writing and enabling — or first-install `enable --now`
    # fails with "Unit ... not found" and the embed/rerank servers never start.
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    # Reclaim the embed/rerank ports from any stale llama-server squatting them
    # (e.g. an orphan left by `uv tool install --force`) so the units can bind —
    # otherwise they crash-loop with "couldn't bind HTTP server socket".
    _reclaim_port(
        "agentalloy-embed.service",
        _LLAMA_EMBED_PORT,
        list(_PORT_MATCH[runtime_artifacts.EMBED_PORT]),
    )
    _reclaim_port(
        "agentalloy-rerank.service",
        _LLAMA_RERANK_PORT,
        list(_PORT_MATCH[runtime_artifacts.RERANK_PORT]),
    )
    # Pass 2: enable + start the now-visible units.
    for unit_name, _content in units:
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", unit_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "%s enable failed (rc=%d): %s",
                unit_name,
                result.returncode,
                (result.stderr or "").strip(),
            )
    return written


def _enable_native_linux(
    uv_bin: str,
    repo_root: Path,
    port: int,
    preset: str | None,
) -> dict[str, Any]:
    env_path = _sanitize_env_for_systemd(install_state.env_path())
    unit_path = _systemd_unit_path()
    content = _render_systemd_unit(uv_bin, repo_root, port, env_path)
    install_state._atomic_write(unit_path, content)  # pyright: ignore[reportPrivateUsage]

    llama_units = _write_llama_units(preset)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    # Reclaim port 47950 from a stale uvicorn (an orphan left by `--force`
    # reinstall also holds the corpus DuckDB lock; killing it releases both).
    _reclaim_port("agentalloy.service", port, list(_PORT_MATCH[runtime_artifacts.SERVICE_PORT]))
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "agentalloy.service"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agentalloy.service enable failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )

    return {
        "unit_path": str(unit_path),
        "llama_units_written": llama_units,
        # Retained for output-schema backward compatibility; ollama is no
        # longer provisioned now that llama-server is the sole runner.
        "ollama_unit_written": False,
    }


# ---------------------------------------------------------------------------
# Native: macOS launchd plist
# ---------------------------------------------------------------------------


def _launchd_plist_path() -> Path:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir / "ai.agentalloy.plist"


def _xml_str(value: str) -> str:
    return f"<string>{xml.sax.saxutils.escape(value)}</string>"


def _render_launchd_plist(uv_bin: str, repo_root: Path, port: int, env_vars: dict[str, str]) -> str:
    env_entries = "\n".join(
        f"    <key>{xml.sax.saxutils.escape(k)}</key>\n    {_xml_str(v)}"
        for k, v in env_vars.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        "  <string>ai.agentalloy</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    {_xml_str(uv_bin)}\n"
        "    <string>run</string>\n"
        "    <string>uvicorn</string>\n"
        "    <string>agentalloy.app:app</string>\n"
        "    <string>--host</string>\n"
        "    <string>127.0.0.1</string>\n"
        "    <string>--port</string>\n"
        f"    {_xml_str(str(port))}\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  {_xml_str(str(repo_root))}\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_entries}\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        "  <string>/tmp/agentalloy.log</string>\n"
        "  <key>StandardErrorPath</key>\n"
        "  <string>/tmp/agentalloy.log</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _read_env_file(env_path: Path) -> dict[str, str]:
    """Parse .env into a dict for inlining into the launchd plist."""
    if not env_path.exists():
        return {}
    env_vars: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            env_vars[key] = val
    return env_vars


def _llama_launchd_plist_path(label: str) -> Path:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return agents_dir / f"{label}.plist"


def _render_llama_launchd_plist(label: str, program_args: list[str]) -> str:
    """Render a launchd plist for a llama-server instance.

    RunAtLoad + KeepAlive give the embed/reranker servers the same
    auto-start-and-restart guarantee the systemd units provide on Linux.
    Logs go to a per-label file under /tmp.
    """
    arg_lines = "\n".join(f"    {_xml_str(a)}" for a in program_args)
    log_path = f"/tmp/{label}.log"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  {_xml_str(label)}\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{arg_lines}\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        f"  {_xml_str(log_path)}\n"
        "  <key>StandardErrorPath</key>\n"
        f"  {_xml_str(log_path)}\n"
        "</dict>\n"
        "</plist>\n"
    )


def _write_llama_launchd_agents(target: str | None = None) -> list[str]:
    """Write + load LaunchAgent plists for the embed (47951) and reranker (47952)
    llama-servers — the macOS mirror of ``_write_llama_units``.

    Returns the list of plist paths written (empty if llama-server is absent).
    Best-effort: skipped gracefully (logged, non-fatal) when llama-server is
    not on PATH, matching the systemd path. ``target`` selects ``-ngl`` so the
    persistent agents Metal-offload like the setup-time launch did.
    """
    llama_bin = shutil.which("llama-server")
    if not llama_bin:
        logger.warning("llama-server not on PATH; skipping embed/reranker LaunchAgents")
        return []

    embed_model = _model_path("nomic-embed-text-v1.5.Q8_0.gguf")
    rerank_model = _model_path("Qwen3-Reranker-0.6B-Q8_0.gguf")
    ngl = _ngl_for_target(target)
    ngl_args = ["-ngl", str(ngl)] if ngl > 0 else []

    written: list[str] = []
    agents = [
        # Embed: --embeddings mode on 47951 (matches _render_llama_embed_unit).
        (
            "ai.agentalloy.embed",
            [
                llama_bin,
                "--embeddings",
                "--pooling",
                "mean",
                "--port",
                str(_LLAMA_EMBED_PORT),
                "--ubatch-size",
                "2048",
                *ngl_args,
                "-m",
                str(embed_model),
            ],
        ),
        # Reranker: completions mode on 47952 — NO --embeddings.
        (
            "ai.agentalloy.rerank",
            [llama_bin, "--port", str(_LLAMA_RERANK_PORT), *ngl_args, "-m", str(rerank_model)],
        ),
    ]
    for label, program_args in agents:
        plist_path = _llama_launchd_plist_path(label)
        content = _render_llama_launchd_plist(label, program_args)
        install_state._atomic_write(plist_path, content)  # pyright: ignore[reportPrivateUsage]
        os.chmod(plist_path, 0o600)
        written.append(str(plist_path))
        # Unload first for idempotent re-runs, then load.
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "%s load failed (rc=%d): %s",
                label,
                result.returncode,
                (result.stderr or "").strip(),
            )
    return written


def _enable_native_macos(
    uv_bin: str,
    repo_root: Path,
    port: int,
    preset: str | None,
) -> dict[str, Any]:
    env_vars = _read_env_file(install_state.env_path())
    plist_path = _launchd_plist_path()
    content = _render_launchd_plist(uv_bin, repo_root, port, env_vars)
    install_state._atomic_write(plist_path, content)  # pyright: ignore[reportPrivateUsage]
    os.chmod(plist_path, 0o600)

    # Unload first in case it's already loaded (idempotent re-run).
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)

    # Register the embed (47951) and reranker (47952) llama-servers as
    # LaunchAgents so they auto-start at login and restart on crash — the
    # macOS mirror of the systemd units written on Linux.
    llama_agents = _write_llama_launchd_agents(preset)

    return {
        "unit_path": str(plist_path),
        "llama_units_written": llama_agents,
        "ollama_unit_written": False,
    }


# ---------------------------------------------------------------------------
# Main enable_service function
# ---------------------------------------------------------------------------


def enable_service(
    mode: str,
    port: int = 47950,
    repo_root: Path | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """Enable the AgentAlloy service. Returns the contract-shaped result."""
    if repo_root is None:
        # Best-effort: find the package root from this file's location.
        repo_root = Path(__file__).resolve().parents[4]

    uv_bin = shutil.which("uv") or sys.executable

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "runtime": None,
        "unit_path": None,
        "llama_units_written": [],
        "ollama_unit_written": False,
        "service_started": False,
    }

    if mode == "native":
        os_name = _detect_os()
        if os_name == "windows":
            print("ERROR: Native service mode is not supported on Windows (v1.1).", file=sys.stderr)
            print("FIX:   Use --mode manual.", file=sys.stderr)
            raise SystemExit(1)
        if os_name == "linux":
            details = _enable_native_linux(uv_bin, repo_root, port, preset)
        else:
            details = _enable_native_macos(uv_bin, repo_root, port, preset)
        result.update(details)
        result["service_started"] = True

    elif mode == "manual":
        print(
            "To start agentalloy manually, run:\n\n    agentalloy serve\n\n"
            "Leave it running in a terminal while you work.",
            file=sys.stderr,
        )
        result["service_started"] = False

    else:
        print(f"ERROR: Unknown mode '{mode}'. Use native or manual.", file=sys.stderr)
        raise SystemExit(1)

    return result


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "enable-service",
        help="Register AgentAlloy as a persistent background service.",
    )
    p.add_argument(
        "--mode",
        choices=["native", "manual"],
        default=None,
        help="Service mode. If omitted, available modes are detected and the user is prompted.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Service port override (default: read from user state, fallback 47950).",
    )
    add_json_flag(p)
    p.set_defaults(func=run)


def _render_human(result: dict[str, Any]) -> None:
    """Render enable service result in human-readable format."""
    mode = result.get("mode", "unknown")
    runtime = result.get("runtime", "")
    unit_path = result.get("unit_path", "")
    action = result.get("action", "enabled")

    print_rich("\n  [bold]Enable Service[/bold]\n")
    print_rich(f"  Mode: [bold]{mode}[/bold]")
    if runtime:
        print_rich(f"  Runtime: {runtime}")
    print_rich(f"  Status: {action}")
    if unit_path:
        print_rich(f"  Unit: {unit_path}")

    print_rich()


def run(args: argparse.Namespace) -> int:
    st = install_state.load_state()
    port = install_state.validate_port(
        args.port if args.port is not None else st.get("port", 47950)
    )
    preset: str | None = _resolve_preset(st)

    mode = args.mode
    if mode is None:
        mode = _prompt_mode()

    result = enable_service(mode=mode, port=port, preset=preset)

    fp, digest = install_state.save_output_file(result, "enable-service.json")
    install_state.record_step(
        st,
        "enable-service",
        extra={
            "output_digest": digest,
            "output_path": str(fp),
            "mode": result["mode"],
            "runtime": result["runtime"],
            "unit_path": result["unit_path"],
        },
    )
    st["service_mode"] = result["mode"]
    st["service_runtime"] = result["runtime"]
    st["service_unit_path"] = result["unit_path"]
    install_state.save_state(st)

    write_result(result, args, human_fn=_render_human)
    return 0


# ---------------------------------------------------------------------------
# Interactive prompts (only used when args are not pre-supplied)
# ---------------------------------------------------------------------------


def _prompt_mode() -> str:
    os_name = _detect_os()
    native_ok = _native_available()

    options: list[tuple[str, str]] = []
    if native_ok:
        mgr = "systemd" if os_name == "linux" else "launchd"
        options.append(("native", f"Persistent — native service ({mgr}, starts at login)"))
    options.append(("manual", "Manual — I'll run `agentalloy serve` myself"))

    print("\nHow should AgentAlloy run between coding sessions?", file=sys.stderr)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}", file=sys.stderr)
    print(file=sys.stderr)

    while True:
        try:
            raw = input(f"Choice [1–{len(options)}] (default 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            raise SystemExit(1) from None
        if raw == "":
            return options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(f"  Please enter a number between 1 and {len(options)}.", file=sys.stderr)
