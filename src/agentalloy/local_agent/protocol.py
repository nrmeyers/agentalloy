"""The local agent's 10-way protocol surface.

Action enum, per-action JSON schemas (OpenAI strict form, so the same payload
drives llama-server's ``response_format: json_schema``), and the
classify / fill / answer prompt builders.

Ported from the ``~/dev/newagent`` spike (whose ``protocol.py`` is lost —
see ``docs/local-agent-design.md``). The spike's two prompt fixes are
encoded as data here:

* the detailed "when to choose ``none``" section (abstention vs sufficient
  context) — earned the abstention gate;
* per-action fill disambiguation hints (fix→build, ``artifact_body``
  slug-vs-query, telemetry numeric k) — earned argument_fill 1.000.

The prompts are the eval harness's input (``eval/local_agent/``): any edit to
them must re-run the full task suite — decision boundaries are non-monotonic
in prompt text, so a "safe" wording change can silently move other gates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

# ---------------------------------------------------------------------------
# Canonical vocabularies
# ---------------------------------------------------------------------------

# Lockstep with ``api.compose_models.Phase`` (kept as a tuple, not an import,
# the way ``install/mcp_server.MCP_PHASES`` does it — the local agent must not
# drag the compose models into its import graph).
PHASES: tuple[str, ...] = (
    "intake",
    "spec",
    "design",
    "plan",
    "build",
    "qa",
    "ship",
    "sdd-fast",
    "add-skill",
    "sdd-flow",
)

# The five typed entity edge kinds in the code index (overgraph_store.py).
ENTITY_KINDS: tuple[str, ...] = ("CONSTRAINTS", "TOUCHES", "REQUIRES", "COMMAND", "STAKEHOLDER")


class Action(StrEnum):
    """The 10-way classify surface (order = classify-prompt order)."""

    CODE_SEARCH = "code_search"
    SYMBOLS = "symbols"
    KNOWLEDGE_WHY = "knowledge_why"
    KNOWLEDGE_RELATED = "knowledge_related"
    KNOWLEDGE_ENTITIES = "knowledge_entities"
    ARTIFACT_BODY = "artifact_body"
    CONTRACT_DETAIL = "contract_detail"
    TELEMETRY = "telemetry"
    GET_SKILL_FOR = "get_skill_for"
    NONE = "none"


ACTIONS: tuple[Action, ...] = tuple(Action)

# Mirrors ``install/mcp_server._QUERY_ACTIONS`` (plus get_skill_for / none) so
# the local agent speaks the same language the injected guidance teaches the
# harness model to use.
ACTION_DESCRIPTIONS: dict[Action, str] = {
    Action.CODE_SEARCH: "Semantic code search across the indexed codebase.",
    Action.SYMBOLS: "Look up a symbol by fully-qualified name (function, class, etc.).",
    Action.KNOWLEDGE_WHY: "Read the design decision governing a specific symbol.",
    Action.KNOWLEDGE_RELATED: "Find decisions related to a topic query.",
    Action.KNOWLEDGE_ENTITIES: (
        "List typed entity edges (CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER) "
        "touching a symbol."
    ),
    Action.ARTIFACT_BODY: "Read the full body of a recorded phase artifact.",
    Action.CONTRACT_DETAIL: "Read the full detail of a contract by ID.",
    Action.TELEMETRY: "Recent composition traces with token savings data.",
    Action.GET_SKILL_FOR: "Fetch composed skill guidance for a coding task and phase.",
    Action.NONE: (
        "Stop — the question is not answerable by codebase intelligence, or the steps "
        "already executed retrieved enough context to answer."
    ),
}


# ---------------------------------------------------------------------------
# Per-action JSON schemas (OpenAI strict form)
# ---------------------------------------------------------------------------
#
# Strict-structured-outputs rules applied to every schema: every property is
# required, ``additionalProperties`` is false, and "optional" fields are typed
# ``[type, "null"]`` with an enum that includes null — the fill prompt tells
# the model to emit ``null`` when the question gives no value. No ``default``
# keys: defaults live in the fill prompt and in executor normalization.

_TYPE_LITERAL: dict[str, str] = {
    "string": '"<value>"',
    "integer": "<int>",
}


def _obj(props: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(props),
        "additionalProperties": False,
    }


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _nullable_enum(values: Sequence[str], description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "enum": [*values, None], "description": description}


_ACTION_SCHEMAS: dict[Action, dict[str, Any]] = {
    Action.CODE_SEARCH: _obj(
        {
            "query": _str("Short natural-language phrase about what the code does"),
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Results to return",
            },
        }
    ),
    Action.SYMBOLS: _obj({"query": _str("Fully-qualified symbol name")}),
    Action.KNOWLEDGE_WHY: _obj({"query": _str("Fully-qualified symbol name")}),
    Action.KNOWLEDGE_RELATED: _obj(
        {
            "query": _str("Short topic phrase (not a symbol name)"),
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Results to return",
            },
        }
    ),
    Action.KNOWLEDGE_ENTITIES: _obj(
        {
            "query": _str("Symbol FQN or short name"),
            "kind": _nullable_enum(
                ENTITY_KINDS, "Edge kind filter — null unless a specific kind is asked for"
            ),
        }
    ),
    Action.ARTIFACT_BODY: _obj(
        {
            "phase": _nullable_enum(PHASES, "Lifecycle phase scoping the lookup"),
            "slug": _str("Contract slug (the work item)"),
            "query": _str("Artifact name (a file name, e.g. design.md)"),
        }
    ),
    Action.CONTRACT_DETAIL: _obj({"slug": _str("Contract ID")}),
    Action.TELEMETRY: _obj(
        {
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Traces to return",
            },
            "phase": _nullable_enum(PHASES, "Phase filter — null unless a phase is named"),
        }
    ),
    Action.GET_SKILL_FOR: _obj(
        {
            "task": _str("One-sentence description of the coding task"),
            "phase": _nullable_enum(PHASES, "Lifecycle phase — null means the default 'build'"),
        }
    ),
    Action.NONE: _obj({}),
}


def action_schema(action: Action) -> dict[str, Any]:
    """Return the per-action argument schema (shared by prompt + validator)."""
    return _ACTION_SCHEMAS[action]


def _response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def classify_response_format() -> dict[str, Any]:
    """``response_format`` for the classify stage (single 10-way enum field)."""
    return _response_format(
        "classify",
        _obj({"action": {"type": "string", "enum": [a.value for a in ACTIONS]}}),
    )


def fill_response_format(action: Action) -> dict[str, Any]:
    """``response_format`` for the fill stage of ``action``."""
    return _response_format(f"fill_{action.value}", _ACTION_SCHEMAS[action])


def _skeleton(schema: dict[str, Any]) -> str:
    """A one-line JSON skeleton of the schema's keys, for the fill prompt."""
    props: dict[str, Any] = schema.get("properties", {})
    if not props:
        return "{}"
    inner = ", ".join(
        f'"{key}": '
        + ("null" if isinstance(spec.get("type"), list) else _TYPE_LITERAL[spec["type"]])
        for key, spec in props.items()
    )
    return "{ " + inner + " }"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = (
    "You are the action classifier for AgentAlloy's local codebase-intelligence agent. "
    "You pick exactly one action from the fixed list that would make progress toward "
    'answering the question, or "none". You never answer the question yourself. '
    "Reply with JSON only."
)

