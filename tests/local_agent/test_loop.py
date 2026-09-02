"""LocalAgentLoop (M1c) — the classify → fill → validate → execute → answer spine.

Pins the per-step budget the orchestrator's prose contracts: one classify
retry, one plain-text retry per empty completion, one schema/retry for an
invalid fill, one validation retry, and an answer attempt at every stop —
including degraded ones (the answer degrades into the raw transcript).
"""

from __future__ import annotations

import json

import pytest

from agentalloy.local_agent.client import ClientStageError
from agentalloy.local_agent.config import LocalAgentConfig, LocalAgentMode
from agentalloy.local_agent.executors import ExecutorError
from agentalloy.local_agent.loop import LocalAgentLoop
from agentalloy.local_agent.protocol import Action, digest_result
from agentalloy.local_agent.validate import ValidationCheck

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Plays back a fixed script; each item is a completion string or an
    exception to raise. Records every call for the prompt-shape asserts."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(
        self, prompt, *, stage: str, max_tokens: int, response_format: dict | None = None
    ) -> tuple[str, float]:
        self.calls.append(
            {
                "stage": stage,
                "system": prompt.system,
                "user": prompt.user,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert item is not None
        return item, 3


class _Validator:
    """Passes every check except the scripted errors, which it pops in order."""

    def __init__(self, errors: list[str] | None = None) -> None:
        self._errors = list(errors) if errors else []
        self.calls: list[tuple[Action, dict]] = []

    async def validate(self, action: Action, args: dict) -> ValidationCheck:
        self.calls.append((action, dict(args)))
        if self._errors:
            return ValidationCheck(False, self._errors.pop(0))
        return ValidationCheck(True)


class _Executor:
    def __init__(self, error: str | None = None, text: str | None = None) -> None:
        self._error = error
        self._text = text
        self.calls: list[tuple[Action, dict, str | None]] = []

    async def execute(self, action: Action, args: dict, *, repo_slug: str | None = None) -> str:
        self.calls.append((action, dict(args), repo_slug))
        if self._error is not None:
            raise ExecutorError(self._error)
        return self._text if self._text is not None else f"{action.value} ok"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides: object) -> LocalAgentConfig:
    kwargs: dict = {
        "mode": LocalAgentMode.ON,
        "url": "http://127.0.0.1:59999",
        "model": "test-model",
        "timeout_ms": 30000,
        "max_steps": 2,
        "max_tokens": 2048,
        "result_cap_chars": 2400,
    }
    kwargs.update(overrides)
    return LocalAgentConfig(**kwargs)


def _loop(
    client: _ScriptedClient, config: LocalAgentConfig, validator: _Validator, executor: _Executor
) -> LocalAgentLoop:
    return LocalAgentLoop(client, config=config, validator=validator, executors=executor)


def _empty(stage: str = "classify") -> ClientStageError:
    return ClientStageError(stage, "empty completion content", None, empty_completion=True)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_classify_fill_execute_none_answer(self) -> None:
        doc = {"phase": "build", "slug": "auth-fix", "query": "design.md"}
        client = _ScriptedClient(
            [
                '{"action": "artifact_body"}',
                json.dumps(doc),
                '{"action": "none"}',
                "The design lives in design.md.",
            ]
        )
        validator = _Validator()
        executor = _Executor()
        loop = _loop(client, _config(), validator, executor)

        result = await loop.run("Where is the auth design?", repo="main-repo", phase="build")

        assert result.answer == "The design lives in design.md."
        assert result.stop_reason == "none"
        assert result.degraded is False
        assert result.degrade_reason is None
        assert result.model_tag == "test-model"
        assert result.total_ms >= 0

        assert len(result.steps) == 2
        first, last = result.steps
        assert (first.step, first.action, first.args, first.validation) == (
            1,
            "artifact_body",
            doc,
            "ok",
        )
        assert first.result_chars == len("artifact_body ok")
        assert (last.step, last.action, last.args) == (2, "none", {})

        assert [c["stage"] for c in client.calls] == ["classify", "fill", "classify", "answer"]
        assert client.calls[0]["response_format"]["json_schema"]["name"] == "classify"
        assert client.calls[1]["response_format"]["json_schema"]["name"] == "fill_artifact_body"
        assert client.calls[3]["response_format"] is None
        assert "CURRENT PHASE\nbuild" in client.calls[0]["user"]
        # The transcript carries the executed step before the answer is asked.
        assert (
            "STEP 1: artifact_body(phase='build', slug='auth-fix', query='design.md')"
            in client.calls[3]["user"]
        )
        # The executor and validator ran against the request's repo slug.
        assert executor.calls == [(Action.ARTIFACT_BODY, doc, "main-repo")]
        assert validator.calls == [(Action.ARTIFACT_BODY, doc)]

    async def test_result_digest_is_capped_before_the_transcript(self) -> None:
        big = "x" * 50
        # Two-step budget: the second classify must also be scripted (none).
        client = _ScriptedClient(
            ['{"action": "code_search"}', '{"query": "q", "k": 3}', '{"action": "none"}', "done"]
        )
        executor = _Executor(text=big)
        loop = _loop(client, _config(result_cap_chars=10), _Validator(), executor)

        result = await loop.run("q")

        assert result.steps[0].result_chars == len(digest_result(big, 10))
        assert client.calls[3]["user"].count(big) == 0
        assert "[truncated" in client.calls[3]["user"]

    async def test_stage_latencies_recorded(self) -> None:
        client = _ScriptedClient(['{"action": "none"}', "nothing to look up"])
        result = await _loop(client, _config(), _Validator(), _Executor()).run("q")
        # The 'none' step has a classify latency but no fill.
        assert result.steps[0].stage_latencies_ms == {"classify": 3}


# ---------------------------------------------------------------------------
# Classify leg
# ---------------------------------------------------------------------------


class TestClassifyLeg:
    async def test_none_answers_immediately(self) -> None:
        client = _ScriptedClient(['{"action": "none"}', "Nothing to look up."])
        result = await _loop(client, _config(), _Validator(), _Executor()).run("q")
        assert result.answer == "Nothing to look up."
        assert result.stop_reason == "none"
        assert result.steps[0].action == "none"
        assert [c["stage"] for c in client.calls] == ["classify", "answer"]

    async def test_invalid_classify_retries_once_then_degrades(self) -> None:
        client = _ScriptedClient(["not json", "still not json"])
        result = await _loop(client, _config(max_steps=1), _Validator(), _Executor()).run("q")
        assert result.degraded is True
        assert result.stop_reason == "classify_failed"
        assert result.degrade_reason == "classify_failed"
        assert [c["stage"] for c in client.calls] == ["classify", "classify_retry"]
        assert result.steps[0].validation == "schema-invalid classify output after one retry"
        # A degraded result still carries an answer — the raw transcript.
        assert result.answer == "(no steps were executed)"

    async def test_empty_classify_gets_plaintext_retry(self) -> None:
        client = _ScriptedClient(
            [
                _empty("classify"),
                '{"action": "none"}',
                "Nothing to look up.",
            ]
        )
        result = await _loop(client, _config(), _Validator(), _Executor()).run("q")
        assert result.degraded is False
        assert [c["stage"] for c in client.calls] == ["classify", "classify_plaintext", "answer"]
        # The plain-text retry drops the response_format.
        assert client.calls[1]["response_format"] is None

    async def test_empty_classify_both_modes_propagates_to_router(self) -> None:
        client = _ScriptedClient([_empty("classify"), _empty("classify_plaintext")])
        loop = _loop(client, _config(), _Validator(), _Executor())
        with pytest.raises(ClientStageError) as exc:
            await loop.run("q")
        assert "both schema and plain-text modes" in str(exc.value)


# ---------------------------------------------------------------------------
# Fill leg
# ---------------------------------------------------------------------------


class TestFillLeg:
    async def test_schema_invalid_fill_retries_with_feedback(self) -> None:
        client = _ScriptedClient(
            [
                '{"action": "code_search"}',
                "oops, not json",
                '{"query": "auth bug", "k": 5}',
                '{"action": "none"}',
                "found it",
            ]
        )
        executor = _Executor()
        loop = _loop(client, _config(), _Validator(), executor)
        result = await loop.run("where is the auth bug?")
        assert result.degraded is False
        assert [c["stage"] for c in client.calls] == [
            "classify",
            "fill",
            "fill_retry",
            "classify",
            "answer",
        ]
        assert "not a valid JSON object" in client.calls[2]["user"]
        assert executor.calls == [(Action.CODE_SEARCH, {"query": "auth bug", "k": 5}, None)]

    async def test_validation_error_feeds_single_retry(self) -> None:
        err = "argument 'slug'='x' is not a known contract"
        client = _ScriptedClient(
            [
                '{"action": "contract_detail"}',
                '{"slug": "x"}',
                '{"slug": "y"}',
                '{"action": "none"}',
                "done",
            ]
        )
        validator = _Validator(errors=[err])
        executor = _Executor()
        loop = _loop(client, _config(), validator, executor)
        result = await loop.run("show the contract")
        assert result.degraded is False
        # The retry carries the index-grounded error verbatim, mid-prompt.
        assert f"YOUR PREVIOUS ATTEMPT FAILED VALIDATION:\n{err}\n" in client.calls[2]["user"]
        assert executor.calls == [(Action.CONTRACT_DETAIL, {"slug": "y"}, None)]
        assert result.steps[0].args == {"slug": "y"}
        assert result.steps[0].validation == "ok"

    async def test_validation_failure_degrades_the_step(self) -> None:
        err = "argument 'slug'='x' is not a known contract"
        client = _ScriptedClient(
            ['{"action": "contract_detail"}', '{"slug": "x"}', '{"slug": "x"}']
        )
        executor = _Executor()
        loop = _loop(client, _config(max_steps=1), _Validator(errors=[err, err]), executor)
        result = await loop.run("show the contract")
        assert result.degraded is True
        assert result.stop_reason == "validation_failed"
        assert result.degrade_reason == f"validation_failed: {err}"
        # The retry got the same (still invalid) args and nothing was executed.
        assert executor.calls == []
        assert result.steps[0].args == {"slug": "x"}
        assert result.steps[0].validation == err
        assert [c["stage"] for c in client.calls] == ["classify", "fill", "fill_retry"]
        # Degraded answers fall back to the raw transcript.
        assert result.answer == "(no steps were executed)"

    async def test_empty_fill_gets_plaintext_retry(self) -> None:
        client = _ScriptedClient(
            [
                '{"action": "code_search"}',
                _empty("fill"),
                '{"query": "q", "k": 3}',
                '{"action": "none"}',
                "done",
            ]
        )
        result = await _loop(client, _config(), _Validator(), _Executor()).run("q")
        assert result.degraded is False
        assert [c["stage"] for c in client.calls] == [
            "classify",
            "fill",
            "fill_plaintext",
            "classify",
            "answer",
        ]
        assert client.calls[2]["response_format"] is None

    async def test_empty_fill_both_modes_propagates_to_router(self) -> None:
        client = _ScriptedClient(
            ['{"action": "code_search"}', _empty("fill"), _empty("fill_plaintext")]
        )
        loop = _loop(client, _config(), _Validator(), _Executor())
        with pytest.raises(ClientStageError) as exc:
            await loop.run("q")
        assert "both schema and plain-text modes" in str(exc.value)


# ---------------------------------------------------------------------------
# Execution leg
# ---------------------------------------------------------------------------


class TestExecutionLeg:
    async def test_executor_error_recorded_and_loop_continues(self) -> None:
        err = "the code index is not available on this service"
        client = _ScriptedClient(
            [
                '{"action": "code_search"}',
                '{"query": "q", "k": 3}',
                '{"action": "none"}',
                "nothing found",
            ]
        )
        loop = _loop(client, _config(), _Validator(), _Executor(error=err))
        result = await loop.run("q")
        assert result.degraded is False
        assert result.answer == "nothing found"
        assert result.steps[0].result_chars == len(f"Execution unavailable: {err}")
        assert f"Execution unavailable: {err}" in client.calls[3]["user"]

    async def test_duplicate_step_stops_before_execution(self) -> None:
        client = _ScriptedClient(
            [
                '{"action": "code_search"}',
                '{"query": "q", "k": 3}',
                '{"action": "code_search"}',
                '{"query": "q", "k": 3}',
                "found it",
            ]
        )
        executor = _Executor()
        loop = _loop(client, _config(max_steps=2), _Validator(), executor)
        result = await loop.run("q")
        assert result.degraded is False
        assert result.stop_reason == "duplicate"
        assert len(result.steps) == 2
        assert len(executor.calls) == 1  # the duplicate never executed


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudget:
    async def test_step_budget_exhaustion_answers_without_degrade(self) -> None:
        client = _ScriptedClient(
            ['{"action": "code_search"}', '{"query": "q", "k": 3}', "found it"]
        )
        executor = _Executor()
        loop = _loop(client, _config(max_steps=1), _Validator(), executor)
        result = await loop.run("q")
        assert result.degraded is False
        assert result.stop_reason == "step_budget"
        assert len(result.steps) == 1
        assert len(executor.calls) == 1
        assert result.answer == "found it"


# ---------------------------------------------------------------------------
# Answer leg
# ---------------------------------------------------------------------------


class TestAnswerLeg:
    async def test_empty_answer_degrades_with_raw_transcript(self) -> None:
        client = _ScriptedClient(
            ['{"action": "code_search"}', '{"query": "q", "k": 3}', _empty("answer")]
        )
        executor = _Executor(text="1 result(s):")
        loop = _loop(client, _config(max_steps=1), _Validator(), executor)
        result = await loop.run("q")
        assert result.degraded is True
        assert result.degrade_reason == "answer_gen"
        assert result.stop_reason == "step_budget"
        # The fallback is the raw transcript, so the caller still sees the data.
        assert result.answer == "STEP 1: code_search(query='q', k=3)\n1 result(s):"
        # max_steps=1: the budget ends the step loop straight into the answer —
        # there is no second classify round.
        assert [c["stage"] for c in client.calls] == ["classify", "fill", "answer"]
