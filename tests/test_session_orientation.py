"""Session-aware orientation: session-key resolution + per-(phase, session) announce.

Covers ``agentalloy.api.proxy_session`` (header union, fingerprint, precedence),
the session-aware announce gate in ``evaluate_signal`` (new session re-orients on
an already-announced phase; same session stays quiet; concurrent sessions don't
thrash), and the ``session_key`` / ``session_source`` trace columns.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest import mock

import pytest

from agentalloy.api.proxy_models import ProxyMessage, ProxyRequest
from agentalloy.api.proxy_session import (
    extract_session_header,
    fingerprint_request,
    resolve_session_key,
    session_header_names,
)
from agentalloy.api.proxy_signal import commit_markers, evaluate_signal
from agentalloy.storage.vector_store import CompositionTrace, VectorStore, open_or_create

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(*user_texts: str, tools: bool = True) -> ProxyRequest:
    """A request whose messages are the given user turns (in order).

    Carries a tool array by default, modelling a genuine agent turn — the
    carrier-request gate in ``evaluate_signal`` only announces / advances the cursor
    for tool-carrying requests. Pass ``tools=False`` to model a harness background
    micro-request (quota ping, title / topic-detection haiku call) that shares the
    session id but must never burn a cadence marker.
    """
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=t) for t in user_texts],
        tools=[{"name": "Read", "description": "read a file", "input_schema": {}}]
        if tools
        else None,
    )


def _set_phase(tmp_path: Path, phase: str) -> None:
    d = tmp_path / ".agentalloy"
    d.mkdir(exist_ok=True)
    (d / "phase").write_text(f"phase: {phase}\n")


def _skill() -> dict[str, object]:
    return {
        "signal_keywords": [],
        "exit_gates": {},
        "applies_to_phases": ["build"],
        "domain_tags": None,
        "raw_prose": "Operating instructions.",
    }


def _eval(
    req: ProxyRequest, tmp_path: Path, session_id: str | None = None, *, deliver: bool = True
):  # type: ignore[no-untyped-def]
    """Evaluate the signal, then (by default) commit the deferred cadence markers.

    ``evaluate_signal`` no longer writes ``.agentalloy/{announced,composed}`` itself —
    the injection path commits them only after a non-empty block is delivered. These
    cadence tests assert the once-then-quiet behavior, which only holds when delivery
    happened, so ``_eval`` simulates that commit. Pass ``deliver=False`` to model a
    degraded turn (compose produced nothing) that must NOT burn the session.
    """
    with (
        mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value=_skill(),
        ),
        mock.patch(
            "agentalloy.api.proxy_signal.check_transition_trigger",
            return_value=None,
        ),
    ):
        result = asyncio.run(evaluate_signal(req, tmp_path, session_id=session_id))
    if deliver:
        commit_markers(
            tmp_path,
            result,
            announce_emitted=result.announce,
            cursor_emitted=result.announce_cursor,
        )
    return result


# ---------------------------------------------------------------------------
# proxy_session: header union + extraction
# ---------------------------------------------------------------------------


def test_registry_union_includes_claude_code_header() -> None:
    assert "x-claude-code-session-id" in session_header_names()


def test_extract_session_header_case_insensitive() -> None:
    assert extract_session_header({"X-Claude-Code-Session-Id": "sess-9"}) == "sess-9"
    assert extract_session_header({"x-claude-code-session-id": "  sess-9 "}) == "sess-9"


def test_extract_session_header_absent_or_empty() -> None:
    assert extract_session_header(None) is None
    assert extract_session_header({}) is None
    assert extract_session_header({"x-claude-code-session-id": "  "}) is None
    assert extract_session_header({"user-agent": "claude-cli"}) is None


# ---------------------------------------------------------------------------
# proxy_session: fingerprint + resolution precedence
# ---------------------------------------------------------------------------


def test_fingerprint_stable_for_same_first_message() -> None:
    # Later turns append assistant/user history; the FIRST user message is the
    # session anchor, so the fingerprint is stable across a session.
    a = fingerprint_request(_req("build the thing"))
    b = fingerprint_request(_req("build the thing", "more context", "and more"))
    assert a is not None and a == b


def test_fingerprint_differs_for_different_first_message() -> None:
    assert fingerprint_request(_req("task A")) != fingerprint_request(_req("task B"))


def test_fingerprint_none_without_user_text() -> None:
    req = ProxyRequest(model="m", messages=[ProxyMessage(role="system", content="hi")])
    assert fingerprint_request(req) is None


def test_resolve_prefers_header_over_fingerprint() -> None:
    key, source = resolve_session_key(_req("anything"), "sess-1")
    assert (key, source) == ("sess-1", "header")


def test_resolve_falls_back_to_fingerprint() -> None:
    key, source = resolve_session_key(_req("anything"), None)
    assert source == "fingerprint"
    assert key == fingerprint_request(_req("anything"))


def test_resolve_none_when_nothing_available() -> None:
    req = ProxyRequest(model="m", messages=[ProxyMessage(role="system", content="x")])
    assert resolve_session_key(req, None) == (None, None)


# ---------------------------------------------------------------------------
# evaluate_signal: per-(phase, session) announce cadence
# ---------------------------------------------------------------------------


def test_new_session_reorients_on_already_announced_phase(tmp_path: Path) -> None:
    _set_phase(tmp_path, "build")
    # Session A enters the phase → announces, then stays quiet.
    assert _eval(_req("a"), tmp_path, session_id="A").announce is True
    assert _eval(_req("a"), tmp_path, session_id="A").announce is False
    # Session B joins the SAME already-announced phase → re-orients. This is the
    # whole point: the per-repo announced marker must not silence a new session.
    assert _eval(_req("b"), tmp_path, session_id="B").announce is True
    # ...and B then stays quiet too.
    assert _eval(_req("b"), tmp_path, session_id="B").announce is False


def test_concurrent_sessions_do_not_thrash(tmp_path: Path) -> None:
    _set_phase(tmp_path, "build")
    # Both sessions announce once.
    assert _eval(_req("a"), tmp_path, session_id="A").announce is True
    assert _eval(_req("b"), tmp_path, session_id="B").announce is True
    # Alternating turns afterward stay quiet for both — the announced set remembers
    # both, so they don't re-announce each other every turn.
    assert _eval(_req("a"), tmp_path, session_id="A").announce is False
    assert _eval(_req("b"), tmp_path, session_id="B").announce is False
    assert _eval(_req("a"), tmp_path, session_id="A").announce is False


def test_degraded_turn_does_not_burn_session(tmp_path: Path) -> None:
    # The regression this guards: evaluate_signal must NOT record a session as
    # oriented when the turn delivered nothing (embed down / empty block / soft-fail
    # to the original body). Such a turn re-announces until one actually delivers —
    # the marker is committed only by the injection path, post-delivery.
    _set_phase(tmp_path, "build")
    # Two degraded turns in a row (no commit) keep announcing — the session is not burned.
    assert _eval(_req("a"), tmp_path, session_id="A", deliver=False).announce is True
    assert _eval(_req("a"), tmp_path, session_id="A", deliver=False).announce is True
    # A delivered turn commits the marker...
    assert _eval(_req("a"), tmp_path, session_id="A", deliver=True).announce is True
    # ...and only then does the session go quiet.
    assert _eval(_req("a"), tmp_path, session_id="A").announce is False


def test_background_request_does_not_announce_or_burn(tmp_path: Path) -> None:
    # The recurring "no orientation block" bug: a harness reuses one session id for
    # its main loop AND background micro-requests (Claude Code's quota ping, title /
    # topic-detection calls). Those carry no tools. A tool-less request must NOT
    # announce and must NOT burn the session marker, so the real agent turn that
    # follows still gets oriented.
    _set_phase(tmp_path, "build")
    # Background ping for session A arrives first (tool-less) — stays quiet, no burn.
    assert _eval(_req("quota", tools=False), tmp_path, session_id="A").announce is False
    assert _eval(_req("quota", tools=False), tmp_path, session_id="A").announce is False
    # The real agent turn for the SAME session still announces (marker not burned).
    assert _eval(_req("real task"), tmp_path, session_id="A").announce is True
    # ...then goes quiet as usual.
    assert _eval(_req("real task"), tmp_path, session_id="A").announce is False


def test_background_request_does_not_advance_cursor(tmp_path: Path) -> None:
    # The Tier 2 sibling of the gate: a tool-less background request must not advance
    # the work-item cursor either, or the domain block would be silently dropped from
    # the carrier turn that follows.
    _set_phase(tmp_path, "build")
    assert _eval(_req("quota", tools=False), tmp_path, session_id="A").announce_cursor is False


def test_fingerprint_session_reorients_when_first_message_changes(tmp_path: Path) -> None:
    # No header → fingerprint. A genuinely new conversation (different opening
    # message) re-orients; resending history (same opening) stays quiet.
    _set_phase(tmp_path, "build")
    assert _eval(_req("task one"), tmp_path).announce is True
    assert _eval(_req("task one", "follow up"), tmp_path).announce is False
    assert _eval(_req("task two"), tmp_path).announce is True


def test_phase_change_reorients_all_sessions(tmp_path: Path) -> None:
    _set_phase(tmp_path, "build")
    assert _eval(_req("a"), tmp_path, session_id="A").announce is True
    assert _eval(_req("a"), tmp_path, session_id="A").announce is False
    # Phase advances; the set resets, so the same session re-orients for the new phase.
    _set_phase(tmp_path, "qa")
    with (
        mock.patch(
            "agentalloy.api.proxy_signal._load_workflow_skill_for_phase",
            return_value={**_skill(), "applies_to_phases": ["qa"]},
        ),
        mock.patch("agentalloy.api.proxy_signal.check_transition_trigger", return_value=None),
    ):
        r = asyncio.run(evaluate_signal(_req("a"), tmp_path, session_id="A"))
    assert r.announce is True


def test_signal_result_carries_repo_and_session(tmp_path: Path) -> None:
    _set_phase(tmp_path, "build")
    r = _eval(_req("hello"), tmp_path, session_id="sess-x")
    assert r.repo == str(tmp_path)
    assert r.session_key == "sess-x"
    assert r.session_source == "header"


# ---------------------------------------------------------------------------
# Trace columns round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> VectorStore:  # type: ignore[misc]
    with open_or_create(tmp_path / "t.duck") as s:
        yield s


def test_session_columns_roundtrip(store: VectorStore) -> None:
    store.record_composition_trace(
        CompositionTrace(
            trace_id="s1",
            request_ts=int(time.time() * 1000),
            phase="build",
            task_prompt="t",
            status="compose",
            session_key="sess-1",
            session_source="header",
        )
    )
    row = {r.trace_id: r for r in store.query_traces(limit=5)}["s1"]
    assert row.session_key == "sess-1"
    assert row.session_source == "header"
