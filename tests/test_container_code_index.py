"""Live-image test for the code-index module inside the deploy container.

Builds the deploy image (``Containerfile``) with real podman and boots it with
``CODE_INDEX_ENABLED=1``, proving the image ships the ``[code-index]`` extra
(tree-sitter + grammars) and serves ``/code/*`` when the operator flips the
env toggle. Module toggling is env-driven, so the baked entrypoint needs no
changes — the test overrides the entrypoint to launch uvicorn directly,
skipping the GGUF download / llama-server bootstrap (irrelevant here and far
too heavy for a test).

Marked ``container`` (via conftest ``_CONTAINER_FILES``): excluded from the
fast default suite, run serially with ``pytest -m container -n0``. The image
build is layer-cached by podman, so repeat runs are cheap; a cold build pulls
the llama.cpp/node base images once.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMAGE_TAG = "localhost/agentalloy:test-code-index"
_BUILD_TIMEOUT_S = 1800  # cold build pulls base images; cached rebuilds take seconds
_BOOT_TIMEOUT_S = 120


def _podman_env() -> dict[str, str]:
    """Environment for podman subprocesses, pinned to the HOST's real stores.

    conftest isolates XDG_DATA_HOME/XDG_CONFIG_HOME (function-scoped autouse)
    and TMPDIR (session-scoped) into pytest tmp dirs. Rootless podman keys its
    image graphroot off XDG_DATA_HOME, so a module-scoped build and a
    function-scoped run would otherwise hit DIFFERENT stores — the run then
    fails to resolve the just-built local tag and tries to pull it. Strip the
    overrides so every podman call in this file shares the host store (which
    also keeps the built image layer-cached for developers).
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "TMPDIR")
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_json(url: str) -> tuple[int, Any]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


@pytest.fixture(scope="module")
def podman() -> str:
    binary = shutil.which("podman")
    if binary is None:
        pytest.skip("podman not available on PATH")
    return binary


@pytest.fixture(scope="module")
def deploy_image(podman: str) -> str:
    """Build the deploy image from the repo's Containerfile (layer-cached)."""
    build = subprocess.run(
        [podman, "build", "-t", _IMAGE_TAG, "-f", str(_REPO_ROOT / "Containerfile"), "."],
        cwd=_REPO_ROOT,
        env=_podman_env(),
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_S,
    )
    if build.returncode != 0:
        pytest.fail(
            "podman build failed:\n"
            f"--- stdout (tail) ---\n{build.stdout[-4000:]}\n"
            f"--- stderr (tail) ---\n{build.stderr[-4000:]}"
        )
    return _IMAGE_TAG


def test_image_serves_code_index_when_enabled(podman: str, deploy_image: str) -> None:
    """CODE_INDEX_ENABLED=1 → /health reports the module enabled and /code/*
    responds — i.e. the image really ships the [code-index] extra."""
    name = f"agentalloy-test-code-index-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    run = subprocess.run(
        [
            podman,
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:47950",
            "-e",
            "CODE_INDEX_ENABLED=1",
            "-e",
            "AGENTALLOY_RELEASE_CHECK=0",
            # Skip the baked bootstrap entrypoint (GGUF download + llama-servers):
            # the module registration under test happens in create_app, and the
            # service tolerates an absent embed server (degraded compose).
            "--entrypoint",
            "/app/.venv/bin/python",
            deploy_image,
            "-m",
            "uvicorn",
            "agentalloy.app:app",
            "--host",
            "0.0.0.0",
            "--port",
            "47950",
        ],
        env=_podman_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, f"podman run failed: {run.stderr}"

    try:
        # Poll /health until the app is up (uvicorn + lifespan startup).
        deadline_attempts = _BOOT_TIMEOUT_S  # 1s-ish per attempt (5s urlopen timeout)
        health: Any = None
        last_err: Exception | None = None
        for _ in range(deadline_attempts):
            try:
                status_code, health = _get_json(f"http://127.0.0.1:{port}/health")
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                last_err = exc
                time.sleep(1)
                continue
            assert status_code == 200
            break
        else:
            logs = subprocess.run(
                [podman, "logs", name],
                env=_podman_env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            pytest.fail(
                f"service never became reachable ({last_err!r});\n"
                f"container logs (tail):\n{(logs.stdout + logs.stderr)[-4000:]}"
            )

        assert health["modules"]["code_index"] == "enabled", health

        status_code, repos = _get_json(f"http://127.0.0.1:{port}/code/repos")
        assert status_code == 200
        assert repos == []
    finally:
        # Guaranteed teardown — --rm plus an explicit rm -f so an interrupted
        # run can't leave an orphaned rootless container behind.
        subprocess.run(
            [podman, "rm", "-f", "-t", "5", name],
            env=_podman_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
