"""The harness e2e matrix: real binaries → real proxy → stub upstream.

Run: ``uv run pytest -m harness_e2e -n0 -q``

Per harness (skipped when its binary is absent):
1. Write the repo-scoped carrier with the real wiring (where one exists).
2. Launch the binary headlessly with one prompt, pointed at the proxy.
3. HARD assert: the stub upstream received the request — proves wiring,
   transport, and proxy forwarding end to end.
4. SOFT assert (``HARNESS_E2E_EXPECT_INJECTION=1``, set in nightly where a
   corpus + embed server are provisioned): the forwarded last user message
   carries an AGENTALLOY injection marker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness_e2e.conftest import EXPECT_INJECTION
from tests.harness_e2e.drivers import CASES, HarnessCase
from tests.harness_e2e.upstream_stub import UpstreamStub, user_texts

pytestmark = pytest.mark.harness_e2e

INJECTION_MARKER = "AGENTALLOY"


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_harness_roundtrip(
    case: HarnessCase,
    proxy: int,
    upstream_stub: UpstreamStub,
    work_repo: Path,
) -> None:
    if shutil.which(case.binary) is None:
        pytest.skip(f"{case.binary} not installed")
    if case.xfail_reason:
        pytest.xfail(case.xfail_reason)

    if case.wire is not None:
        case.wire(proxy, work_repo)

    env = {**os.environ}
    for key in case.scrub_env:
        env.pop(key, None)
    env.update(case.env(proxy, work_repo))
    # A shell syncs $PWD on cd; subprocess(cwd=...) does not, leaving pytest's
    # own directory in it. opencode ≥1.17 trusts $PWD over the process cwd for
    # project resolution — with the stale value it loads no repo opencode.json
    # and dies with ProviderModelNotFoundError. Model the shell.
    env["PWD"] = str(work_repo)

    before = len(upstream_stub.captured)
    result = subprocess.run(
        case.argv(work_repo),
        cwd=work_repo,
        env=env,
        # Headless: an inherited stdin pipe makes some harnesses (codex) block
        # on "reading additional input from stdin".
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=case.timeout,
    )

    new_requests = upstream_stub.captured[before:]
    assert new_requests, (
        f"{case.name}: no request reached the stub upstream through the proxy.\n"
        f"exit={result.returncode}\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )

    if EXPECT_INJECTION:
        texts = user_texts(new_requests)
        assert any(INJECTION_MARKER in t for t in texts), (
            f"{case.name}: request reached upstream but no {INJECTION_MARKER} "
            f"marker was injected into the last user message.\n"
            f"last user texts (truncated): {[t[:300] for t in texts]}"
        )
