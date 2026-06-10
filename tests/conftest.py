"""Shared pytest fixtures."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.storage.vector_store import VectorStore, open_or_create

# Port used by the agentalloy server — must be freed between tests.
_DEFAULT_PORT = 47950


@pytest.fixture(autouse=True)
def _isolated_xdg_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point XDG state dirs at a per-test tmp dir for the whole suite.

    install_state and config resolve XDG_CONFIG_HOME / XDG_DATA_HOME
    per-call, so redirecting the env isolates every test from the
    developer's real ~/.config/agentalloy and ~/.local/share/agentalloy —
    and matches CI, which has no real install. (A previous fixture in
    tests/install/conftest.py rmtree'd the real dirs instead; running the
    test suite destroyed any local AgentAlloy install.)
    """
    config_dir = tmp_path / "xdg-config"
    data_dir = tmp_path / "xdg-data"
    config_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))


@pytest.fixture(scope="session", autouse=True)
def _isolated_ambient_tmpdir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Pin TMPDIR to the pytest tmp tree for the whole session.

    Ambient temp-file writers — ``tempfile`` defaults in production code
    (e.g. the entrypoint ``NamedTemporaryFile`` in container_runtime),
    podman's ``$TMPDIR/containers-user-$UID`` storage, leaked ``mkdtemp``
    dirs — otherwise fall back to the repo working directory when /tmp
    isn't writable (sandboxed runners), leaving residue in the checkout.
    """
    tmp = tmp_path_factory.mktemp("ambient-tmp")
    old = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = None  # drop cached resolution so the new TMPDIR takes effect
    yield
    if old is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = old
    tempfile.tempdir = None


# Processes that predate the pytest session are not ours to kill — a
# developer's real agentalloy service on the default port must survive a
# test run. Leaked test servers necessarily start after this timestamp.
_SESSION_START_EPOCH = time.time()


def _proc_start_epoch(pid: int) -> float | None:
    """Best-effort process start time (epoch seconds) via /proc. None if unknown."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 (starttime, clock ticks since boot); fields 1-2 are
        # "pid (comm)" where comm may contain spaces — split after ')'.
        ticks = float(stat.rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(ln.split()[1]) for ln in f if ln.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _kill_port(port: int) -> None:
    """Kill processes leaked onto *port* by this test session (best-effort).

    Only processes that started after the session began are killed; a
    pre-existing service (e.g. the developer's real agentalloy instance)
    is left alone.
    """
    try:
        out = subprocess.check_output(
            ["ss", "-tlnp"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if f":{port}" in line and "users:(" in line:
                # Extract PID from "users:(\"<name>\",pid=<N>,fd=<M>)"
                start = line.rfind("pid=")
                if start == -1:
                    continue
                end = line.index(",", start)
                pid = int(line[start + 4 : end])
                started = _proc_start_epoch(pid)
                if started is None or started < _SESSION_START_EPOCH:
                    continue
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


@pytest.fixture(autouse=True)
def _free_default_port():
    """Kill any process holding the default server port before and after each test.

    Prevents ``address already in use`` failures when tests leave background
    servers (rootlessport, uvicorn, etc.) running.
    """
    _kill_port(_DEFAULT_PORT)
    yield
    _kill_port(_DEFAULT_PORT)


@pytest.fixture(autouse=True)
def clear_container_sentinel():
    """Clear AGENTALLOY_DB_LOCK_HELD between every test.

    The sentinel is set in os.environ by stop_service_in_container() and
    cleared by restart_service_in_container(). If a test exercises the stop
    path without the restart path, the sentinel leaks into subsequent tests
    and causes stop_service_in_container() to short-circuit silently.
    """
    os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)
    yield
    os.environ.pop("AGENTALLOY_DB_LOCK_HELD", None)


@pytest.fixture
def app() -> FastAPI:
    # Skip the production lifespan (which opens LadybugDB + Ollama).
    # Per-test fixtures wire dependency_overrides explicitly.
    return create_app(use_default_lifespan=False)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def vector_store(tmp_path: Path) -> Iterator[VectorStore]:
    """Empty DuckDB vector store at a tmp path. Tests that exercise
    compose/retrieve construction use this for the new vector_store
    constructor parameter. Empty store means search_similar returns no
    hits — fine for tests that mock retrieval results anyway."""
    with open_or_create(tmp_path / "test.duck") as vs:
        yield vs