FILL_SYSTEM = (
    "You fill the JSON arguments of a chosen codebase-intelligence action. "
    "Reply with a JSON object only. Never answer the question, and never invent "
    "values the question or transcript does not contain."
)

ANSWER_SYSTEM = (
    "You are AgentAlloy's local codebase-intelligence answerer. Answer the question "
    "strictly from the transcript provided — cite the symbols, paths, decisions, "
    "contracts, or artifacts you actually retrieved. Never invent codebase facts. "
    "This agent is read-only: if the question asked for a code change, say what you "
    "found and that nothing was changed."
)


@dataclass(frozen=True)
class StagePrompt:
    """A (system, user) pair ready for ``OpenAICompatClient.chat``."""

    system: str
    user: str


def _phase_block(phase: str | None) -> str:
    return f"CURRENT PHASE\n{phase}\n\n" if phase else ""


def _transcript_block(transcript: str) -> str:
    return f"TRANSCRIPT OF STEPS ALREADY EXECUTED\n{transcript or '(none yet)'}"


def build_classify_prompt(
    question: str, transcript: str, *, phase: str | None = None
) -> StagePrompt:
    """Classify stage: the 10-way choice over question + transcript.

    The "WHEN TO CHOOSE none" section is load-bearing (spike iteration 2):
    abstention cases exhausted max_tokens thinking before the fix, and the
    DO-verb misfire weak spot is mitigated by naming edit requests explicitly.
    """
    catalog = "\n".join(f"- {a.value}: {ACTION_DESCRIPTIONS[a]}" for a in ACTIONS)
    valid = ", ".join(a.value for a in ACTIONS)
    user = (
        "CHOOSE ONE ACTION FOR THE QUESTION.\n\n"
        "ACTIONS:\n"
        f"{catalog}\n\n"
        'WHEN TO CHOOSE "none"\n'
        'Pick "none" — not a speculative query — in exactly two cases:\n'
        "1. ABSTENTION — the question is not a codebase-intelligence lookup:\n"
        '   - it asks to CHANGE, CREATE, or REMOVE code or files ("fix the bug", '
        '"add a feature", "refactor", "delete") — this agent is read-only; an '
        "investigative question about how something works is still a lookup, but a "
        "request to edit is not answerable here;\n"
        "   - it asks for an opinion, a prediction, or general software-engineering "
        "or world knowledge that no repository query can ground;\n"
        "   - it is about a project, system, or thing this repository does not contain.\n"
        "2. SUFFICIENT CONTEXT — the transcript below already retrieved what the "
        "answer needs. Never repeat an action that already ran with the same "
        'arguments, and do not run a second query "just in case" when the first '
        "returned relevant results.\n\n"
        'A wrong action wastes a step and returns "No results"; "none" ends the '
        "request with an honest answer. When the question is plausibly about this "
        "codebase, pick the action.\n\n"
        f"QUESTION\n{question}\n\n"
        f"{_phase_block(phase)}"
        f"{_transcript_block(transcript)}\n\n"
        f'Reply with JSON only: {{"action": "<one of: {valid}>"}}'
    )
    return StagePrompt(system=CLASSIFY_SYSTEM, user=user)


