"""Fixtures for the harness e2e matrix.

Stands up the real stack on real sockets:

    harness binary ──► agentalloy proxy (uvicorn subprocess) ──► upstream stub

The proxy subprocess gets sandboxed XDG dirs (never contends the live
service's DuckDB lock or the user's real config) and an OS-assigned free
port. Injection assertions are tiered: transport is always asserted; marker
injection is asserted only when ``HARNESS_E2E_EXPECT_INJECTION=1`` (nightly
provisions a corpus + embed server; a sandboxed local run composes from an
empty corpus).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.harness_e2e.upstream_stub import UpstreamStub, start_upstream_stub

EXPECT_INJECTION = os.environ.get("HARNESS_E2E_EXPECT_INJECTION") == "1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def upstream_stub() -> Iterator[UpstreamStub]:
    stub = start_upstream_stub()
    yield stub
    stub.stop()


@pytest.fixture(scope="session")
def bare_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The proxy's repo-resolution root for bare (tokenless) ``/v1`` requests.

    Harnesses with user-scoped carriers (cline, openclaw) wire to the bare
    ``/v1`` surface — no ``/proj/<token>`` — so the proxy resolves their
    phase/marker state via ``AGENTALLOY_PROJECT_DIR`` / its own process cwd.
    Point both at THIS sandboxed, phase-seeded dir: the matrix then exercises
    that documented deployment shape, and the proxy can never read (or mutate —
    it did, before this fixture) the developer's checkout state.
    """
    root = tmp_path_factory.mktemp("bare-root")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True, capture_output=True)
    from agentalloy.install.subcommands.wire import (
        _seed_entry_phase,  # pyright: ignore[reportPrivateUsage]
    )

    _seed_entry_phase(root)
    return root


@pytest.fixture(autouse=True)
def _reset_bare_markers(bare_root: Path) -> None:
    """Reset per-repo cadence markers in ``bare_root`` before each case.

    All bare-surface harnesses send the SAME prompt, so they share a session
    fingerprint — without a reset, whichever runs first burns the announce
    marker for the rest and their injection assertions fail on shared state.
    """
    for name in ("announced", "composed", "banner-turns"):
        (bare_root / ".agentalloy" / name).unlink(missing_ok=True)


@pytest.fixture(scope="session")
def proxy(
    upstream_stub: UpstreamStub,
    bare_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[int]:
    """Run the real proxy as a uvicorn subprocess against the stub upstream.

    Yields the proxy port. XDG dirs are sandboxed unless
    ``HARNESS_E2E_USE_REAL_STATE=1`` (nightly sets it after provisioning the
    corpus — the runner's user scope IS the sandbox there). Repo resolution
    for tokenless requests is pinned to ``bare_root`` (env + process cwd).
    """
    port = _free_port()
    env = {**os.environ}
    env["AGENTALLOY_PROJECT_DIR"] = str(bare_root)
    if os.environ.get("HARNESS_E2E_USE_REAL_STATE") != "1":
        sandbox = tmp_path_factory.mktemp("xdg")
        env["XDG_CONFIG_HOME"] = str(sandbox / "config")
        env["XDG_DATA_HOME"] = str(sandbox / "data")
    env.update(
        {
            "UPSTREAM_URL": upstream_stub.base_url,
            "UPSTREAM_MODEL": "stub-model",
            "UPSTREAM_API_KEY": "stub-key",
            "ANTHROPIC_UPSTREAM_URL": upstream_stub.base_url,
            "RESPONSES_UPSTREAM_URL": upstream_stub.base_url,
            "LM_ASSIST": "off",
            "SIGNAL_INTENT_BACKEND": "cosine",
            "AGENTALLOY_RELEASE_CHECK": "0",
            "LOG_LEVEL": "WARNING",
        }
    )

    log_path = tmp_path_factory.mktemp("proxy") / "proxy.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentalloy.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            cwd=bare_root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    f"proxy failed to become healthy; log:\n{log_path.read_text()[-4000:]}"
                )
            time.sleep(0.5)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def work_repo(tmp_path: Path) -> Path:
    """A minimal git repo for the harness to operate in.

    Seeded with the entry phase via the same helper ``agentalloy wire`` uses:
    composition short-circuits when ``.agentalloy/phase`` is absent, so a
    wired-but-phaseless repo is inert and the nightly injection assertion
    (``HARNESS_E2E_EXPECT_INJECTION=1``) can never pass without it.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "hello.py").write_text('print("hello")\n')
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=e2e@agentalloy.test",
            "-c",
            "user.name=agentalloy-e2e",
            "commit",
            "-qm",
            "init",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    from agentalloy.install.subcommands.wire import (
        _seed_entry_phase,  # pyright: ignore[reportPrivateUsage]
    )

    _seed_entry_phase(tmp_path)
    return tmp_path
