"""``enable-service`` subcommand.

Registers AgentAlloy as a persistent background service so it starts
automatically without requiring ``agentalloy serve`` each session.

Two modes
---------
native
    Linux: writes a systemd user unit (~/.config/systemd/user/agentalloy.service)
    and enables + starts it. No root required.

    macOS: writes a launchd LaunchAgent plist
    (~/Library/LaunchAgents/ai.agentalloy.plist) and loads it.

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

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

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


def _ollama_unit_exists() -> bool:
    for scope in (["--user"], ["--system", "--global"]):
        try:
            result = subprocess.run(
                ["systemctl", *scope, "cat", "ollama.service"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def _write_ollama_unit(uv_bin: str) -> Path | None:  # noqa: ARG001
    """Write a minimal ollama user unit. Returns path or None on failure."""
    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        return None
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    unit_dir = config_home / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "ollama.service"
    content = (
        "[Unit]\n"
        "Description=Ollama embedding service\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={ollama_bin} serve\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    install_state._atomic_write(unit_path, content)  # pyright: ignore[reportPrivateUsage]
    return unit_path


def _enable_native_linux(
    uv_bin: str, repo_root: Path, port: int, preset: str | None
) -> dict[str, Any]:
    env_path = _sanitize_env_for_systemd(install_state.env_path())
    unit_path = _systemd_unit_path()
    content = _render_systemd_unit(uv_bin, repo_root, port, env_path)
    install_state._atomic_write(unit_path, content)  # pyright: ignore[reportPrivateUsage]

    ollama_unit_written = False
    ollama_enabled = False
    if preset != "radeon" and not _ollama_unit_exists():
        written = _write_ollama_unit(uv_bin)
        ollama_unit_written = written is not None
        if ollama_unit_written:
            result = subprocess.run(
                ["systemctl", "--user", "enable", "--now", "ollama.service"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                ollama_enabled = True
            else:
                logger.warning(
                    "ollama.service enable failed (rc=%d): %s",
                    result.returncode,
                    (result.stderr or "").strip(),
                )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "agentalloy.service"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Roll back ollama enable if agentalloy enable failed.
        if ollama_enabled:
            try:
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", "ollama.service"],
                    capture_output=True,
                    text=True,
                )
                logger.warning("Rolled back ollama.service enable after agentalloy.service failure")
            except Exception:
                pass
        raise RuntimeError(
            f"agentalloy.service enable failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )

    if preset == "radeon":
        print(
            "NOTE: LM Studio cannot be managed by systemd. Enable 'Start on Login' "
            "in LM Studio → Settings so the embedding server starts automatically.",
            file=sys.stderr,
        )

    return {
        "unit_path": str(unit_path),
        "ollama_unit_written": ollama_unit_written,
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


def _enable_native_macos(
    uv_bin: str, repo_root: Path, port: int, preset: str | None
) -> dict[str, Any]:
    env_vars = _read_env_file(install_state.env_path())
    plist_path = _launchd_plist_path()
    content = _render_launchd_plist(uv_bin, repo_root, port, env_vars)
    install_state._atomic_write(plist_path, content)  # pyright: ignore[reportPrivateUsage]
    os.chmod(plist_path, 0o600)

    # Unload first in case it's already loaded (idempotent re-run).
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)

    if preset == "radeon":
        print(
            "NOTE: LM Studio cannot be managed by launchd. Enable 'Start on Login' "
            "in LM Studio → Settings so the embedding server starts automatically.",
            file=sys.stderr,
        )

    return {
        "unit_path": str(plist_path),
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
    preset: str | None = st.get("preset")

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
