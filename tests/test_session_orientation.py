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
from agentalloy.api.proxy_signal import evaluate_signal
from agentalloy.storage.vector_store import CompositionTrace, VectorStore, open_or_create

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(*user_texts: str) -> ProxyRequest:
    """A request whose messages are the given user turns (in order)."""
    return ProxyRequest(
        model="gpt-4",
        messages=[ProxyMessage(role="user", content=t) for t in user_texts],
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


def _eval(req: ProxyRequest, tmp_path: Path, session_id: str | None = None):  # type: ignore[no-untyped-def]
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
        return asyncio.run(evaluate_signal(req, tmp_path, session_id=session_id))


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
