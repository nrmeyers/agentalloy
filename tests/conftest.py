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
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentalloy.app import create_app
from agentalloy.storage.vector_store import VectorStore, open_or_create

# Port used by the agentalloy server — must be freed between tests.
_DEFAULT_PORT = 47950


# Paths under the developer's REAL home that harness wiring writes to.
# XDG redirection does not cover them: providers resolve via Path.home().
# Three incident classes have hit real user state from tests (#87 XDG dirs,
# #88/#114 the live service, and hook wiring writing ~/.claude/settings.json
# + ~/.agentalloy/hooks twice during PR #118 development) — this tripwire
# fails the offending TEST instead of letting pollution land silently.
_REAL_HOME_SENTINELS = (
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".agentalloy",
)


def _home_fingerprint() -> tuple[tuple[str, float, int], ...]:
    out: list[tuple[str, float, int]] = []
    for path in _REAL_HOME_SENTINELS:
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime, st.st_size))
        except OSError:
            out.append((str(path), -1.0, -1))
    return tuple(out)


@pytest.fixture(autouse=True)
def _guard_real_home_wiring() -> Iterator[None]:
    """Fail any test that mutates real-home wiring artifacts."""
    before = _home_fingerprint()
    yield
    after = _home_fingerprint()
    assert after == before, (
        "Test modified REAL home wiring state (~/.claude/settings.json or "
        f"~/.agentalloy): {before} -> {after}. Patch Path.home() (see "
        "tests/install/test_claude_code_hook_wiring.py fake_home fixture)."
    )


@pytest.fixture(autouse=True)
def _pin_signal_intent_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the signals-layer intent backend to the deterministic cosine floor.

    The shipped default is ``reranker`` (a measured win — see BENCHMARKS.md), but
    that backend pair-scores against a Qwen3-Reranker server (default :47952).
    Left unpinned, any test that drives intent classification would attempt a
    live call to that port — failing open to cosine on CI (a wasted syscall) but
    silently using the *real* reranker on a dev box where :47952 is served,
    making verdicts environment-dependent. Pin cosine so the unit suite is
    hermetic, and reset the process-wide scorer cache so each test re-reads the
    backend from its env. The reranker backend is covered explicitly in
    tests/test_classifier_reranker.py, which deletes this pin to exercise the
    default-on path against a faked transport.
    """
    from agentalloy.signals.classifier import reset_intent_scorer_cache

    monkeypatch.setenv("SIGNAL_INTENT_BACKEND", "cosine")
    reset_intent_scorer_cache()


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


@pytest.fixture(scope="session", autouse=True)
def _guard_server_proc_stop() -> Iterator[None]:
    """Refuse to SIGTERM a pre-session process via ``server_proc.stop``.

    The #88 ``_free_default_port`` guard only constrains this conftest's own
    ``_kill_port`` helper. It does NOT cover production code paths a test may
    reach unmocked — ``uninstall`` (stop_services), ``server-stop``,
    ``server-restart``, and ``wrap`` all call ``server_proc.stop(pid)`` on
    whatever is listening on the configured port. ``uninstall`` even confirms
    the listener is agentalloy via ``/proc/<pid>/cmdline``, so a developer's
    real ``uvicorn agentalloy.app:app`` instance matches and gets killed.

    This session-scoped guard wraps ``server_proc.stop`` at its single seam
    and turns any attempt to stop a process that predates the pytest session
    into a no-op (mirroring the start-time check in ``_kill_port``). Leaked
    test servers necessarily start after ``_SESSION_START_EPOCH`` and are
    still stoppable, so legitimate lifecycle tests are unaffected. This
    catches ANY current or future test that reaches a real ``stop`` unmocked.
    """
    from agentalloy.install import server_proc

    real_stop = server_proc.stop

    def _guarded_stop(pid: int, timeout_s: float = 10.0) -> str:
        started = _proc_start_epoch(pid)
        if started is not None and started < _SESSION_START_EPOCH:
            # Pre-session process — not ours to kill. Report success so
            # callers treating "stopped" as a post-condition don't fail.
            return "term"
        return real_stop(pid, timeout_s=timeout_s)

    with patch.object(server_proc, "stop", _guarded_stop):
        yield


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
