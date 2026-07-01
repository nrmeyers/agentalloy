"""Container-leg implementations of the ``server-*`` lifecycle verbs.

The host (native) path lives in :mod:`agentalloy.install.server_proc` and drives
host processes/ports. On a *container* deployment those operations are wrong: a
host uvicorn would run against the empty host corpus instead of the container's
baked one. These helpers drive the container runtime instead.

Since PR #250 the container runs the image's **baked** entrypoint (no host
bind-mount of the entrypoint script), so ``{runtime} start/restart/stop`` work
and survive — we never recreate the container here.

Each ``_run_*`` returns ``(exit_code, payload)``; the payload mirrors the host
path's ``--json`` shape closely enough that callers can hand it straight to
``write_result``.
"""

from __future__ import annotations

import subprocess
from typing import Any

from agentalloy.install import server_proc
from agentalloy.install.server_proc import DeploymentTarget

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYSTEM = 2

# State strings ``_container_state`` returns (lowercase ``.State.Status``) that
# mean the container exists but is not running, so a ``start`` is appropriate.
_STOPPED_STATES = {"created", "exited", "stopped", "dead", "configured"}


def _functional_runtime_or_error(
    target: DeploymentTarget, verb: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Validate the resolved runtime; return ``(runtime, None)`` or ``(None, error)``.

    A missing runtime (neither podman nor docker resolvable) or one that fails
    ``<rt> info`` (daemon/machine down) is a user-correctable condition, so we
    return a structured error payload rather than raising.
    """
    runtime = target.runtime
    if not runtime:
        return None, {
            "action": "error",
            "deployment": "container",
            "container": target.container_name,
            "error": (
                "no container runtime found (podman/docker not on PATH); "
                "install one or re-run the agentalloy installer"
            ),
        }

    from agentalloy.install.subcommands.container_runtime import _runtime_is_functional

    if not _runtime_is_functional(runtime):
        return None, {
            "action": "error",
            "deployment": "container",
            "container": target.container_name,
            "runtime": runtime,
            "error": (
                f"container runtime '{runtime}' is not functional "
                f"('{runtime} info' failed) — is the daemon/machine running?"
            ),
        }
    return runtime, None


def _state(runtime: str, name: str) -> str:
    from agentalloy.install.subcommands.container_runtime import _container_state

    return _container_state(runtime, name)


def _runtime_call(
    runtime: str, args: list[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``{runtime} {args...}`` capturing output (mirrors container_runtime style)."""
    return subprocess.run(  # noqa: S603 — fixed argv, runtime resolved from state
        [runtime, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # cwd="/" — immune to the rootless-podman bind-mount-teardown race
        # that can transiently break getcwd() for callers whose cwd is under
        # the bind-mounted projects root (issue #303).
        cwd="/",
    )


def run_start(target: DeploymentTarget, *, wait: float) -> tuple[int, dict[str, Any]]:
    """``server-start`` on a container: start a stopped container, then wait /health.

    * missing container -> error (remediation: re-run the installer)
    * already running    -> no-op success
    * stopped/exited     -> ``{runtime} start {name}`` + readiness wait
    """
    runtime, err = _functional_runtime_or_error(target, "server-start")
    if err is not None:
        return EXIT_USER, err

    assert runtime is not None  # narrowed by _functional_runtime_or_error
    name = target.container_name
    state = _state(runtime, name)

    if state == "":
        return EXIT_USER, {
            "action": "error",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "error": (
                f"container '{name}' does not exist — re-run the agentalloy installer to create it"
            ),
        }

    if state == "running":
        return EXIT_OK, {
            "action": "already_running",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "port": target.port,
        }

    proc = _runtime_call(runtime, ["start", name], timeout=60.0)
    if proc.returncode != 0:
        return EXIT_USER, {
            "action": "error",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "error": f"'{runtime} start {name}' failed: {proc.stderr.strip()}",
        }

    ready = server_proc.health_ready(target.port, wait)
    payload = {
        "action": "started",
        "deployment": "container",
        "container": name,
        "runtime": runtime,
        "port": target.port,
        "ready": ready,
    }
    if not ready:
        payload["error"] = (
            f"container '{name}' started but /health was not ready on "
            f":{target.port} within {wait:.1f}s"
        )
        return EXIT_SYSTEM, payload
    return EXIT_OK, payload


def run_stop(target: DeploymentTarget, *, timeout: float) -> tuple[int, dict[str, Any]]:
    """``server-stop`` on a container: ``{runtime} stop --time {timeout} {name}``.

    Already-stopped (or missing) containers are an idempotent success no-op so
    repeated stops and the setup composer don't treat them as failures.
    """
    runtime, err = _functional_runtime_or_error(target, "server-stop")
    if err is not None:
        return EXIT_USER, err

    assert runtime is not None
    name = target.container_name
    state = _state(runtime, name)

    if state == "" or state in _STOPPED_STATES:
        return EXIT_OK, {
            "action": "already_stopped",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
        }

    proc = _runtime_call(
        runtime, ["stop", "--time", str(int(timeout)), name], timeout=timeout + 30.0
    )
    if proc.returncode != 0:
        return EXIT_USER, {
            "action": "error",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "error": f"'{runtime} stop {name}' failed: {proc.stderr.strip()}",
        }
    return EXIT_OK, {
        "action": "stopped",
        "deployment": "container",
        "container": name,
        "runtime": runtime,
    }


def run_restart(
    target: DeploymentTarget, *, stop_timeout: float, wait: float
) -> tuple[int, dict[str, Any]]:
    """``server-restart`` on a container: ``{runtime} restart`` then wait /health.

    A restart on a never-started container would fail, so we route a
    not-running container through ``start`` instead. Missing container -> error.
    """
    runtime, err = _functional_runtime_or_error(target, "server-restart")
    if err is not None:
        return EXIT_USER, err

    assert runtime is not None
    name = target.container_name
    state = _state(runtime, name)

    if state == "":
        return EXIT_USER, {
            "action": "error",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "error": (
                f"container '{name}' does not exist — re-run the agentalloy installer to create it"
            ),
        }

    if state != "running":
        # Nothing to restart; bring it up. Reuse start's readiness handling.
        return run_start(target, wait=wait)

    proc = _runtime_call(
        runtime, ["restart", "--time", str(int(stop_timeout)), name], timeout=stop_timeout + 90.0
    )
    if proc.returncode != 0:
        return EXIT_USER, {
            "action": "error",
            "deployment": "container",
            "container": name,
            "runtime": runtime,
            "error": f"'{runtime} restart {name}' failed: {proc.stderr.strip()}",
        }

    ready = server_proc.health_ready(target.port, wait)
    payload = {
        "action": "restarted",
        "deployment": "container",
        "container": name,
        "runtime": runtime,
        "port": target.port,
        "ready": ready,
    }
    if not ready:
        payload["error"] = (
            f"container '{name}' restarted but /health was not ready on "
            f":{target.port} within {wait:.1f}s"
        )
        return EXIT_SYSTEM, payload
    return EXIT_OK, payload


def status(target: DeploymentTarget) -> dict[str, Any]:
    """``server-status`` on a container: report ``_container_state`` + /health.

    Mirrors the host payload keys (``port``, ``reachable``) plus container-specific
    ``deployment``/``container``/``runtime``/``state`` so ``--json`` consumers can
    tell the two paths apart without losing the shared fields.
    """
    runtime = target.runtime
    name = target.container_name
    if not runtime:
        return {
            "deployment": "container",
            "container": name,
            "runtime": None,
            "state": "",
            "port": target.port,
            "reachable": False,
            "error": "no container runtime found (podman/docker not on PATH)",
        }

    state = _state(runtime, name)
    # Only probe health when the container claims to be running — a quick TCP/HTTP
    # check against a stopped container just wastes the timeout.
    reachable = state == "running" and server_proc.health_ready(target.port, 2.0)
    return {
        "deployment": "container",
        "container": name,
        "runtime": runtime,
        "state": state or "unknown",
        "port": target.port,
        "reachable": reachable,
    }
