"""``uninstall`` subcommand.

Full teardown for a agentalloy install. By default removes:

- Harness wiring (sentinel-bounded blocks) in every repo recorded in
  ``install-state.json#harness_files_written`` — not just cwd. Each
  entry is validated against its own recorded ``repo_root`` plus the
  suffix allowlist + sha256 tamper check, so a tampered state file
  can't redirect deletion. Pass ``--no-all-repos`` to limit cleanup
  to cwd (matching the legacy behavior).
- Native systemd / launchd service units (agentalloy).
- A manual-mode agentalloy server still listening on the configured
  port.
- User-scope ``.env`` and ``install-state.json``.
- Outputs directory (``${XDG_DATA_HOME}/agentalloy/outputs/``) and
  ``server.log`` — derivable artifacts that hold no user content.
- The ``uv tool`` installation of agentalloy.
- Container image (``ghcr.io/nrmeyers/agentalloy:latest``) for container
  deployments.

With ``--remove-data`` (or the ``full`` preset), also removes:

- Container named volumes (``agentalloy-data``).
- AgentAlloy download cache (``~/.cache/agentalloy``).
- Skills datastore (corpus DB) and the entire ``${XDG_DATA_HOME}/agentalloy/``
  directory.

Preserves the corpus DB (``${XDG_DATA_HOME}/agentalloy/corpus/``) by
default — pass ``--remove-data`` to wipe the entire user_data_dir.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import uninstall_proxy
from agentalloy.install.subcommands.container_runtime import DEFAULT_IMAGE
from agentalloy.install.subcommands.wire_harness import SENTINEL_BEGIN, SENTINEL_END

SCHEMA_VERSION = 1


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Interactive prompt helpers (preset menu + per-item yes/no)
# ---------------------------------------------------------------------------


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    """Yes/no prompt that mirrors ``reset.py``'s confirmation pattern.

    Default is shown in the prompt; bare Enter accepts it. EOF/Ctrl-C
    returns the default (treat as "skip" for safety on dirty input).
    """
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"  {question} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def _prompt_uninstall_preset() -> str:
    """Show the top-level uninstall menu. Returns 'keep-data', 'full', or 'custom'.

    Falls back to ``full`` on any input error — the user said "uninstall",
    so the default should actually uninstall everything.
    """
    print("", file=sys.stderr)
    print("  What kind of uninstall?", file=sys.stderr)
    print(
        "    1) Full uninstall            — remove everything (default)",
        file=sys.stderr,
    )
    print(
        "    2) Keep data, just unwire    "
        "— remove harness wiring + .env; keep models, datastore, services",
        file=sys.stderr,
    )
    print("    3) Custom                    — ask per-item", file=sys.stderr)
    try:
        raw = input("  Choice [1-3] (default 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        return "full"
    if raw == "2":
        return "keep-data"
    if raw == "3":
        return "custom"
    return "full"


def _prompt_uninstall_custom() -> dict[str, bool]:
    """Ask per-item yes/no questions for a custom uninstall.

    Returns a dict with keys: ``stop_services``, ``remove_models``,
    ``remove_datastore``, ``remove_wiring``, ``remove_env_state``.
    All default to False so a confused user can answer "no" to everything
    and end up with a no-op rather than a surprise teardown.
    """
    print("", file=sys.stderr)
    return {
        "stop_services": _prompt_yes_no("Stop running services (embed + reranker llama-server)?"),
        "remove_models": _prompt_yes_no("Remove pulled models (llama-server GGUF cache)?"),
        "remove_datastore": _prompt_yes_no("Remove skills datastore (corpus DB)?"),
        "remove_wiring": _prompt_yes_no(
            "Remove harness wiring (CLAUDE.md, .cursorrules, MCP entries, etc.)?"
        ),
        "remove_env_state": _prompt_yes_no("Remove .env and install-state directory?"),
    }


# ---------------------------------------------------------------------------
# Model & daemon teardown helpers
# ---------------------------------------------------------------------------


def _remove_pulled_models(st: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove every model recorded in ``state["models_pulled"]``.

    Entries are ``"<runner>:<model>"``. llama-server models are GGUF files
    under ``${XDG_DATA_HOME}/agentalloy/models/``. Missing files are warnings,
    not errors — the goal is best-effort cleanup.
    """
    actions: list[dict[str, Any]] = []
    pulled: list[Any] = st.get("models_pulled") or []
    if not pulled:
        return actions

    models_dir = install_state.user_data_dir() / "models"

    for entry in pulled:
        if not isinstance(entry, str) or ":" not in entry:
            actions.append({"entry": entry, "action": "skipped_malformed_entry"})
            continue
        runner, _, model = entry.partition(":")
        runner = runner.strip()
        model = model.strip()
        if not runner or not model:
            actions.append({"entry": entry, "action": "skipped_empty_fields"})
            continue

        if runner == "llama-server":
            gguf_path = models_dir / model
            if gguf_path.exists():
                try:
                    gguf_path.unlink()
                    actions.append(
                        {
                            "runner": runner,
                            "model": model,
                            "path": str(gguf_path),
                            "action": "gguf_removed",
                        }
                    )
                except OSError as exc:
                    actions.append(
                        {
                            "runner": runner,
                            "model": model,
                            "path": str(gguf_path),
                            "action": "gguf_remove_failed",
                            "error": str(exc),
                        }
                    )
            else:
                actions.append(
                    {
                        "runner": runner,
                        "model": model,
                        "path": str(gguf_path),
                        "action": "gguf_already_absent",
                    }
                )
        else:
            # Unknown runner (lm-studio, fastflowlm, etc.). We don't manage
            # those caches — surface the intent so the user can decide.
            actions.append(
                {
                    "runner": runner,
                    "model": model,
                    "action": "skipped_unmanaged_runner",
                    "hint": (
                        f"AgentAlloy doesn't track the {runner} model cache. "
                        "Remove it manually using the runner's own tooling."
                    ),
                }
            )
    return actions


# ---------------------------------------------------------------------------
# Container teardown
# ---------------------------------------------------------------------------


# Named volumes the container deployment may have created. Stopping/removing
# the container does NOT remove its named volumes — those survive uninstall by
# default, which means a "fresh" reinstall reuses the prior corpus. We remove
# them explicitly when the caller asks for --remove-data.
_COMPOSE_NAMED_VOLUMES: tuple[str, ...] = ("agentalloy-data",)


def _resolve_compose_binary(st: dict[str, Any]) -> str | None:
    """Return the absolute path or bare name of the container runtime
    binary (e.g. ``/usr/bin/podman`` or ``docker``), or None if state
    doesn't have enough info. Matches the resolution logic used by
    ``_stop_container_stack``.

    Reads ``runtime_binary`` (label e.g. ``"podman compose"``) from state
    and falls back to ``shutil.which`` on the binary name.
    """
    compose_binary_path = st.get("compose_binary_path")
    if compose_binary_path and Path(compose_binary_path).exists():
        return str(compose_binary_path)
    # v4+: read from runtime_binary (migrated from compose_binary)
    compose_binary_label = st.get("runtime_binary")
    if not isinstance(compose_binary_label, str) or not compose_binary_label:
        return None
    parts = compose_binary_label.split()
    return parts[0] if parts else None


