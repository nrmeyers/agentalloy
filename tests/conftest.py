"""Shared pytest fixtures."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# Make the eval/ package importable by tests (e.g. eval/judge_local.py,
# eval/domain_tasks.py).  This is needed because ``pytest --import-mode=importlib``
# (used in CI) does not add the repo root to sys.path the way the default
# ``prepend`` mode does.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
del _REPO_ROOT

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
from agentalloy.storage.fragment_store import LanceFragmentStore

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
def _never_launch_hermes_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hermes-agent wiring from spawning a real gateway daemon.

    ``_wire_proxy_hermes_agent`` ends by (re)starting a repo-scoped hermes
    gateway via subprocess; on a dev box with hermes on PATH that would launch
    a live daemon against a tmp_path home. Tests that exercise the restart
    logic itself re-patch this (tests/install/test_hermes_agent_proxy_wiring.py
    ``TestRestartHermesGateway`` stubs subprocess.run / shutil.which instead).
    """
    from agentalloy.install.subcommands import wire_harness

    monkeypatch.setattr(wire_harness, "_restart_hermes_gateway", lambda root: True)
    # Same rationale for `mise trust`: never touch the developer's real mise
    # trust database from tests.
    monkeypatch.setattr(wire_harness, "_mise_trust", lambda path: True)


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
    """Best-effort process start time (epoch seconds). None if unknown.

    Reads /proc on Linux; falls back to ``ps -o etime=`` on platforms without
    /proc (macOS/BSD), so the port-cleanup fixture can still distinguish a
    session-leaked server from a developer's pre-existing one.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 (starttime, clock ticks since boot); fields 1-2 are
        # "pid (comm)" where comm may contain spaces — split after ')'.
        ticks = float(stat.rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(ln.split()[1]) for ln in f if ln.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return _ps_start_epoch(pid)


def _parse_etime(etime: str) -> float | None:
    """Parse a ps ``etime`` field ([[DD-]HH:]MM:SS) into elapsed seconds."""
    if not etime:
        return None
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = [int(p) for p in etime.split(":")]
        if len(parts) == 2:
            hours, mins, secs = 0, parts[0], parts[1]
        elif len(parts) == 3:
            hours, mins, secs = parts
        else:
            return None
    except ValueError:
        return None
    return days * 86400 + hours * 3600 + mins * 60 + secs


def _ps_start_epoch(pid: int) -> float | None:
    """Start time via ``ps -o etime=`` (elapsed time) for non-/proc platforms.

    macOS/BSD ps has no ``etimes`` (epoch) keyword, but ``etime`` (elapsed,
    locale-independent) is portable; subtract it from now for the start epoch.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-o", "etime=", "-p", str(pid)],
            text=True,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None
    elapsed = _parse_etime(out)
    return time.time() - elapsed if elapsed is not None else None


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
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    killed = False
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
                killed = True
    if not killed:
        return
    # SIGTERM is async and podman's rootlessport lingers; wait (bounded) for the
    # OS to actually release the port so the next port-binding test can't race it.
    for _ in range(30):
        try:
            chk = subprocess.check_output(["ss", "-tln"], text=True, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return
        if f":{port}" not in chk:
            return
        time.sleep(0.1)


@pytest.fixture(autouse=True)
def _free_default_port(request: pytest.FixtureRequest) -> Iterator[None]:
    """Kill any process this session leaked onto the default server port, before
    and after each *port-binding* test.

    Gated to tests marked ``xdist_group("port47950")`` (the real-port container/
    server tests, pinned to a single worker by ``--dist loadgroup``). For every
    other test this is a no-op — which both drops two ``ss`` subprocess calls per
    test and, under ``-n auto``, prevents one worker from SIGTERM-ing a :47950
    listener another worker just started.
    """
    marker = request.node.get_closest_marker("xdist_group")
    if not (marker and marker.args and marker.args[0] == "port47950"):
        yield
        return
    _kill_port(_DEFAULT_PORT)
    yield
    _kill_port(_DEFAULT_PORT)


# Test files that bind the real :47950 (or start real servers/containers). Under
# ``-n auto --dist loadgroup`` these are pinned to one worker so two binders never
# run concurrently, and ``_free_default_port`` arms its cleanup only for them.
_PORT47950_FILES = (
    "test_container_edge_cases",
    "test_container_service",
    "test_container_e2e",
    "test_server_proc",
    "test_port_guard",
    "test_wrap",
    "test_simple_setup_container",
    "test_backup_restore",
)

# Real-podman test files — slow + port-bound; flaky in a fast parallel run. The
# ``container`` marker excludes them from the default suite (see pyproject
# addopts); they run serially via ``pytest -m container -n0`` (CI + on demand).
_CONTAINER_FILES = (
    "test_container_edge_cases",
    "test_container_service",
    "test_container_e2e",
    "test_container_code_index",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if any(name in item.nodeid for name in _PORT47950_FILES):
            item.add_marker(pytest.mark.xdist_group("port47950"))
        if any(name in item.nodeid for name in _CONTAINER_FILES):
            item.add_marker(pytest.mark.container)


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
    # Skip the production lifespan (which opens the DuckDB/Lance stores + embedder).
    # Per-test fixtures wire dependency_overrides explicitly.
    return create_app(use_default_lifespan=False)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def vector_store(tmp_path: Path) -> Iterator[LanceFragmentStore]:
    """Empty Lance fragment store at a tmp path. Tests that exercise
    compose/retrieve construction use this for the ``vector_store``
    constructor parameter (a FragmentStore in v5). Empty store means
    search_similar returns no hits — fine for tests that mock retrieval
    results anyway."""
    fs = LanceFragmentStore(tmp_path / "fragments.lance")
    try:
        yield fs
    finally:
        fs.close()


# ---------------------------------------------------------------------------
# Shared corpus template — built once per session, copied per test.
#
# Rebuilding the 8-skill fixture corpus per test (LadybugStore.migrate() +
# load_fixtures() + StubLMClient reembed, ~2-4s each) dominated suite wall time.
# Build it ONCE into a session template; each test gets an isolated copytree
# (copy << rebuild). Under xdist the session scope is per-worker, so it's built
# once per process, not once per test.
# ---------------------------------------------------------------------------

_STUB_EMBED_MODEL = "stub-embed"


@pytest.fixture(scope="session")
def corpus_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the fixture corpus (DuckDB skill graph + embedded Lance dataset)
    once per session. Returns a directory holding ``agentalloy.duck`` and
    ``fragments.lance``."""
    from agentalloy.fixtures.loader import load_fixtures
    from agentalloy.install.importer import reembed_corpus
    from agentalloy.storage.skill_store import open_skill_store
    from tests.support import StubLMClient

    base = tmp_path_factory.mktemp("corpus_template")
    ss = open_skill_store(str(base / "agentalloy.duck"))
    ss.migrate()
    load_fixtures(ss)
    stub = StubLMClient()
    fs = LanceFragmentStore(base / "fragments.lance")
    reembed_corpus(
        fs,
        ss,
        embed=lambda texts: stub.embed(model=_STUB_EMBED_MODEL, texts=texts),
        model=_STUB_EMBED_MODEL,
    )
    fs.rebuild_fts_index()  # BM25 leg + fallback path need the FTS index
    fs.close()
    ss.close()
    return base


@pytest.fixture
def corpus_dir(corpus_template: Path, tmp_path: Path) -> Path:
    """An isolated per-test copy of the session corpus template. Returns a dir
    holding ``agentalloy.duck`` (file) + ``fragments.lance`` (dir). Cheap (copy,
    not rebuild), so tests that mutate the store stay independent."""
    import shutil

    dst = tmp_path / "corpus"
    shutil.copytree(corpus_template, dst)
    return dst
