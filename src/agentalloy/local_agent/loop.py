"""The local-agent request loop (M1c) — classify → fill → validate → execute.

Pure state machine over an injected model client (testable — no global state,
no hidden config reads). Per request:

    transcript = []                       # capped step results only
    for step in 1..max_steps:
        classify(question, transcript)    # schema; retry once (same prompt)
            -> none                      -> answer
            -> action A
        fill(A, question, transcript)     # schema; retry once — carrying the
                                          # index-grounded validation error
                                          # when that is what failed
        validate(args)                    # index-grounded, fail-open
        execute(A, args)                  # in-process against the stores
        transcript += digest(result)      # cap per result, NOT the window
    answer(question, transcript)          # plain completion, no schema

The two failure postures are deliberately different:

* LM stage unavailable (down / 5xx / timeout — ``ClientStageError`` without
  ``empty_completion``) → **fail latch**: the error propagates to the router,
  which returns a structured 503 with the reason and keeps the process-wide
  cooldown tripped (v1 contract).
* LM stage answered but wrong (schema-invalid after one retry, index-grounded
  validation failing after one retry, empty answer) → **fail open**: the
  response carries the raw step results with ``degraded`` set — honest, never
  fabricated.

The documented ``json_schema``-unsupported signature (empty completion under
``response_format``) is handled inside :meth:`LocalAgentLoop._chat_stage`: one
plain-text retry of the stage, then the stage is treated as answered-with-no-
output (degrade) rather than endpoint-down — a capable-but-schema-blind build
must not latch the process off.

One deliberate deviation from the design sketch: the duplicate-step check runs
**before** execution, not after. Re-running an identical read query would only
burn the (slow) query to discover the step is a repeat; the stop is the same.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentalloy.local_agent.client import ClientStageError, OpenAICompatClient
from agentalloy.local_agent.config import LocalAgentConfig
from agentalloy.local_agent.executors import ExecutorError, Executors
from agentalloy.local_agent.protocol import (
    Action,
    StagePrompt,
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
from agentalloy.local_agent.validate import ValidationCheck, Validator

logger = logging.getLogger(__name__)

# stop_reason values (the response records which rule ended the loop)
STOP_NONE = "none"
STOP_STEP_BUDGET = "step_budget"
STOP_DUPLICATE = "duplicate"
STOP_CLASSIFY_FAILED = "classify_failed"
STOP_FILL_FAILED = "fill_failed"
STOP_VALIDATION_FAILED = "validation_failed"

# degrade_reason values (degraded=True implies one of these)
DEGRADE_CLASSIFY = "classify_failed"
DEGRADE_FILL = "fill_failed"
DEGRADE_VALIDATION = "validation_failed"
DEGRADE_ANSWER_GEN = "answer_gen"

# Fed back into the single fill retry when the first reply was not valid JSON.
_SCHEMA_FEEDBACK = (
    "Your reply was not a valid JSON object for this action's schema. "
    "Reply with the JSON object only."
)


@dataclass
class StepRecord:
    """One loop step, as reported in the response (inspectable trace)."""

    step: int
    action: str
    args: dict[str, Any]
    validation: str | None = None
    result_chars: int | None = None
    stage_latencies_ms: dict[str, int] = field(default_factory=dict[str, int])


@dataclass
class LoopResult:
    """The loop's terminal output — the router shapes this into the response."""

    answer: str
    steps: list[StepRecord]
    stop_reason: str
    degraded: bool
    degrade_reason: str | None
    model_tag: str
    total_ms: int