# Per-action fill disambiguation hints. The three in bold-ink come from the
# spike's second prompt iteration (argument_fill 0.933 → 1.000).
_FILL_HINTS: dict[Action, tuple[str, ...]] = {
    Action.CODE_SEARCH: (
        "query is a short natural-language phrase about what the code does; "
        "k is how many results you need (1-100) — default to 10.",
    ),
    Action.SYMBOLS: (
        "query is the fully-qualified name of ONE symbol (module.path.name). "
        "If the question names a symbol, copy it exactly.",
    ),
    Action.KNOWLEDGE_WHY: (
        "query is the fully-qualified name of the symbol whose governing design "
        "decision you want; copy it exactly from the question or transcript.",
    ),
    Action.KNOWLEDGE_RELATED: (
        "query is a short topic phrase (not a symbol name); default k to 8.",
    ),
    Action.KNOWLEDGE_ENTITIES: (
        "query is a symbol name (FQN or short name); kind is one of "
        f"{', '.join(ENTITY_KINDS)} — emit null unless the question asks for a "
        "specific kind.",
    ),
    Action.ARTIFACT_BODY: (
        "slug is the CONTRACT slug (the work item); query is the ARTIFACT NAME "
        "(a file name such as design.md or spec.md). Do not put the search phrase "
        "in slug, and do not put the slug in query. Emit null for phase unless the "
        "question names a phase.",
    ),
    Action.CONTRACT_DETAIL: (
        "slug is the contract ID exactly as it appears in the question or "
        "transcript; do not invent or abbreviate it.",
    ),
    Action.TELEMETRY: (
        "k is a plain integer (e.g. 10), never a string; phase is null unless the "
        "question names a phase.",
    ),
    Action.GET_SKILL_FOR: (
        "task is one sentence describing the coding task. phase is 'build' unless "
        "the question names a phase — use 'sdd-fast' ONLY for an explicit "
        "quick-fix or hotfix request.",
    ),
    Action.NONE: (),
}


