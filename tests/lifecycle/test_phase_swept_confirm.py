"""T3 — phase-swept-by-another-session confirm (phase-boundary-confirmation).

Phase is repo-wide shared state: a phase transition clears every session's
work-item cursor and reseeds it for the new phase (see
`skill_loader._write_phase_atomic`). An *already-oriented* session whose phase
gets moved by a *different, concurrent* session on the same repo would
otherwise just silently reorient to the new phase's workflow block with no
indication anything unusual happened. When the phase file's `transitioned_by`
names a session other than the one now evaluating, the signal layer emits a
deterministic confirm directive instead of silently adopting it.

Ambiguous cases (no recorded actor, or the actor IS this session) must never
fire — false positives here would nag on every ordinary solo-session
transition. Rides the same [agentalloy-confirm] seam as T1/T2; never writes
the phase file.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import evaluate_signal
from tests.support import seed_announced, seed_phase


def _req(text: str = "continue", *, tools: bool = True) -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=text)],
        tools=[{"name": "Read", "description": "read", "input_schema": {}}] if tools else [],
    )


def _set_phase(tmp: Path, phase: str, *, transitioned_by: str | None = None) -> None:
    seed_phase(tmp, phase, actor=transitioned_by)


def _seed_announced(tmp: Path, phase: str, keys: list[str]) -> None:
    seed_announced(tmp, phase, keys)


def _ship_record(tmp: Path, slug: str = "some-feature") -> None:
    d = tmp / "docs" / "ship"
    d.mkdir(exist_ok=True, parents=True)
    (d / f"{slug}.md").write_text("# Ship\n")


async def test_swept_by_other_session_confirms(tmp_path: Path):
    # Announced state is stale (last known phase "build"); the file now reads
    # "design", moved by a different session — phase_changed=True this turn.
    _set_phase(tmp_path, "design", transitioned_by="other-session")
    _seed_announced(tmp_path, "build", ["me"])
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert sig.confirm_directives, "an existing session swept to a new phase must confirm"
    joined = "\n".join(sig.confirm_directives).lower()
    assert "design" in joined and "confirm" in joined and "different" in joined


async def test_self_transition_does_not_confirm(tmp_path: Path):
    # transitioned_by matches the CURRENT session — this session moved the
    # phase itself (e.g. the announce commit lagged); must stay quiet.
    _set_phase(tmp_path, "design", transitioned_by="me")
    _seed_announced(tmp_path, "build", ["me"])
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert not sig.confirm_directives


async def test_unknown_transitioned_by_does_not_confirm(tmp_path: Path):
    # No recorded actor (e.g. a bare CLI `phase set` outside a tracked
    # session, or a repo predating this field) — ambiguous, must not nag.
    _set_phase(tmp_path, "design")  # no transitioned_by line
    _seed_announced(tmp_path, "build", ["me"])
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert not sig.confirm_directives


async def test_swept_with_ship_landed_combines(tmp_path: Path):
    _set_phase(tmp_path, "ship", transitioned_by="other-session")
    _seed_announced(tmp_path, "qa", ["me"])
    _ship_record(tmp_path)
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert len(sig.confirm_directives) == 1
    joined = sig.confirm_directives[0].lower()
    assert "confirm" in joined and "intake" in joined and "different" in joined


async def test_swept_on_intake_is_silent(tmp_path: Path):
    _set_phase(tmp_path, "intake", transitioned_by="other-session")
    _seed_announced(tmp_path, "ship", ["me"])
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert not sig.confirm_directives


async def test_swept_confirm_does_not_write_phase(tmp_path: Path):
    _set_phase(tmp_path, "design", transitioned_by="other-session")
    _seed_announced(tmp_path, "build", ["me"])
    await evaluate_signal(_req(), tmp_path, session_id="me")
    from agentalloy.signals.skill_loader import (
        _phase_state,  # pyright: ignore[reportPrivateUsage]
    )

    state = _phase_state(tmp_path)
    assert state is not None
    assert state.phase == "design"
    assert state.transitioned_by == "other-session"


async def test_toolless_header_request_still_fires(tmp_path: Path):
    # Unified carrier gate: session_key presence is the sole carrier signal, so a
    # tool-less header-keyed request still confirms when swept to another phase.
    _set_phase(tmp_path, "design", transitioned_by="other-session")
    _seed_announced(tmp_path, "build", ["me"])
    sig = await evaluate_signal(_req(tools=False), tmp_path, session_id="me")
    assert sig.confirm_directives, "an existing session swept to a new phase must confirm"