class LocalAgentLoop:
    """Runs one ``/local-agent/ask`` request against an injected LM client.

    The client is called through :func:`asyncio.to_thread` (it is a blocking
    httpx wrapper) so a multi-second local-model stage never blocks the
    service's event loop.
    """

    def __init__(
        self,
        client: OpenAICompatClient,
        *,
        config: LocalAgentConfig,
        validator: Validator,
        executors: Executors,
    ) -> None:
        self._client = client
        self._config = config
        self._validator = validator
        self._executors = executors
        self._started = 0.0

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def run(
        self,
        question: str,
        *,
        repo: str | None = None,
        phase: str | None = None,
    ) -> LoopResult:
        """Run the step loop for one question and produce the final answer.

        ``repo`` (the request's repo slug) scopes code-index execution;
        ``phase`` (validated by the router) is prompt context only.

        Raises :class:`ClientStageError` when an LM stage is unavailable —
        the router turns that into the structured 503.
        """
        self._started = time.perf_counter()
        max_steps = max(1, min(3, self._config.max_steps))
        transcript: list[tuple[str, Mapping[str, Any], str]] = []
        steps: list[StepRecord] = []
        executed: set[tuple[str, str]] = set()
        stop_reason = STOP_STEP_BUDGET

        for step_no in range(1, max_steps + 1):
            lat: dict[str, int] = {}
            transcript_str = render_transcript(transcript)

            # ---- classify ------------------------------------------------
            prompt = build_classify_prompt(question, transcript_str, phase=phase)
            content, ms = await self._chat_stage("classify", prompt, classify_response_format())
            lat["classify"] = ms
            action = parse_classify(content)
            if action is None:
                content, ms = await self._chat_stage(
                    "classify_retry", prompt, classify_response_format()
                )
                lat["classify_retry"] = ms
                action = parse_classify(content)
            if action is None:
                steps.append(
                    StepRecord(
                        step_no,
                        "classify",
                        {},
                        validation="schema-invalid classify output after one retry",
                        stage_latencies_ms=lat,
                    )
                )
                return self._result(
                    transcript,
                    steps,
                    STOP_CLASSIFY_FAILED,
                    degrade_reason=DEGRADE_CLASSIFY,
                )
            if action is Action.NONE:
                steps.append(StepRecord(step_no, "none", {}, stage_latencies_ms=lat))
                stop_reason = STOP_NONE
                break

            # ---- fill (one retry: schema-invalid or index-grounded) -------
            prompt = build_fill_prompt(action, question, transcript_str, phase=phase)
            content, ms = await self._chat_stage("fill", prompt, fill_response_format(action))
            lat["fill"] = ms
            args = parse_fill(action, content)
            check: ValidationCheck | None = None
            if args is not None:
                check = await self._validated(action, args, lat)
            if args is None or (check is not None and not check.ok):
                feedback = (
                    check.error
                    if (args is not None and check is not None and not check.ok)
                    else _SCHEMA_FEEDBACK
                )
                prompt = build_fill_prompt(
                    action, question, transcript_str, phase=phase, validation_error=feedback
                )
                content, ms = await self._chat_stage(
                    "fill_retry", prompt, fill_response_format(action)
                )
                lat["fill_retry"] = ms
                args2 = parse_fill(action, content)
                if args2 is not None:
                    check = await self._validated(action, args2, lat)
                    if check.ok:
                        args = args2
                else:
                    check = ValidationCheck(False, "fill retry returned schema-invalid output")
            if args is None or check is None or not check.ok:
                if args is None:
                    detail = (
                        check.error
                        if check is not None
                        else "fill stage produced no valid output after one retry"
                    )
                    steps.append(
                        StepRecord(
                            step_no, action.value, {}, validation=detail, stage_latencies_ms=lat
                        )
                    )
                    return self._result(
                        transcript, steps, STOP_FILL_FAILED, degrade_reason=DEGRADE_FILL
                    )
                detail = (
                    check.error
                    if check is not None
                    else "index-grounded validation failed after one retry"
                )
                steps.append(
                    StepRecord(
                        step_no, action.value, args, validation=detail, stage_latencies_ms=lat
                    )
                )
                return self._result(
                    transcript,
                    steps,
                    STOP_VALIDATION_FAILED,
                    degrade_reason=f"{DEGRADE_VALIDATION}: {detail}",
                )

            # ---- duplicate check (before execution — see module docstring) --
            key = (action.value, json.dumps(args, sort_keys=True))
            if key in executed:
                steps.append(StepRecord(step_no, action.value, args, stage_latencies_ms=lat))
                stop_reason = STOP_DUPLICATE
                break
            executed.add(key)

            # ---- execute ---------------------------------------------------
            started = time.perf_counter()
            try:
                result = await self._executors.execute(action, args, repo_slug=repo)
            except ExecutorError as exc:
                # Honest transcript line — the next classify sees the gap.
                result = f"Execution unavailable: {exc}"
            lat["execute"] = int((time.perf_counter() - started) * 1000)

            capped = digest_result(result, self._config.result_cap_chars)
            steps.append(
                StepRecord(
                    step_no,
                    action.value,
                    args,
                    "ok",
                    len(capped),
                    lat,
                )
            )
            transcript.append((action.value, args, capped))

        # ---- answer (plain completion; fail open into the raw results) ----
        try:
            content, ms = await self._chat_stage(
                "answer",
                build_answer_prompt(question, render_transcript(transcript), phase=phase),
                None,
            )
            answer = content.strip()
            if not answer:
                raise ClientStageError(
                    "answer", "empty completion content", ms, empty_completion=True
                )
        except ClientStageError as exc:
            logger.warning("local agent answer stage degraded: %s", exc)
            return self._result(
                transcript,
                steps,
                stop_reason,
                degrade_reason=DEGRADE_ANSWER_GEN,
            )
        return self._result(transcript, steps, stop_reason, answer=answer)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _result(
        self,
        transcript: list[tuple[str, Mapping[str, Any], str]],
        steps: list[StepRecord],
        stop_reason: str,
        *,
        answer: str | None = None,
        degrade_reason: str | None = None,
    ) -> LoopResult:
        return LoopResult(
            answer=answer if answer is not None else self._fallback_answer(transcript),
            steps=steps,
            stop_reason=stop_reason,
            degraded=degrade_reason is not None,
            degrade_reason=degrade_reason,
            model_tag=self._config.model,
            total_ms=self._elapsed_ms(),
        )

    @staticmethod
    def _fallback_answer(transcript: list[tuple[str, Mapping[str, Any], str]]) -> str:
        """Deterministic degraded answer: the raw step results, never invented."""
        text = render_transcript(transcript)
        return text if text and text != "(none yet)" else "(no steps were executed)"

    async def _validated(
        self, action: Action, args: dict[str, Any], lat: dict[str, int]
    ) -> ValidationCheck:
        started = time.perf_counter()
        check = await self._validator.validate(action, args)
        lat["validate"] = int((time.perf_counter() - started) * 1000)
        return check

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)

    async def _chat_stage(
        self,
        stage: str,
        prompt: StagePrompt,
        response_format: Mapping[str, Any] | None,
    ) -> tuple[str, int]:
        """One stage completion in a worker thread; returns ``(content, ms)``.

        Raises :class:`ClientStageError` for unavailable/timeout/5xx (the
        router's 503 path). An empty completion under ``response_format`` —
        the documented ``json_schema``-unsupported signature — gets the
        design's one plain-text retry of the stage instead of latching the
        endpoint; if that too comes back empty the stage is reported empty.
        """
        fmt: dict[str, Any] | None = dict(response_format) if response_format is not None else None
        try:
            return await asyncio.to_thread(
                self._client.chat,
                prompt,
                stage=stage,
                max_tokens=self._config.max_tokens,
                response_format=fmt,
            )
        except ClientStageError as exc:
            if not exc.empty_completion or fmt is None:
                raise
            logger.warning(
                "local agent %s stage: empty completion under json_schema; "
                "retrying the stage in plain text",
                stage,
            )
            try:
                return await asyncio.to_thread(
                    self._client.chat,
                    prompt,
                    stage=f"{stage}_plaintext",
                    max_tokens=self._config.max_tokens,
                    response_format=None,
                )
            except ClientStageError as exc2:
                if exc2.empty_completion:
                    raise ClientStageError(
                        stage,
                        "empty completion in both schema and plain-text modes "
                        "(the serving build may not support response_format json_schema)",
                        None,
                    ) from exc2
                raise
