"""Protocol surface (M1a) — schemas, parsers, prompts, transcript helpers.

The contract is: every action schema is strict (closed, fully required),
parsers fail closed on anything malformed or out-of-range, and the prompts
are fully deterministic (no clock, no randomness, no ambient context).
"""

from __future__ import annotations

import json

import pytest

from agentalloy.local_agent.protocol import (
    ACTION_DESCRIPTIONS,
    ACTIONS,
    PHASES,
    Action,
    action_schema,
    build_answer_prompt,
    build_classify_prompt,
    build_fill_prompt,
    classify_response_format,
    digest_result,
    fill_response_format,
    parse_classify,
    parse_fill,
    render_transcript,
)


class TestParseClassify:
    def test_valid_action(self) -> None:
        assert parse_classify('{"action": "code_search"}') is Action.CODE_SEARCH

    def test_code_fenced_json(self) -> None:
        assert parse_classify('```json\n{"action": "none"}\n```') is Action.NONE

    def test_every_action_parses(self) -> None:
        for action in ACTIONS:
            assert parse_classify(json.dumps({"action": action.value})) is action

    def test_bare_string_is_none(self) -> None:
        # The parser requires a JSON object; a bare word is not one.
        assert parse_classify("none") is None

    def test_unknown_action_is_none(self) -> None:
        assert parse_classify('{"action": "vibe_check"}') is None

    def test_non_string_action_is_none(self) -> None:
        assert parse_classify('{"action": 42}') is None

    def test_non_json_is_none(self) -> None:
        assert parse_classify("I think code_search") is None
        assert parse_classify("") is None


class TestParseFill:
    def test_valid_args_pass_through(self) -> None:
        args = parse_fill(Action.CODE_SEARCH, '{"query": "cache eviction", "k": 5}')
        assert args == {"query": "cache eviction", "k": 5}

    def test_missing_nullable_normalized_to_none(self) -> None:
        args = parse_fill(Action.KNOWLEDGE_ENTITIES, '{"query": "Symbol.x"}')
        assert args is not None and args == {"query": "Symbol.x", "kind": None}

    def test_missing_nullable_phase_normalized(self) -> None:
        args = parse_fill(Action.ARTIFACT_BODY, '{"slug": "01-auth", "query": "design.md"}')
        assert args is not None and args["phase"] is None

    def test_invented_key_rejected(self) -> None:
        assert parse_fill(Action.CODE_SEARCH, '{"query": "x", "k": 1, "extra": 1}') is None

    def test_missing_required_rejected(self) -> None:
        assert parse_fill(Action.CODE_SEARCH, '{"k": 1}') is None
        assert parse_fill(Action.TELEMETRY, "{}") is None

    def test_type_violations_rejected(self) -> None:
        assert parse_fill(Action.CODE_SEARCH, '{"query": "x", "k": true}') is None
        assert parse_fill(Action.CODE_SEARCH, '{"query": 7, "k": 1}') is None
        assert parse_fill(Action.TELEMETRY, '{"k": "10"}') is None

    def test_enum_violation_rejected(self) -> None:
        assert parse_fill(Action.TELEMETRY, '{"k": 10, "phase": "vibes"}') is None

    def test_min_max_enforced(self) -> None:
        assert parse_fill(Action.CODE_SEARCH, '{"query": "x", "k": 0}') is None
        assert parse_fill(Action.CODE_SEARCH, '{"query": "x", "k": 101}') is None

    def test_not_json(self) -> None:
        assert parse_fill(Action.SYMBOLS, "definitely not json") is None


class TestSchemas:
    def test_every_schema_is_strict(self) -> None:
        for action in ACTIONS:
            schema = action_schema(action)
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
            # Strict JSON schema: every property is required.
            assert set(schema["required"]) == set(schema["properties"])

    def test_response_formats_are_strict_json_schema(self) -> None:
        rf = classify_response_format()
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "classify"
        enum = rf["json_schema"]["schema"]["properties"]["action"]["enum"]
        assert enum == [action.value for action in ACTIONS]

        frf = fill_response_format(Action.TELEMETRY)
        assert frf["json_schema"]["name"] == "fill_telemetry"
        assert frf["json_schema"]["strict"] is True

    def test_action_descriptions_cover_every_action(self) -> None:
        assert set(ACTION_DESCRIPTIONS) == set(ACTIONS)

    def test_phases_match_store_canonical_set(self) -> None:
        assert "sdd-fast" in PHASES


class TestTranscriptHelpers:
    def test_digest_under_cap_unchanged(self) -> None:
        assert digest_result("hello", 100) == "hello"

    def test_digest_truncates_with_suffixed_note(self) -> None:
        out = digest_result("x" * 100, 10)
        assert out.startswith("x" * 10)
        assert "truncated 90 of 100 chars" in out

    def test_digest_nonpositive_cap_passthrough(self) -> None:
        assert digest_result("x" * 100, 0) == "x" * 100

    def test_render_empty(self) -> None:
        assert render_transcript([]) == "(none yet)"

    def test_render_with_and_without_args(self) -> None:
        out = render_transcript(
            [("code_search", {"query": "q", "k": 3}, "3 result(s):"), ("none", {}, "ok")]
        )
        assert "STEP 1: code_search(query='q', k=3)" in out
        assert "3 result(s):" in out
        assert "STEP 2: none" in out
        assert "ok" in out


class TestPromptBuilders:
    def test_classify_prompt_lists_every_action_and_question(self) -> None:
        prompt = build_classify_prompt("how does auth work?", "")
        for action in ACTIONS:
            assert action.value in prompt.user
        assert "how does auth work?" in prompt.user

    def test_classify_prompt_includes_phase(self) -> None:
        prompt = build_classify_prompt("q", "", phase="build")
        assert "CURRENT PHASE\nbuild" in prompt.user

    def test_classify_prompt_empty_transcript_placeholder(self) -> None:
        assert "(none yet)" in build_classify_prompt("q", "").user

    def test_classify_prompt_is_deterministic(self) -> None:
        a = build_classify_prompt("q", "t", phase="build")
        b = build_classify_prompt("q", "t", phase="build")
        assert a == b

    def test_fill_prompt_carries_retry_feedback(self) -> None:
        prompt = build_fill_prompt(
            Action.CONTRACT_DETAIL, "q", "", validation_error="expected slug X"
        )
        assert "expected slug X" in prompt.user
        assert "FAILED VALIDATION" in prompt.user

    def test_fill_prompt_raises_for_none(self) -> None:
        with pytest.raises(ValueError):
            build_fill_prompt(Action.NONE, "q", "")

    def test_answer_prompt_contains_transcript_and_question(self) -> None:
        prompt = build_answer_prompt("what is the plan?", "STEP 1: code_search\nfound it")
        assert "found it" in prompt.user
        assert "what is the plan?" in prompt.user
