"""State leg tests.

Tests the structured JSON context briefing that replaces CLI-based state queries:
- ``build_state_leg()`` — JSON shape, minimal state, contract data, gate status
- State injection via ``inject_into_anthropic_messages(kind="state")`` — strip-and-replace
- State injection via ``inject_into_openai_messages(kind="state")`` — same pattern
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from agentalloy.api.proxy_injection import (
    STATE_MARKER_BEGIN,
    STATE_MARKER_END,
    inject_into_anthropic_messages,
    inject_into_openai_messages,
)
from agentalloy.api.state_leg import build_state_leg

# ---------------------------------------------------------------------------
# build_state_leg() — JSON shape and content
# ---------------------------------------------------------------------------


class TestBuildStateLeg:
    def test_minimal_state_has_phase_and_mode(self) -> None:
        result = build_state_leg("build")
        assert result is not None
        state = json.loads(result)
        assert state["phase"] == "build"
        assert state["mode"] == "workflow"

    def test_paused_mode(self) -> None:
        result = build_state_leg("design", paused_mode=True)
        assert result is not None
        state = json.loads(result)
        assert state["mode"] == "paused"

    def test_empty_phase_returns_none(self) -> None:
        assert build_state_leg("") is None

    def test_gate_status(self) -> None:
        result = build_state_leg(
            "build",
            gates_met=["scope_touched_in_diff"],
            gates_unmet=["tests_present"],
        )
        assert result is not None
        state = json.loads(result)
        assert state["gates"]["passing"] == ["scope_touched_in_diff"]
        assert state["gates"]["failing"] == ["tests_present"]
        assert state["gates"]["blocked"] is True

    def test_gates_all_passing(self) -> None:
        result = build_state_leg(
            "spec",
            gates_met=["artifact_exists", "approval_recorded"],
            gates_unmet=[],
        )
        assert result is not None
        state = json.loads(result)
        assert state["gates"]["blocked"] is False

    def test_no_gates_omits_gate_section(self) -> None:
        result = build_state_leg("intake")
        assert result is not None
        state = json.loads(result)
        assert "gates" not in state

    def test_actions_include_record_artifact(self) -> None:
        result = build_state_leg("build")
        assert result is not None
        state = json.loads(result)
        assert "record_artifact" in state["actions"]

    def test_actions_show_blocked_when_gates_unmet(self) -> None:
        result = build_state_leg(
            "build",
            gates_unmet=["tests_present"],
        )
        assert result is not None
        state = json.loads(result)
        assert "blocked" in state["actions"]

    def test_actions_show_advance_when_gates_pass(self) -> None:
        result = build_state_leg(
            "build",
            gates_met=["scope_touched_in_diff", "tests_present"],
            gates_unmet=[],
        )
        assert result is not None
        state = json.loads(result)
        assert "advance_phase" in state["actions"]

    def test_actions_always_include_query(self) -> None:
        result = build_state_leg("design")
        assert result is not None
        state = json.loads(result)
        assert "query" in state["actions"]
        assert "agentalloy_query" in state["actions"]["query"]

    def test_contract_state_from_store(self) -> None:
        store = MagicMock()
        store.get_contract.return_value = {
            "contract_id": "build/auth-refactor",
            "phase": "build",
            "slug": "auth-refactor",
            "domain_tags": '["fastapi", "auth"]',
            "scope_touches": '["src/auth/**"]',
            "scope_avoids": "[]",
            "success_criteria": '["Token validation works"]',
            "related_contracts": "[]",
            "body": "Implement auth refactor",
            "created_at": "2026-01-01T00:00:00",
            "route": "full",
        }
        store.list_artifacts.return_value = [
            {"name": "design.artifact", "content": "## Architecture\nEvent sourcing chosen."},
        ]

        result = build_state_leg(
            "build",
            store=store,
            contract_id="build/auth-refactor",
        )
        assert result is not None
        state = json.loads(result)
        assert "contract" in state
        assert state["contract"]["slug"] == "auth-refactor"
        assert "fastapi" in state["contract"]["domain_tags"]
        assert state["contract"]["scope"]["touches"] == ["src/auth/**"]
        assert "artifacts" in state["contract"]
        assert state["contract"]["artifacts"]["design.artifact"]["recorded"] is True

    def test_store_failure_yields_state_without_contract(self) -> None:
        store = MagicMock()
        store.get_contract.side_effect = RuntimeError("db error")

        result = build_state_leg(
            "build",
            store=store,
            contract_id="build/broken",
        )
        assert result is not None
        state = json.loads(result)
        assert state["phase"] == "build"
        assert "contract" not in state

    def test_no_store_omits_contract(self) -> None:
        result = build_state_leg("build")
        assert result is not None
        state = json.loads(result)
        assert "contract" not in state

    def test_no_repo_root_omits_scope(self) -> None:
        result = build_state_leg("build")
        assert result is not None
        state = json.loads(result)
        assert "scope" not in state

    def test_scope_present_when_repo_root_given(self) -> None:
        result = build_state_leg("build", repo_root="/home/u/dev/agentalloy")
        assert result is not None
        state = json.loads(result)
        assert state["scope"]["repo_root"] == "/home/u/dev/agentalloy"
        assert state["scope"]["repo"] == "agentalloy"
        assert state["scope"]["stream_id"]
        assert state["scope"]["service"]

    def test_scope_query_hint_carries_service_and_params(self) -> None:
        result = build_state_leg("build", repo_root="/home/u/dev/agentalloy")
        assert result is not None
        state = json.loads(result)
        scope = state["scope"]
        query = state["actions"]["query"]
        assert scope["service"] in query
        assert f"?repo_root={scope['repo_root']}" in query
        assert f"?repo={scope['repo']}" in query


# ---------------------------------------------------------------------------
# State injection — Anthropic surface
# ---------------------------------------------------------------------------

STATE_JSON = json.dumps({"phase": "build", "mode": "workflow"}, indent=2)


class TestAnthropicStateInjection:
    def test_state_injects_into_last_user_string(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "earlier"},
                {"role": "user", "content": "latest user"},
            ],
        }
        result = inject_into_anthropic_messages(payload, STATE_JSON, phase="build", kind="state")
        last = result["messages"][1]["content"]
        assert STATE_MARKER_BEGIN in last
        assert STATE_MARKER_END in last
        assert STATE_JSON in last
        assert last.startswith("latest user")
        assert result["messages"][0]["content"] == "earlier"

    def test_state_injects_into_last_user_list(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
        }
        result = inject_into_anthropic_messages(payload, STATE_JSON, phase="build", kind="state")
        content = result["messages"][0]["content"]
        assert isinstance(content, list)
        joined = "\n".join(b.get("text", "") for b in content)
        assert STATE_MARKER_BEGIN in joined
        assert STATE_JSON in joined

    def test_second_state_strip_replaces_no_stacking(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "messages": [{"role": "user", "content": "hi"}],
        }
        state1 = json.dumps({"phase": "build"}, indent=2)
        state2 = json.dumps({"phase": "design"}, indent=2)
        once = inject_into_anthropic_messages(payload, state1, phase="build", kind="state")
        twice = inject_into_anthropic_messages(once, state2, phase="design", kind="state")
        content = twice["messages"][0]["content"]
        assert content.count(STATE_MARKER_BEGIN) == 1
        assert state2 in content
        assert state1 not in content
        assert content.startswith("hi")

    def test_state_does_not_disturb_workflow_block(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "messages": [{"role": "user", "content": "hi"}],
        }
        from agentalloy.api.proxy_injection import ANTHROPIC_MARKER_END, anthropic_marker_begin

        with_workflow = inject_into_anthropic_messages(
            payload, "workflow prose", phase="build", kind="workflow"
        )
        with_both = inject_into_anthropic_messages(
            with_workflow, STATE_JSON, phase="build", kind="state"
        )
        content = with_both["messages"][0]["content"]
        assert anthropic_marker_begin("build") in content
        assert ANTHROPIC_MARKER_END in content
        assert STATE_MARKER_BEGIN in content
        assert STATE_MARKER_END in content
        assert "workflow prose" in content
        assert STATE_JSON in content

    def test_state_does_not_disturb_banner(self) -> None:
        payload: dict[str, Any] = {
            "model": "claude",
            "messages": [{"role": "user", "content": "hi"}],
        }
        from agentalloy.api.proxy_injection import BANNER_MARKER_BEGIN, BANNER_MARKER_END

        with_banner = inject_into_anthropic_messages(
            payload, "banner text", phase="build", kind="banner"
        )
        with_both = inject_into_anthropic_messages(
            with_banner, STATE_JSON, phase="build", kind="state"
        )
        content = with_both["messages"][0]["content"]
        assert BANNER_MARKER_BEGIN in content
        assert BANNER_MARKER_END in content
        assert STATE_MARKER_BEGIN in content
        assert "banner text" in content
        assert STATE_JSON in content


# ---------------------------------------------------------------------------
# State injection — OpenAI surface
# ---------------------------------------------------------------------------


class TestOpenAIStateInjection:
    def test_state_injects_string_content(self) -> None:
        from agentalloy.api.proxy_models import ProxyMessage

        messages = [
            ProxyMessage(role="user", content="earlier"),
            ProxyMessage(role="user", content="latest"),
        ]
        result = inject_into_openai_messages(messages, STATE_JSON, phase="build", kind="state")
        assert result is not None
        assert STATE_MARKER_BEGIN in result[1].content
        assert STATE_JSON in result[1].content
        assert result[0].content == "earlier"

    def test_state_strip_replaces(self) -> None:
        from agentalloy.api.proxy_models import ProxyMessage

        messages = [ProxyMessage(role="user", content="hi")]
        state1 = json.dumps({"phase": "build"}, indent=2)
        state2 = json.dumps({"phase": "design"}, indent=2)
        once = inject_into_openai_messages(messages, state1, phase="build", kind="state")
        assert once is not None
        twice = inject_into_openai_messages(once, state2, phase="design", kind="state")
        assert twice is not None
        assert STATE_MARKER_BEGIN in twice[0].content
        assert state2 in twice[0].content
        assert state1 not in twice[0].content
