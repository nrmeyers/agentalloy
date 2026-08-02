"""The harness e2e matrix: real binaries → real proxy → stub upstream.

Run: ``uv run pytest -m harness_e2e -n0 -q``

Per harness (skipped when its binary is absent):
1. Write the repo-scoped carrier with the real wiring (where one exists).
2. Launch the binary headlessly with one prompt, pointed at the proxy —
   **twice**, same prompt, same repo. See ``_run`` for why twice.
3. HARD assert: the stub upstream received a request on each run — proves
   wiring, transport, and proxy forwarding end to end, and that the second
   invocation isn't silently dropped.
4. HARD assert: **every** forwarded request — both runs — carries exactly one
   leg-3 workflow block on its system leg. Leg 3 has no cadence (#506), so a
   turn without it is the #499 regression, and it needs no corpus, so this
   holds on a plain sandboxed local run.
5. SOFT assert (``HARNESS_E2E_EXPECT_INJECTION=1``, set in nightly where a
   corpus + embed server are provisioned): run 1's forwarded last user message
   carries an AGENTALLOY marker. Scoped to run 1 on purpose — the banner is
   cadence-gated and leg 1 is gated on ``should_compose``, so run 2's *user*
   leg can legitimately be bare. That asymmetry is exactly what step 4 pins.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness_e2e.conftest import EXPECT_INJECTION
from tests.harness_e2e.drivers import CASES, HarnessCase
from tests.harness_e2e.upstream_stub import CapturedRequest, UpstreamStub, system_texts, user_texts

pytestmark = pytest.mark.harness_e2e

INJECTION_MARKER = "AGENTALLOY"
# Leg 3 lands on the system leg under the same marker pair leg 1 uses on the
# user leg — the two are told apart by which leg they land on, not by text.
LEG3_BLOCK = re.compile(r"BEGIN AGENTALLOY-CONTEXT phase=(\w+)")


def _run(
    case: HarnessCase,
    proxy: int,
    upstream_stub: UpstreamStub,
    work_repo: Path,
    label: str,
) -> list[CapturedRequest]:
    """Invoke the harness binary once; return the requests it caused.

    Called twice per case with the identical prompt — identical on purpose:
    for a fingerprint-keyed harness a same-prompt rerun in the same repo
    resolves to the SAME session key, and the cadence markers live in the
    DuckDB store inside the *proxy subprocess*. Run 2 therefore arrives at a
    proxy that has already seen this session, which is what makes a
    delivered-once record (the #499 bug class) observable: invisible on run 1,
    silent on run 2. Changing the prompt changes the fingerprint and quietly
    turns that half of the test off.

    Whether a given harness actually shares the session across two process
    launches is a property of that harness, not of us. The observable
    discriminator is leg 2: the banner is cadence-gated on (phase, session),
    so a run-2 first request WITHOUT a banner shares run 1's session, and one
    WITH a banner got a fresh one. Measured across the matrix: continue-local,
    aider, cline, hermes-agent, codex, and openclaw share; claude-code,
    opencode, qwen-code, and copilot-cli mint a new session. For the sharing
    six the doubling exercises the deliver-once path directly; for the other
    four it doubles the leg-3 sample and covers a second cold start. Both are
    worth having, and the split is worth re-measuring if a harness's session
    handling changes.

    Deliberately no tool-call emulation to force a multi-turn agent loop:
    that would mean matching ten harnesses' declared tool schemas, and a
    harness handed a call for a tool it never declared fails ten ways.
    """
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
        f"{case.name} ({label}): no request reached the stub upstream through the proxy.\n"
        f"exit={result.returncode}\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )
    return new_requests


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

    first = _run(case, proxy, upstream_stub, work_repo, "run 1")
    second = _run(case, proxy, upstream_stub, work_repo, "run 2")

    # HARD: leg 3 has no cadence — every carrier turn of both runs must carry
    # it, and exactly one block (an accumulating block is a replace that
    # missed). Unlike leg 1 this needs no corpus: the prose comes off the
    # phase's bundled workflow skill, gated only on carrier + phase, both of
    # which ``work_repo`` guarantees. The prose is read straight off the
    # wheel-bundled _packs/sdd YAML, so this holds on a cold box with no
    # corpus and no network — it does not belong in the nightly-only tier.
    #
    # If this ever flakes with a 0 mixed into the counts, the cause to check
    # first is a NON-CARRIER auxiliary request (title generation, summary):
    # no user message means no fingerprint means no session key means no leg
    # 3, correctly. The fix then is to filter those out, NOT to loosen `all`
    # to `any` — which would silently readmit the deliver-once regression.
    legs = system_texts(first + second)
    counts = [len(LEG3_BLOCK.findall(leg)) for leg in legs]
    assert counts and all(n == 1 for n in counts), (
        f"{case.name}: expected exactly one leg-3 workflow block on the system "
        f"leg of every forwarded request; got per-request counts {counts} "
        f"across {len(first)} + {len(second)} requests (run 1 + run 2).\n"
        f"A run of zeros on run 2 only is the #499 deliver-once regression.\n"
        f"system legs (truncated): {[leg[:200] for leg in legs]}"
    )

    if EXPECT_INJECTION:
        texts = user_texts(first)
        assert any(INJECTION_MARKER in t for t in texts), (
            f"{case.name}: request reached upstream but no {INJECTION_MARKER} "
            f"marker was injected into the last user message.\n"
            f"last user texts (truncated): {[t[:300] for t in texts]}"
        )