def build_fill_prompt(
    action: Action,
    question: str,
    transcript: str,
    *,
    phase: str | None = None,
    validation_error: str | None = None,
) -> StagePrompt:
    """Fill stage: arguments for ``action`` under its JSON schema.

    ``validation_error`` is the spike's ``expected 'x'`` feedback from a failed
    index-grounded check — one retry carries it, then the request degrades.
    """
    if action is Action.NONE:
        raise ValueError("build_fill_prompt: 'none' has no arguments to fill")
    schema = _ACTION_SCHEMAS[action]
    props: dict[str, Any] = schema["properties"]
    fields = "\n".join(
        f"- {key} ({spec.get('type', '?')}): {spec.get('description', '')}"
        for key, spec in props.items()
    )
    hints = "\n".join(f"- {hint}" for hint in _FILL_HINTS[action])

    retry = ""
    if validation_error:
        retry = (
            "YOUR PREVIOUS ATTEMPT FAILED VALIDATION:\n"
            f"{validation_error}\n"
            "Fix the offending argument(s) and reply again.\n\n"
        )

    user = (
        f'Fill the arguments for action "{action.value}".\n\n'
        f"ACTION: {action.value} — {ACTION_DESCRIPTIONS[action]}\n"
        f"ARGUMENTS (a JSON object with exactly these fields):\n{fields}\n\n"
        f"HOW TO FILL THEM:\n{hints}\n\n"
        f"{retry}"
        f"QUESTION\n{question}\n\n"
        f"{_phase_block(phase)}"
        f"{_transcript_block(transcript)}\n\n"
        "RULES:\n"
        "- Copy symbol names and identifiers EXACTLY as they appear in the question "
        "or transcript; never shorten a fully-qualified name.\n"
        '- Where a field type includes "null", emit null when the question gives '
        "no value.\n"
        "- Integer fields are plain integers, never strings.\n\n"
        f"Reply with JSON only: {_skeleton(schema)}"
    )
    return StagePrompt(system=FILL_SYSTEM, user=user)


def build_answer_prompt(question: str, transcript: str, *, phase: str | None = None) -> StagePrompt:
    """Answer stage: plain (schema-free) completion over question + transcript."""
    user = (
        "ANSWER THE QUESTION USING ONLY THE TRANSCRIPT.\n\n"
        "- If the transcript retrieved relevant results, answer directly and cite "
        "what you found (symbol names, file paths, decision or contract IDs, "
        "artifact names).\n"
        '- If a step returned "No results" or the transcript is empty, say plainly '
        "that the codebase query found nothing relevant, and briefly state what was "
        "looked for. Do not pad with guesses.\n"
        "- If the question asked to change code: nothing was changed (this agent is "
        "read-only) — describe what you found instead.\n"
        "- Be concise: a few sentences to a short paragraph. No preamble.\n\n"
        f"QUESTION\n{question}\n\n"
        f"{_phase_block(phase)}"
        f"TRANSCRIPT\n{transcript or '(no steps were executed)'}"
    )
    return StagePrompt(system=ANSWER_SYSTEM, user=user)