def _remove_compose_volumes(
    st: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Force-remove the container deployment's named volumes.

    Must run AFTER ``_stop_container_stack`` — `volume rm` errors with
    "volume is being used" if any container still mounts it. Idempotent:
    missing volumes are logged but do not produce warnings.

    Returns a list of action dicts (one per volume attempted).
    """
    actions: list[dict[str, Any]] = []
    if st.get("deployment") != "container":
        return actions

    binary = _resolve_compose_binary(st)
    if binary is None:
        warnings.append(
            "Container deployment detected but runtime binary unresolved — "
            "skipping volume cleanup. Remove manually with your runtime: "
            f"`<podman|docker> volume rm -f {' '.join(_COMPOSE_NAMED_VOLUMES)}`"
        )
        return actions

    for vol in _COMPOSE_NAMED_VOLUMES:
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, binary resolved above
                [binary, "volume", "rm", "-f", vol],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except OSError as exc:
            warnings.append(f"volume rm {vol}: binary not found ({binary}): {exc}")
            actions.append({"action": "volume_rm_skipped", "volume": vol, "error": str(exc)})
            continue
        except subprocess.TimeoutExpired:
            warnings.append(f"volume rm {vol} timed out after 30s")
            actions.append({"action": "volume_rm_timeout", "volume": vol})
            continue

        if result.returncode == 0:
            actions.append({"action": "volume_removed", "volume": vol})
        else:
            # `volume rm -f` on a non-existent volume returns non-zero on
            # podman but exits 0 on docker; treat both as success when the
            # error is "no such volume". Anything else is a real warning.
            stderr = (result.stderr or "").strip()
            if "no such volume" in stderr.lower() or "not found" in stderr.lower():
                actions.append({"action": "volume_already_gone", "volume": vol})
            else:
                warnings.append(f"volume rm {vol} failed: {stderr or 'unknown error'}")
                actions.append({"action": "volume_rm_failed", "volume": vol, "error": stderr})

    return actions


# ---------------------------------------------------------------------------
# Container image teardown
# ---------------------------------------------------------------------------


def _remove_container_image(
    st: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Remove the local container image after the container is gone.

    Must run AFTER ``_stop_container_stack`` — ``image rm`` fails if the
    image is still tagged by a running container. Idempotent: missing
    images are logged but do not produce warnings.

    Returns a list of action dicts (one per image attempted).
    """
    actions: list[dict[str, Any]] = []
    if st.get("deployment") != "container":
        return actions

    binary = _resolve_compose_binary(st)
    if binary is None:
        warnings.append(
            "Container deployment detected but runtime binary unresolved — "
            "skipping image cleanup. Remove manually with your runtime: "
            "`<podman|docker> rmi -f ghcr.io/nrmeyers/agentalloy:latest`"
        )
        return actions

    image_tag = st.get("image_tag") or DEFAULT_IMAGE
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, binary resolved above
            [binary, "rmi", "-f", image_tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        warnings.append(f"image rm: binary not found ({binary}): {exc}")
        actions.append({"action": "image_rm_skipped", "image": image_tag, "error": str(exc)})
        return actions
    except subprocess.TimeoutExpired:
        warnings.append(f"image rm {image_tag} timed out after 30s")
        actions.append({"action": "image_rm_timeout", "image": image_tag})
        return actions

    if result.returncode == 0:
        actions.append({"action": "image_removed", "image": image_tag})
    else:
        stderr = (result.stderr or "").strip()
        if "no such image" in stderr.lower() or "not found" in stderr.lower():
            actions.append({"action": "image_already_gone", "image": image_tag})
        else:
            warnings.append(f"image rm {image_tag} failed: {stderr or 'unknown error'}")
            actions.append({"action": "image_rm_failed", "image": image_tag, "error": stderr})

    return actions


# ---------------------------------------------------------------------------
# AgentAlloy cache directory teardown
# ---------------------------------------------------------------------------


def _remove_agentalloy_cache(warnings: list[str]) -> list[dict[str, Any]]:
    """Remove the AgentAlloy download cache (``~/.cache/agentalloy``).

    Only acts when the directory exists. Idempotent: missing directory
    is logged but does not produce warnings.

    Returns a list of action dicts (always one entry).
    """
    cache_dir = Path.home() / ".cache" / "agentalloy"
    if not cache_dir.exists():
        return [{"action": "cache_already_gone", "path": str(cache_dir)}]

    try:
        shutil.rmtree(cache_dir)
        return [{"action": "cache_removed", "path": str(cache_dir)}]
    except OSError as exc:
        warnings.append(f"Failed to remove AgentAlloy cache at {cache_dir}: {exc}")
        return [{"action": "cache_rm_failed", "path": str(cache_dir), "error": str(exc)}]


def _stop_container_stack(
    st: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Stop and remove container stack.

    Reads ``runtime_binary`` (label e.g. ``"podman compose"``) and
    ``image_tag`` (compose file path for v3 states, image tag for v4+)
    from state. For v4+ single-container model, uses direct container
    operations (stop + rm) instead of compose down.

    Returns a list of action dicts. On failure, adds warnings but continues.
    """
    actions: list[dict[str, Any]] = []

    # v4+: read from runtime_binary (migrated from compose_binary)
    runtime_binary_label = st.get("runtime_binary")
    container_name = st.get("container_name") or "agentalloy"

    if st.get("deployment") != "container":
        # State says no container deployment — but state records `deployment`
        # only at the END of the wizard, so a container install that hung or
        # was interrupted mid-bootstrap leaves deployment unset while its
        # (often Exited) container survives, holding the rootlessport
        # reservation and failing every later install with "address already
        # in use". Detect that corpse by name instead of trusting state.
        from agentalloy.install.subcommands.container_runtime import (
            _detect_runtime_binary,
            _list_conflicting_containers,
        )

        runtime = _detect_runtime_binary()
        if runtime is None:
            return actions
        port_raw = st.get("port")
        port = port_raw if isinstance(port_raw, int) else 47950
        conflicts = _list_conflicting_containers(runtime, container_name, port)
        named = [(n, s) for n, s in conflicts if n == container_name]
        if not named:
            for name, status in conflicts:
                warnings.append(
                    f"Container '{name}' ({status}) publishes port {port} but was not "
                    f"created by this install — leaving it; remove manually with "
                    f"`{runtime} rm {name}` if it blocks reinstall."
                )
            return actions
        warnings.append(
            f"State has no container deployment recorded, but container "
            f"'{container_name}' exists (likely an interrupted install) — removing it."
        )
        runtime_binary_label = runtime

    if not runtime_binary_label:
        warnings.append(
            "Container deployment detected but runtime_binary is missing in state — "
            "skipping container teardown."
        )
        return actions

    # Determine the binary name from the label (e.g. "podman compose" -> "podman")
    parts = runtime_binary_label.split()
    binary_name = parts[0] if parts else None
    if binary_name is None:
        warnings.append(
            f"Invalid runtime_binary label in state: {runtime_binary_label!r} — "
            "skipping container teardown."
        )
        return actions

    # For v4+ single-container model: stop and remove the container directly
    # instead of using compose down (which requires the compose file)
    try:
        # Stop the container
        result = subprocess.run(
            [binary_name, "stop", container_name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            actions.append({"action": "container_stopped", "container": container_name})
        else:
            stderr = result.stderr.strip() if result.stderr else "unknown error"
            # Container may already be stopped — that's OK
            if "no such container" in stderr.lower() or "not found" in stderr.lower():
                actions.append({"action": "container_already_stopped", "container": container_name})
            else:
                warnings.append(f"container stop failed: {stderr}")
                actions.append(
                    {
                        "action": "container_stop_failed",
                        "container": container_name,
                        "error": stderr,
                    }
                )
    except OSError as exc:
        warnings.append(f"container stop: binary not found ({binary_name}): {exc}")
        actions.append(
            {"action": "container_stop_skipped", "container": container_name, "error": str(exc)}
        )
    except subprocess.TimeoutExpired:
        warnings.append("container stop timed out after 60s")
        actions.append({"action": "container_stop_timeout", "container": container_name})

    # Remove the container
    try:
        result = subprocess.run(
            [binary_name, "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            actions.append({"action": "container_removed", "container": container_name})
        else:
            stderr = result.stderr.strip() if result.stderr else "unknown error"
            if "no such container" in stderr.lower():
                actions.append({"action": "container_already_gone", "container": container_name})
            else:
                warnings.append(f"container rm failed: {stderr}")
                actions.append(
                    {"action": "container_rm_failed", "container": container_name, "error": stderr}
                )
    except OSError as exc:
        warnings.append(f"container rm: binary not found ({binary_name}): {exc}")
        actions.append(
            {"action": "container_rm_skipped", "container": container_name, "error": str(exc)}
        )
    except subprocess.TimeoutExpired:
        warnings.append("container rm timed out after 30s")
        actions.append({"action": "container_rm_timeout", "container": container_name})

    return actions


def _extract_sentinel_content(text: str, begin: str, end: str) -> str | None:
    """Extract the content between sentinel markers, or None if not found."""
    if begin not in text or end not in text:
        return None
    b = text.index(begin) + len(begin)
    e = text.index(end)
    return text[b:e].strip()


def _remove_sentinel_block(text: str, begin: str, end: str) -> str:
    """Remove the sentinel block (inclusive) from text."""
    if begin not in text or end not in text:
        return text

    b = text.index(begin)
    e = text.index(end) + len(end)

    # Validate order: if end appears before begin, there's no valid block
    # to remove; return content unchanged.
    end_marker_start = text.index(end)
    if end_marker_start < b:
        return text

    # Consume trailing newline
    if e < len(text) and text[e] == "\n":
        e += 1
    elif e + 1 < len(text) and text[e : e + 2] == "\r\n":
        e += 2
    # Consume blank line before block if present
    if b > 0 and text[b - 1] == "\n":
        b -= 1
        if b > 0 and text[b - 1] == "\n":
            b -= 1
    result = text[:b] + text[e:]
    # Clean up double blank lines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def _stop_native_service(st: dict[str, Any]) -> list[dict[str, Any]]:
    """Stop and disable the native systemd/launchd service if one was registered."""
    actions: list[dict[str, Any]] = []
    mode = st.get("service_mode")
    unit_path_str = st.get("service_unit_path")
    if mode != "native" or not unit_path_str:
        return actions

    unit_path = Path(unit_path_str)
    os_name = sys.platform

    if os_name == "linux" and unit_path.suffix == ".service":
        unit_name = unit_path.name
        for cmd in (
            ["systemctl", "--user", "disable", "--now", unit_name],
            ["systemctl", "--user", "daemon-reload"],
        ):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(cmd, capture_output=True, timeout=10)
        if unit_path.exists():
            unit_path.unlink()
            actions.append({"path": str(unit_path), "action": "deleted_systemd_unit"})
        # Also remove the sanitized env file written alongside the unit
        sanitized = unit_path.parent / "agentalloy.env"
        if sanitized.exists():
            sanitized.unlink()
            actions.append({"path": str(sanitized), "action": "deleted_systemd_env"})

    elif os_name == "darwin" and unit_path.suffix == ".plist":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["launchctl", "unload", "-w", str(unit_path)],
                capture_output=True,
                timeout=10,
            )
        if unit_path.exists():
            unit_path.unlink()
            actions.append({"path": str(unit_path), "action": "deleted_launchd_plist"})

    return actions


def _detect_install_mode() -> dict[str, Any]:
    """Detect how the agentalloy CLI was installed.

    Returns a dict with keys:
        mode: "uv_tool" | "pipx" | "editable" | "unknown"
        binary_path: absolute path to the agentalloy shim, or None
        venv_path: absolute path to the owning venv, or None
        details: human-readable description

    Detection order (first match wins):
        1. uv tool list — if agentalloy appears, mode is "uv_tool"
        2. pipx list — if agentalloy appears, mode is "pipx"
        3. which agentalloy resolves under a .venv + pyproject.toml present
           → mode is "editable"
        4. otherwise "unknown"
    """
    binary_path = shutil.which("agentalloy")

    # 1. Check uv tool list
    uv = shutil.which("uv")
    if uv:
        try:
            result = subprocess.run(
                [uv, "tool", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    # uv tool list outputs lines like "agentalloy <version> <path>"
                    first_token = line.strip().split()[0] if line.strip() else ""
                    if first_token == "agentalloy":
                        return {
                            "mode": "uv_tool",
                            "binary_path": binary_path,
                            "venv_path": None,
                            "details": "Installed via uv tool install",
                        }
        except (subprocess.TimeoutExpired, OSError):
            pass  # fall through to next check

    # 2. Check pipx list
    pipx = shutil.which("pipx")
    if pipx:
        try:
            result = subprocess.run(
                [pipx, "list", "--short"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    # pipx list --short outputs "agentalloy <version>"
                    first_token = line.strip().split()[0] if line.strip() else ""
                    if first_token == "agentalloy":
                        return {
                            "mode": "pipx",
                            "binary_path": binary_path,
                            "venv_path": None,
                            "details": "Installed via pipx (legacy)",
                        }
        except (subprocess.TimeoutExpired, OSError):
            pass  # fall through to next check

    # 3. Check editable: binary under .venv + pyproject.toml with name=agentalloy
    if binary_path:
        binary_parent = Path(binary_path).resolve()
        # Walk up looking for a .venv directory
        for ancestor in binary_parent.parents:
            if ancestor.name == ".venv":
                # Walk up from .venv to find the repo root
                repo_root = ancestor.parent
                pyproject = repo_root / "pyproject.toml"
                if pyproject.exists():
                    try:
                        content = pyproject.read_text()
                        if 'name = "agentalloy"' in content or "name = 'agentalloy'" in content:
                            return {
                                "mode": "editable",
                                "binary_path": binary_path,
                                "venv_path": str(ancestor),
                                "details": f"Editable install at {ancestor}",
                            }
                    except OSError:
                        pass
                break

    # 4. Unknown
    return {
        "mode": "unknown",
        "binary_path": binary_path,
        "venv_path": None,
        "details": "Install mode could not be determined",
    }


def _remove_cli_install(mode_info: dict[str, Any]) -> dict[str, Any]:
    """Remove the CLI installation based on detected mode.

    Dispatches on mode_info["mode"]:
        - "uv_tool": runs uv tool uninstall agentalloy
        - "pipx": runs pipx uninstall agentalloy
        - "editable": prints manual instructions, does NOT delete the venv
        - "unknown": returns skipped

    Returns an action dict with a "mode" field and the action taken.
    """
    mode = mode_info.get("mode", "unknown")

    if mode == "uv_tool":
        return _remove_uv_tool_internal()
    elif mode == "pipx":
        return _remove_pipx_install()
    elif mode == "editable":
        return _handle_editable_install(mode_info)
    else:
        return _handle_unknown_install(mode_info)


def _remove_uv_tool_internal() -> dict[str, Any]:
    """Internal: remove the uv tool installation. Returns an action dict."""
    uv = shutil.which("uv")
    if not uv:
        return {"action": "uv_tool_skipped", "mode": "uv_tool", "reason": "uv not found in PATH"}
    try:
        result = subprocess.run(
            [uv, "tool", "uninstall", "agentalloy"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"action": "uv_tool_uninstalled", "mode": "uv_tool"}
        return {
            "action": "uv_tool_skipped",
            "mode": "uv_tool",
            "reason": result.stderr.strip() or "not installed",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"action": "uv_tool_skipped", "mode": "uv_tool", "reason": str(exc)}


def _remove_pipx_install() -> dict[str, Any]:
    """Remove pipx installation. Returns an action dict."""
    pipx = shutil.which("pipx")
    if not pipx:
        return {
            "action": "pipx_skipped",
            "mode": "pipx",
            "reason": "pipx not found in PATH",
        }
    try:
        result = subprocess.run(
            [pipx, "uninstall", "agentalloy"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {"action": "pipx_uninstalled", "mode": "pipx"}
        return {
            "action": "pipx_skipped",
            "mode": "pipx",
            "reason": result.stderr.strip() or "not installed",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"action": "pipx_skipped", "mode": "pipx", "reason": str(exc)}


def _handle_editable_install(mode_info: dict[str, Any]) -> dict[str, Any]:
    """Editable install: cannot auto-remove; print instructions instead."""
    venv_path = mode_info.get("venv_path")
    return {
        "action": "editable_install_left_in_place",
        "mode": "editable",
        "venv_path": venv_path,
        "details": (
            f"Editable install at {venv_path}. "
            "The agentalloy command will remain available until you remove "
            "the venv manually (rm -rf <venv>) or run 'uv tool uninstall "
            "agentalloy' if you also have a tool install."
        ),
    }


def _handle_unknown_install(mode_info: dict[str, Any]) -> dict[str, Any]:
    """Unknown install: return skipped with helpful message."""
    binary_path = mode_info.get("binary_path")
    reason = f"agentalloy binary found at {binary_path} but install mode unknown"
    if not binary_path:
        reason = "agentalloy binary not found in PATH"
    return {
        "action": "cli_install_skipped",
        "mode": "unknown",
        "reason": reason,
    }


def _remove_uv_tool() -> dict[str, Any]:
    """Legacy wrapper around _remove_uv_tool_internal for backward compatibility."""
    return _remove_uv_tool_internal()


def uninstall(
    remove_data: bool = False,
    force: bool = False,
    root: Path | None = None,
    *,
    remove_user_state: bool = True,
    remove_env: bool = True,
    all_repos: bool = True,
    remove_models: bool = False,
    remove_wiring: bool = True,
    stop_services: bool | None = None,
) -> dict[str, Any]:
    """Remove harness wiring, .env, and state. Returns contract-shaped result.

    ``remove_user_state`` and ``remove_env`` are False for the per-repo
    ``unwire`` verb, which must touch only sentinels in the cwd repo and
    leave the user-scope `${XDG_CONFIG_HOME}/agentalloy/` directory alone.
    Default True preserves the original full-teardown behavior of
    `uninstall` so existing callers don't change semantics.

    ``all_repos`` controls whether the ``harness_files_written`` walk
    cleans entries outside cwd. Default True for ``uninstall`` (full
    teardown — once the CLI is gone the user can no longer ``cd && unwire``
    into other repos). The ``unwire`` callsite passes ``all_repos=False``
    to preserve cwd-only semantics.
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    st = install_state.load_state(root)

    # Back-compat: when stop_services isn't explicitly provided, derive
    # from remove_user_state so existing callers (full teardown sets
    # remove_user_state=True; unwire sets it False) keep their behavior.
    if stop_services is None:
        stop_services = remove_user_state

    files_modified: list[dict[str, Any]] = []
    files_removed: list[dict[str, Any]] = []
    warnings: list[str] = []
    model_actions: list[dict[str, Any]] = []

    # 0. Stop container stack (if deployment == "container" and stop_services)
    container_actions: list[dict[str, Any]] = []
    if stop_services:
        container_actions = _stop_container_stack(st, warnings)
        # 0b. Remove named volumes when the caller asked for --remove-data.
        # Stopping/removing the container leaves its named volumes
        # (agentalloy-data) in place; they silently carry old corpus state
        # into the next reinstall. Volume rm must run after the container is
        # stopped so it no longer holds them open.
        if remove_data:
            container_actions.extend(_remove_compose_volumes(st, warnings))
        # 0b. Remove the local container image — must run after container
        # is stopped/removed. Image removal is independent of --remove-data;
        # even a "keep data" uninstall should clean up the deployment image.
        container_actions.extend(_remove_container_image(st, warnings))
    elif remove_data and st.get("deployment") == "container":
        # remove_data=True with stop_services=False is a programmatic
        # mis-use: `volume rm` would fail because containers still hold
        # the volumes open. Surface this loudly instead of silently
        # leaving the volumes behind — callers asking for --remove-data
        # almost certainly expect a full wipe.
        warnings.append(
            "remove_data=True requested without stop_services — named "
            "volumes (agentalloy-data) cannot "
            "be removed while the container still mounts them. Re-run with "
            "stop_services=True, or stop the container and remove the "
            "volumes manually with `<podman|docker> volume rm`."
        )

    # 1. Remove harness wiring. State is user-scoped and may carry entries
    # from multiple repos, but the containment check MUST use a trusted
    # bound — both `path` and `repo_root` come from the state file and a
    # tampered entry like `{"path": "/etc/shadow", "repo_root": "/etc"}`
    # would otherwise pass a per-entry check trivially. The trusted bound
    # is the cwd-derived `root` (or the known per-tool user config dirs).
    # An entry whose recorded `repo_root` doesn't match cwd is skipped at
    # this invocation; the user can `cd` into that repo to clean it up.
    # When remove_wiring=False this loop (and the MCP/aider cleanup
    # blocks below) is skipped entirely — sentinels and MCP entries
    # stay in place. Used by Custom uninstall presets where the user
    # answered "no" to "remove harness wiring".
    home = Path.home()
    allowed_user_prefixes = (
        home / ".claude",
        home / ".cursor",
        home / ".continue",
        home / ".agentalloy",
        home / ".openclaw",
    )
    # Set of harness target basenames / suffix-paths we ever write. Any
    # `path` in state that doesn't end in one of these is rejected even
    # if the containment check would otherwise allow it.
    allowed_path_suffixes = (
        "CLAUDE.md",
        "GEMINI.md",
        ".clinerules",
        ".cursorrules",
        ".cursor/rules/agentalloy.mdc",
        ".continuerc.json",
        ".cursor/mcp.json",
        ".aider.conf.yml",
        ".agentalloy-aider-instructions.md",
        ".opencode/system-prompt.md",
        "mcp_servers.json",  # ~/.claude/mcp_servers.json
        "claude-code-env.sh",  # ~/.agentalloy/claude-code-env.sh
        ".cline/settings.json",  # Cline proxy config
        ".claude/settings.json",  # Claude Code hook wiring (merged hooks)
        "agentalloy-hook-claude-code.sh",  # ~/.agentalloy/hooks/ hook script
        ".openclaw/plugins.json",  # openclaw plugin config
    )
    root_resolved = root.resolve()
    # Iterate over harness entries only when wiring removal is enabled.
    # Typed binding preserves `entry: dict[str, Any]` for pyright across
    # the loop body — using an inline conditional iterable degrades the
    # inferred type to Unknown.
    harness_entries: list[dict[str, Any]] = (
        st.get("harness_files_written", []) if remove_wiring else []
    )
    # Track paths already handled by the harness loop so downstream
    # special-case sections (Continue, Cursor MCP, Claude MCP, etc.)
    # can skip them to avoid double-processing.
    handled_paths: set[str] = set()
    for entry in harness_entries:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            warnings.append(f"Skipping harness entry with non-string path: {entry!r}")
            continue
        path = Path(raw_path)
        # Reject paths that don't end in a known harness target — defends
        # against tampered entries like `/etc/shadow`.
        if not any(str(path).endswith(suffix) for suffix in allowed_path_suffixes):
            warnings.append(
                f"Skipping harness entry with non-harness path (state may be tampered): {raw_path}"
            )
            continue
        # Containment: the path must live under one of three trusted roots:
        # (a) cwd-derived `root` (always allowed),
        # (b) a known user-scope harness prefix (~/.claude, etc.),
        # (c) when `all_repos=True`, the entry's own recorded `repo_root` —
        #     but only after we revalidate the path is inside that root and
        #     ends in the suffix allowlist (already checked above). The
        #     suffix allowlist is the trust anchor that prevents a tampered
        #     `{path: "/etc/shadow", repo_root: "/etc"}` from passing.
        path_inside_cwd_repo = install_state.is_inside_root(path, root)
        path_inside_user = any(
            install_state.is_inside_root(path, p) for p in allowed_user_prefixes if p.exists()
        )
        path_inside_entry_repo = False
        entry_repo_root_str = entry.get("repo_root")
        entry_repo_root: Path | None = None
        if all_repos and isinstance(entry_repo_root_str, str) and entry_repo_root_str:
            entry_repo_root = Path(entry_repo_root_str)
            path_inside_entry_repo = install_state.is_inside_root(path, entry_repo_root)
        if not (path_inside_cwd_repo or path_inside_user or path_inside_entry_repo):
            # Entry belongs to a different repo and we're not authorized to
            # cross repos (all_repos=False) — or the recorded repo_root
            # doesn't actually contain the path. Track the skip so the user
            # can see why state still has entries.
            warnings.append(
                f"Skipping harness entry from a different repo "
                f"(repo_root={entry_repo_root_str!r}): {raw_path}"
            )
            continue
        # Defense in depth: even when path passes containment, refuse to
        # follow into the trusted root via a path that escapes via `..`.
        try:
            resolved = path.resolve()
            if path_inside_cwd_repo and not str(resolved).startswith(str(root_resolved)):
                warnings.append(
                    f"Skipping harness entry that escapes repo root via symlink: {raw_path}"
                )
                continue
            if path_inside_entry_repo and entry_repo_root is not None:
                entry_root_resolved = entry_repo_root.resolve()
                if not str(resolved).startswith(str(entry_root_resolved)):
                    warnings.append(
                        f"Skipping harness entry that escapes its repo root via symlink: {raw_path}"
                    )
                    continue
        except OSError:
            warnings.append(f"Cannot resolve harness path: {raw_path}")
            continue
        if not path.exists():
            warnings.append(f"Harness file not found (already removed?): {path}")
            continue

        content = path.read_text()
        sentinel_begin = entry.get("sentinel_begin", SENTINEL_BEGIN)
        sentinel_end = entry.get("sentinel_end", SENTINEL_END)

        # NEW: Restore original content if we backed it up
        original = entry.get("original_content")
        if original is not None:
            # File existed before install — restore it to pre-install state
            path.write_text(original)
            files_modified.append({"path": str(path), "action": "restored_original"})
            handled_paths.add(raw_path)
            continue

        if sentinel_begin and sentinel_begin in content and sentinel_end in content:
            # Check if content was modified inside sentinels
            current_inner = _extract_sentinel_content(content, sentinel_begin, sentinel_end)
            stored_sha = entry.get("content_sha256", "")

            # For dedicated files (no sentinels in content), just delete
            if entry.get("action") == "wrote_new_file":
                path.unlink()
                files_removed.append({"path": str(path), "action": "deleted_dedicated_file"})
                handled_paths.add(raw_path)
                continue

            if current_inner is not None and stored_sha:
                # Verify the inner content hasn't drifted since wire-harness wrote it.
                # If it has, the user (or another tool) edited inside the sentinels;
                # without --force, refuse to clobber their changes.
                current_sha = _sha256(current_inner)
                expected = (
                    stored_sha[len("sha256:") :] if stored_sha.startswith("sha256:") else stored_sha
                )
                if current_sha != expected and not force:
                    warnings.append(
                        f"Tampered sentinel block in {path} (sha256 mismatch). "
                        f"Skipped to preserve your edits — use --force to remove anyway."
                    )
                    continue

            cleaned = _remove_sentinel_block(content, sentinel_begin, sentinel_end)
            if cleaned.strip():
                path.write_text(cleaned)
                files_modified.append({"path": str(path), "action": "removed_sentinel_block"})
                handled_paths.add(raw_path)
            else:
                # File is now empty after removing our block — delete it
                path.unlink()
                files_removed.append({"path": str(path), "action": "deleted_empty_file"})
                handled_paths.add(raw_path)
        elif entry.get("action") == "wrote_new_file":
            # Dedicated file (no sentinels) — delete it
            path.unlink()
            files_removed.append({"path": str(path), "action": "deleted_dedicated_file"})
            handled_paths.add(raw_path)
        else:
            if force:
                path.unlink()
                files_removed.append({"path": str(path), "action": "force_deleted"})
                handled_paths.add(raw_path)
            else:
                warnings.append(
                    f"Sentinel block not found in {path} — skipped. Use --force to delete anyway."
                )

    # 2a. Handle proxy config cleanup (new sentinel-bounded blocks)
    # Each proxy-wired harness has its own uninstall function in uninstall_proxy
    proxy_removed: list[Path] = []

    # Repo-scope proxies: safe to run on any per-repo unwire
    if remove_wiring:
        # Skip proxy unwiring for paths already handled by harness loop's original_content restore
        if str(root / ".aider.conf.yml") not in handled_paths:
            proxy_removed.extend(uninstall_proxy._unwire_proxy_aider(root))
        opencode_env = str(root / ".opencode" / ".agentalloy-env")
        opencode_prompt = str(root / ".opencode" / "system-prompt.md")
        if opencode_env not in handled_paths and opencode_prompt not in handled_paths:
            proxy_removed.extend(uninstall_proxy._unwire_proxy_opencode(root))
        if str(root / ".cline" / "settings.json") not in handled_paths:
            proxy_removed.extend(uninstall_proxy._unwire_proxy_cline(root))

    # User-scope proxies: only run during a global (all_repos) uninstall to avoid
    # removing global home-dir config when unwiring an unrelated repo.
    if remove_wiring and all_repos:
        hermes_config = Path.home() / ".hermes" / "config.yaml"
        if str(hermes_config) not in handled_paths:
            proxy_removed.extend(uninstall_proxy._unwire_proxy_hermes_agent("user", root))
        claude_env = Path.home() / ".agentalloy" / "claude-code-env.sh"
        if str(claude_env) not in handled_paths:
            proxy_removed.extend(uninstall_proxy._unwire_proxy_claude_code(root))
        # Also remove hook entries from settings.json (legacy path)
        if all_repos:
            settings_removed = uninstall_proxy._unwire_claude_code_hooks_settings_json()
            for sr in settings_removed:
                files_removed.append(sr)
                print(
                    f"  settings.json cleanup: {sr['action']} ({sr.get('key', sr.get('action'))})",
                    file=sys.stderr,
                )

    if proxy_removed:
        for p in proxy_removed:
            files_removed.append({"path": str(p), "action": "unwired_proxy"})
        print("  Proxy config removed:", file=sys.stderr)
        for path in proxy_removed:
            print(f"    - {path}", file=sys.stderr)
        print("", file=sys.stderr)

    # 2. Handle Continue.dev marker cleanup (markdown injection variant)
    continuerc = root / ".continuerc.json"
    if remove_wiring and continuerc.exists() and str(continuerc) not in handled_paths:
        try:
            config = json.loads(continuerc.read_text())
            modified = False

            # 2a. Remove the agentalloy MCP server entry, if present
            servers = config.get("mcpServers")
            if isinstance(servers, dict) and "agentalloy" in servers:
                del servers["agentalloy"]
                if not servers:
                    del config["mcpServers"]
                modified = True

            if "_agentalloy_install_marker" in config:
                # Remove our custom command
                commands = config.get("customCommands", [])
                config["customCommands"] = [c for c in commands if c.get("name") != "skill"]
                if not config["customCommands"]:
                    del config["customCommands"]

                # Remove system message sentinel block
                sys_msg = config.get("systemMessage", "")
                if "<!-- agentalloy:begin -->" in sys_msg:
                    b = sys_msg.index("<!-- agentalloy:begin -->")
                    e = sys_msg.index("<!-- agentalloy:end -->") + len("<!-- agentalloy:end -->")
                    sys_msg = sys_msg[:b].rstrip() + sys_msg[e:].lstrip()
                    if sys_msg.strip():
                        config["systemMessage"] = sys_msg.strip()
                    else:
                        del config["systemMessage"]

                del config["_agentalloy_install_marker"]
                modified = True

            if modified:
                if any(k for k in config if not k.startswith("_")):
                    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
                        continuerc, json.dumps(config, indent=2) + "\n"
                    )
                    files_modified.append({"path": str(continuerc), "action": "cleaned_continuerc"})
                else:
                    continuerc.unlink()
                    files_removed.append(
                        {"path": str(continuerc), "action": "deleted_empty_continuerc"}
                    )
        except json.JSONDecodeError:
            warnings.append(f"Could not parse {continuerc} as JSON — skipped")

    # 2b. Handle Cursor MCP config cleanup (.cursor/mcp.json)
    cursor_mcp = root / ".cursor" / "mcp.json"
    if remove_wiring and cursor_mcp.exists() and str(cursor_mcp) not in handled_paths:
        try:
            cfg = json.loads(cursor_mcp.read_text())
            servers = cfg.get("mcpServers")
            if isinstance(servers, dict) and "agentalloy" in servers:
                del servers["agentalloy"]
                if not servers:
                    cfg.pop("mcpServers", None)
                if cfg:
                    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
                        cursor_mcp, json.dumps(cfg, indent=2) + "\n"
                    )
                    files_modified.append({"path": str(cursor_mcp), "action": "removed_mcp_entry"})
                else:
                    cursor_mcp.unlink()
                    files_removed.append({"path": str(cursor_mcp), "action": "deleted_empty_file"})
        except json.JSONDecodeError:
            warnings.append(f"Could not parse {cursor_mcp} as JSON — skipped")

    # 2c. Handle user-scoped Claude Code MCP config (~/.claude/mcp_servers.json)
    claude_mcp = Path.home() / ".claude" / "mcp_servers.json"
    if remove_wiring and claude_mcp.exists() and str(claude_mcp) not in handled_paths:
        try:
            cfg = json.loads(claude_mcp.read_text())
            servers = cfg.get("mcpServers")
            if isinstance(servers, dict) and "agentalloy" in servers:
                del servers["agentalloy"]
                if not servers:
                    cfg.pop("mcpServers", None)
                if cfg:
                    install_state._atomic_write(  # pyright: ignore[reportPrivateUsage]
                        claude_mcp, json.dumps(cfg, indent=2) + "\n"
                    )
                    files_modified.append({"path": str(claude_mcp), "action": "removed_mcp_entry"})
                else:
                    claude_mcp.unlink()
                    files_removed.append({"path": str(claude_mcp), "action": "deleted_empty_file"})
        except json.JSONDecodeError:
            warnings.append(f"Could not parse {claude_mcp} as JSON — skipped")

    # 3. Handle aider config cleanup
    aider_conf = root / ".aider.conf.yml"
    if remove_wiring and aider_conf.exists() and str(aider_conf) not in handled_paths:
        content = aider_conf.read_text()
        aider_begin = "# <!-- BEGIN agentalloy install -->"
        aider_end = "# <!-- END agentalloy install -->"
        if aider_begin in content:
            cleaned = _remove_sentinel_block(content, aider_begin, aider_end)
            if cleaned.strip():
                aider_conf.write_text(cleaned)
                files_modified.append({"path": str(aider_conf), "action": "removed_sentinel_block"})
            else:
                aider_conf.unlink()
                files_removed.append({"path": str(aider_conf), "action": "deleted_empty_file"})

    # 4. Remove user-scope .env (skipped by `unwire`)
    if remove_env:
        env_path = install_state.env_path()
        if env_path.exists():
            # Restore original .env content if we backed it up
            original_env = st.get("env_original_content")
            if original_env is not None:
                env_path.write_text(original_env)
                files_modified.append({"path": str(env_path), "action": "restored_original"})
            else:
                env_path.unlink()
                files_removed.append({"path": str(env_path), "action": "deleted"})

    # 5. Handle user-data dir contents. Three sub-cases:
    #    - corpus_dir: kept by default (user data); removed only with --remove-data.
    #    - outputs_dir: derivable artifacts (per-step JSON dumps); always
    #      removed when remove_user_state is True so the dir doesn't outlive
    #      the install state that produced it.
    #    - server.log: same — derivable, removed unconditionally on full
    #      teardown.
    # On --remove-data we also wipe the user_data_dir() itself so an empty
    # ${XDG_DATA_HOME}/agentalloy doesn't linger.
    data_kept: list[str] = []
    if remove_user_state:
        from agentalloy.install import server_proc

        outputs = install_state.outputs_dir()
        if outputs.exists():
            shutil.rmtree(outputs)
            files_removed.append({"path": str(outputs), "action": "deleted_outputs_dir"})

        server_log = server_proc.server_log_path()
        if server_log.exists():
            with contextlib.suppress(OSError):
                server_log.unlink()
                files_removed.append({"path": str(server_log), "action": "deleted_server_log"})

    corpus = install_state.corpus_dir()
    if remove_data:
        if corpus.exists():
            shutil.rmtree(corpus)
            files_removed.append({"path": str(corpus), "action": "deleted_data_directory"})
        # Wipe the (now-empty-of-agentalloy-content) user_data_dir too. Use
        # rmtree so any unexpected nesting is handled, but only when the
        # caller asked for --remove-data — the default keeps the dir intact
        # for the corpus.
        udd = install_state.user_data_dir()
        if udd.exists():
            shutil.rmtree(udd)
            files_removed.append({"path": str(udd), "action": "deleted_user_data_dir"})
        # 5a. Remove the AgentAlloy download cache (~/.cache/agentalloy).
        # This holds cloned repos and downloaded assets that are no longer
        # needed once AgentAlloy is uninstalled.
        files_removed.extend(_remove_agentalloy_cache(warnings))
    elif corpus.exists():
        data_kept.append(str(corpus))

    # 5b. Stop a manual-mode agentalloy server still listening on the port.
    # Native systemd/launchd modes are handled in step 6; this catches the
    # case where the user ran `agentalloy server-start` directly.
    if stop_services:
        from agentalloy.install import server_proc

        port = int(st.get("port", 47950) or 47950)
        try:
            pid = server_proc.find_listening_pid(port)
        except Exception as exc:  # noqa: BLE001 — defensive; never block uninstall
            warnings.append(f"Could not check port {port}: {exc}")
            pid = None
        if pid:
            # Check if the process holding the port is actually agentalloy.
            # If not, warn the user instead of trying to kill it.
            is_agentalloy = False
            cmdline_str = ""
            try:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_bytes()
                    # Replace null bytes with spaces for readability
                    cmdline_str = cmdline.decode("utf-8", errors="replace").replace("\x00", " ")
                    is_agentalloy = (
                        "agentalloy.app:app" in cmdline_str
                        or "agentalloy/__main__.py" in cmdline_str
                    )
                else:
                    # macOS fallback: use ps
                    ps_result = subprocess.run(
                        ["ps", "-o", "command=", "-p", str(pid)],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if ps_result.returncode == 0:
                        cmdline_str = ps_result.stdout.strip()
                        is_agentalloy = (
                            "agentalloy.app:app" in cmdline_str
                            or "agentalloy/__main__.py" in cmdline_str
                        )
            except (OSError, subprocess.TimeoutExpired):
                # Can't determine owner — treat as a foreign process to be safe.
                # Defaulting to True would risk killing an unrelated process.
                is_agentalloy = False

            if not is_agentalloy:
                # Port is held by a foreign process — warn, don't kill
                short_cmd_parts = cmdline_str.split()[:3] if cmdline_str else ["<unknown>"]
                cmd_display = " ".join(short_cmd_parts)
                warnings.append(
                    f"Port {port} is held by pid {pid} running '{cmd_display}' — "
                    f"this is not an agentalloy server. Stop it manually "
                    f"(e.g. 'kill {pid}') or reconfigure AgentAlloy to use a "
                    f"different port."
                )
            else:
                try:
                    signal_used = server_proc.stop(pid)
                    files_removed.append(
                        {
                            "path": f"pid://{pid}",
                            "action": f"stopped_manual_server ({signal_used})",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Could not stop server pid {pid} on port {port}: {exc}")

    # 6. Stop and remove native service unit / plist (skipped by `unwire`)
    service_actions: list[dict[str, Any]] = []
    if stop_services:
        service_actions = _stop_native_service(st)
        files_removed.extend(service_actions)

    # 6b. Remove pulled models from runner caches. Independent of
    # stop_services — you may want models gone but services left running
    # for other tools.
    if remove_models:
        model_actions.extend(_remove_pulled_models(st))

    # 7. Remove user-scope state directory (skipped by `unwire`)
    if remove_user_state:
        state_d = install_state.state_dir()
        if state_d.exists():
            shutil.rmtree(state_d)
            files_removed.append({"path": str(state_d), "action": "deleted_state_directory"})

    # 8. Remove CLI installation (skipped by `unwire`). Detect install mode
    # and dispatch to the appropriate removal strategy.
    cli_install_result: dict[str, Any] = {}
    mode_info: dict[str, Any] | None = None
    if remove_user_state:
        mode_info = _detect_install_mode()
        cli_install_result = _remove_cli_install(mode_info)
        # If editable mode, surface the manual-step message as a warning
        if cli_install_result.get("action") == "editable_install_left_in_place":
            details = cli_install_result.get("details", "")
            if details:
                warnings.append(details)

    # Build result: use new key name, keep deprecated alias for one release.
    # 9. Return result dict
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files_modified": files_modified,
        "files_removed": files_removed,
        "data_kept": data_kept,
        "warnings": warnings,
        "cli_install": cli_install_result,
        "models_removed": model_actions,
        "daemons_stopped": [],
        "container_actions": container_actions,
        "install_mode": mode_info,
    }
    # Deprecated alias — remove one release later.
    result["uv_tool"] = cli_install_result
    return result


def _print_uninstall_summary(result: dict[str, Any]) -> None:
    """Print a human-readable summary of the uninstall result to stderr.

    Replaces the raw JSON output with a clean, user-friendly summary.
    """
    print("", file=sys.stderr)
    print("  Uninstall complete.", file=sys.stderr)
    print("", file=sys.stderr)

    # Files modified
    modified = result.get("files_modified", [])
    if modified:
        print("  Files modified:", file=sys.stderr)
        for entry in modified:
            path = entry.get("path", "?")
            action = entry.get("action", "?")
            print(f"    - {path} ({action})", file=sys.stderr)
        print("", file=sys.stderr)

    # Files/dirs removed
    removed = result.get("files_removed", [])
    if removed:
        print("  Removed:", file=sys.stderr)
        for entry in removed:
            path = entry.get("path", "?")
            print(f"    - {path}", file=sys.stderr)
        print("", file=sys.stderr)

    # Models removed
    models = result.get("models_removed", [])
    removed_model_actions = {"gguf_removed"}
    removed_models = [entry for entry in models if entry.get("action") in removed_model_actions]
    other_model_actions = [
        entry for entry in models if entry.get("action") not in removed_model_actions
    ]
    if removed_models:
        print("  Models removed:", file=sys.stderr)
        for entry in removed_models:
            runner = entry.get("runner", "?")
            model = entry.get("model", "?")
            print(f"    - {runner}: {model}", file=sys.stderr)
        print("", file=sys.stderr)
    if other_model_actions:
        print("  Model cleanup:", file=sys.stderr)
        for entry in other_model_actions:
            action = entry.get("action", "?")
            runner = entry.get("runner")
            model = entry.get("model")
            target = f"{runner}: {model}" if runner and model else entry.get("entry")
            detail = f"{action}"
            if target:
                detail += f" ({target})"
            hint = entry.get("hint") or entry.get("error")
            if hint:
                detail += f" - {hint}"
            print(f"    - {detail}", file=sys.stderr)
        print("", file=sys.stderr)

    # Data preserved
    kept = result.get("data_kept", [])
    if kept:
        print("  Data preserved:", file=sys.stderr)
        for entry in kept:
            if isinstance(entry, dict):
                entry_dict = cast(dict[str, Any], entry)
                raw_path = entry_dict.get("path")
                path = raw_path if isinstance(raw_path, str) else "?"
            else:
                path = str(entry)
            print(f"    - {path}", file=sys.stderr)
        print("", file=sys.stderr)

    # CLI install
    cli = result.get("cli_install", {}) or result.get("uv_tool", {})
    action = cli.get("action", "")
    if action == "uv_tool_uninstalled":
        print("  CLI install: uninstalled (uv tool)", file=sys.stderr)
        print("", file=sys.stderr)
    elif action == "pipx_uninstalled":
        print("  CLI install: uninstalled (pipx)", file=sys.stderr)
        print("", file=sys.stderr)
    elif action == "editable_install_left_in_place":
        print("  CLI install: editable install left in place (see warnings)", file=sys.stderr)
        print("", file=sys.stderr)
    elif action in ("uv_tool_skipped", "pipx_skipped", "cli_install_skipped"):
        reason = cli.get("reason", cli.get("details", ""))
        print(f"  CLI install: skipped ({reason})", file=sys.stderr)
        print("", file=sys.stderr)

    # Container actions
    container_actions = result.get("container_actions", [])
    if container_actions:
        print("  Container actions:", file=sys.stderr)
        for entry in container_actions:
            path = entry.get("path", "?")
            action = entry.get("action", "?")
            error = entry.get("error")
            detail = f"    - {path} ({action})"
            if error:
                detail += f" - {error}"
            print(detail, file=sys.stderr)
        print("", file=sys.stderr)

    # Warnings
    warnings = result.get("warnings", [])
    if warnings:
        print("  Warnings:", file=sys.stderr)
        for w in warnings:
            print(f"    ! {w}", file=sys.stderr)
        print("", file=sys.stderr)


def _print_uninstall_json(result: dict[str, Any]) -> None:
    """Print the raw JSON result (for --json flag)."""
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "uninstall",
        help="Remove harness wiring, .env, and state files.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON result (default: human-readable summary).",
    )
    p.add_argument(
        "--remove-data",
        action="store_true",
        default=False,
        help="Also remove data/ directory (default: preserve).",
    )
    p.add_argument(
        "--keep-data",
        action="store_true",
        default=False,
        help="Explicit opt-in for the default behavior (no-op).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force removal even when sentinel blocks are missing or modified.",
    )
    p.add_argument(
        "--all-repos",
        dest="all_repos",
        action="store_true",
        default=True,
        help=(
            "Walk every repo recorded in install-state.json and clean its "
            "sentinel blocks (default). The CLI is removed at the end of "
            "uninstall, so cross-repo cleanup must happen now or never."
        ),
    )
    p.add_argument(
        "--no-all-repos",
        dest="all_repos",
        action="store_false",
        help="Limit cleanup to the current repo (legacy behavior).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help=(
            "Skip the interactive preset/custom prompt and apply the "
            "flag-based behavior directly (use with --remove-data etc. "
            "for scripted / CI invocations)."
        ),
    )
    p.add_argument(
        "--preset",
        choices=("keep-data", "full", "custom"),
        default=None,
        help=(
            "Skip the menu and apply a preset directly. "
            "'keep-data' removes wiring + .env only. "
            "'full' removes everything (services, models, datastore, wiring, state). "
            "'custom' enters the per-item drill-down."
        ),
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    """Translate CLI args + (optionally) interactive prompts into uninstall kwargs.

    Priority (most specific wins):
      1. ``--preset X`` — apply that preset's mapping (skips menu and ignores
         ``--yes``'s legacy mapping; explicit preset is the strongest intent).
      2. ``--yes`` (no preset) — skip prompts, use legacy flag mapping for
         back-compat with scripted/CI invocations.
      3. Non-TTY (no stdin, no preset, no --yes) — same as ``--yes`` to
         avoid wedging on missing input.
      4. Interactive TTY — show preset menu, drill into custom if chosen.
    """
    is_tty = sys.stdin.isatty()
    use_prompt = not (args.yes or args.preset or not is_tty)

    # Defaults: derive from existing flags so --yes scripts behave exactly
    # like they did before this change.
    kwargs: dict[str, Any] = {
        "remove_data": args.remove_data,
        "force": args.force,
        "all_repos": args.all_repos,
        # Sensible legacy defaults — full teardown.
        "remove_user_state": True,
        "remove_env": True,
        "remove_wiring": True,
        "remove_models": False,
        "stop_services": True,
    }

    preset: str | None = args.preset
    if use_prompt:
        preset = _prompt_uninstall_preset()

    if preset == "keep-data":
        kwargs.update(
            {
                "remove_data": False,
                "remove_models": False,
                "stop_services": False,
                "remove_user_state": False,
                "remove_env": True,
                "remove_wiring": True,
            }
        )
    elif preset == "full":
        kwargs.update(
            {
                "remove_data": True,
                "remove_models": True,
                "stop_services": True,
                "remove_user_state": True,
                "remove_env": True,
                "remove_wiring": True,
            }
        )
    elif preset == "custom":
        answers = _prompt_uninstall_custom()
        # Wiring is independent. .env and state dir collapse into one
        # user-facing question — the user typically wants them together,
        # and the uv-tool removal piggybacks on the same answer because
        # it represents "remove agentalloy itself from the system".
        kwargs.update(
            {
                "remove_data": answers["remove_datastore"],
                "remove_models": answers["remove_models"],
                "stop_services": answers["stop_services"],
                "remove_user_state": answers["remove_env_state"],
                "remove_env": answers["remove_env_state"],
                "remove_wiring": answers["remove_wiring"],
            }
        )

    result = uninstall(**kwargs)
    if args.json:
        _print_uninstall_json(result)
    else:
        _print_uninstall_summary(result)
    return 0
