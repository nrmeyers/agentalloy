"""T2 — new-session phase confirm (phase-boundary-confirmation).

When a fresh session (its key not yet oriented for the current phase) resumes on a
non-intake phase, the signal layer emits a deterministic confirm directive telling
the agent to confirm the phase with the user before adopting it — the per-repo
phase file is contended by concurrent sessions, so a stale mid-build resume is
worth a check. Rides the same [agentalloy-confirm] seam and (phase, session)
marker as T1; never writes the phase file.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_signal import evaluate_signal


def _req(text: str = "continue", *, tools: bool = True) -> ProxyRequest:
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=text)],
        tools=[{"name": "Read", "description": "read", "input_schema": {}}] if tools else [],
    )


def _set_phase(tmp: Path, phase: str) -> None:
    d = tmp / ".agentalloy"
    d.mkdir(exist_ok=True, parents=True)
    (d / "phase").write_text(f"phase: {phase}\n")


def _seed_announced(tmp: Path, phase: str, keys: list[str]) -> None:
    """Record `(phase, keys)` as already-oriented — the `.agentalloy/announced`
    format is `<phase>\\t<key1>,<key2>`. A session key NOT in `keys` is 'new'."""
    d = tmp / ".agentalloy"
    d.mkdir(exist_ok=True, parents=True)
    (d / "announced").write_text(f"{phase}\t{','.join(keys)}")


def _ship_record(tmp: Path, slug: str = "some-feature") -> None:
    d = tmp / "docs" / "ship"
    d.mkdir(exist_ok=True, parents=True)
    (d / f"{slug}.md").write_text("# Ship\n")


async def test_new_session_on_build_confirms(tmp_path: Path):
    _set_phase(tmp_path, "build")
    _seed_announced(tmp_path, "build", ["other-session"])  # our key is unseen → new
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert sig.confirm_directives, "a new session on a non-intake phase must confirm"
    joined = "\n".join(sig.confirm_directives).lower()
    assert "build" in joined and "confirm" in joined


async def test_new_session_on_intake_is_silent(tmp_path: Path):
    _set_phase(tmp_path, "intake")
    _seed_announced(tmp_path, "intake", ["other-session"])
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert not sig.confirm_directives, "intake resume is the happy path — no confirm"


async def test_known_session_does_not_reconfirm(tmp_path: Path):
    # Once the session is in the (phase, session) marker set, the confirm goes quiet
    # (AC-4 once-per-session — rides the announce marker).
    _set_phase(tmp_path, "design")
    _seed_announced(tmp_path, "design", ["me"])  # already oriented
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert not sig.confirm_directives


async def test_any_non_intake_phase_fires(tmp_path: Path):
    for phase in ("spec", "design", "build", "qa", "ship"):
        _set_phase(tmp_path, phase)
        _seed_announced(tmp_path, phase, ["other"])
        sig = await evaluate_signal(_req(), tmp_path, session_id="me")
        assert sig.confirm_directives, f"new session on {phase} must confirm"


async def test_precedence_single_directive_on_ship_with_record(tmp_path: Path):
    # New session lands on ship WITH a delivery record → exactly ONE combined
    # directive, never two conflicting MUST blocks.
    _set_phase(tmp_path, "ship")
    _seed_announced(tmp_path, "ship", ["other"])
    _ship_record(tmp_path)
    sig = await evaluate_signal(_req(), tmp_path, session_id="me")
    assert len(sig.confirm_directives) == 1
    joined = sig.confirm_directives[0].lower()
    assert "confirm" in joined and "intake" in joined  # phase-confirm + reset-ask folded


async def test_toolless_header_request_does_not_fire(tmp_path: Path):
    # Carrier gate: a background tool-less header-keyed request must not fire or
    # burn the confirm (orientation-carrier-request-race).
    _set_phase(tmp_path, "build")
    _seed_announced(tmp_path, "build", ["other"])
    sig = await evaluate_signal(_req(tools=False), tmp_path, session_id="me")
    assert not sig.confirm_directives


async def test_new_session_confirm_does_not_write_phase(tmp_path: Path):
    _set_phase(tmp_path, "build")
    _seed_announced(tmp_path, "build", ["other"])
    await evaluate_signal(_req(), tmp_path, session_id="me")
    assert (tmp_path / ".agentalloy" / "phase").read_text() == "phase: build\n"