# ---------------------------------------------------------------------------
# Transcript rendering + result digest (the 2.6B's context discipline)
# ---------------------------------------------------------------------------


def digest_result(result_text: str, cap: int) -> str:
    """Cap one executed result before it enters the transcript.

    The cap (``LOCAL_AGENT_RESULT_CAP_CHARS``) is what keeps multi-step requests
    inside the model window; the DSpark compressor handles latency, not budget.
    """
    if cap <= 0 or len(result_text) <= cap:
        return result_text
    cut = result_text[:cap].rstrip()
    return f"{cut}\n... [truncated {len(result_text) - cap} of {len(result_text)} chars]"


def render_transcript(steps: Sequence[tuple[str, Mapping[str, Any], str]]) -> str:
    """Render executed steps as the transcript block used in every prompt.

    Each step is ``(action, args, result)`` where ``result`` is the already
    capped result text (see :func:`digest_result`).
    """
    if not steps:
        return "(none yet)"
    blocks: list[str] = []
    for i, (action, args, result) in enumerate(steps, start=1):
        if args:
            arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            blocks.append(f"STEP {i}: {action}({arg_str})\n{result}")
        else:
            blocks.append(f"STEP {i}: {action}\n{result}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Stage-output parsers (deterministic reject path, not exceptions)
# ---------------------------------------------------------------------------


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """Parse the (first) JSON object in a stage completion, or None.

    Strict-mode completions are bare JSON; the plain-text fallback (a build
    without json_schema support) may wrap it in a code fence, so the fences are
    stripped before locating the outermost object.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data: dict[str, Any] | None = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_classify(content: str) -> Action | None:
    """Parse a classify completion into an :class:`Action`, or None if invalid.

    None (bad JSON, unknown action, wrong shape) is a schema failure — the loop
    applies its retry-once rule and then degrades; a fabricated action name
    never executes.
    """
    data = _extract_json_object(content)
    if not isinstance(data, dict):
        return None
    value = data.get("action")
    if not isinstance(value, str):
        return None
    try:
        return Action(value)
    except ValueError:
        return None


def _type_ok(value: Any, typ: str) -> bool:
    if typ == "string":
        return isinstance(value, str)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "null":
        return value is None
    return False


def _conforms(data: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """Shape-check fill output against the action schema (strict, no retry loop)."""
    props: dict[str, Any] = dict(schema.get("properties", {}))
    if not set(data) <= set(props):
        return False  # an invented key is a structural hallucination
    for key, spec in props.items():
        if key not in data:
            typ = spec.get("type")
            if not (isinstance(typ, list) and "null" in typ):
                return False  # a required field is missing
            continue
        value = data[key]
        typ = spec.get("type")
        if isinstance(typ, list):
            if not any(_type_ok(value, t) for t in cast(list[str], typ)):
                return False
        elif not _type_ok(value, typ):
            return False
        if "enum" in spec and value is not None and value not in spec["enum"]:
            return False
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                return False
            if "maximum" in spec and value > spec["maximum"]:
                return False
    return True


def parse_fill(action: Action, content: str) -> dict[str, Any] | None:
    """Parse a fill completion into an argument dict, or None if it does not
    conform to the action's schema.

    Missing *nullable* fields are normalized to ``None`` (the model often
    drops them in plain-text mode); missing required fields or invented keys
    are invalid.
    """
    data = _extract_json_object(content)
    if not isinstance(data, dict):
        return None
    if not _conforms(data, _ACTION_SCHEMAS[action]):
        return None
    normalized: dict[str, Any] = {}
    for key in _ACTION_SCHEMAS[action]["properties"]:
        if key not in data:
            normalized[key] = None
        else:
            normalized[key] = data[key]
    return normalized
