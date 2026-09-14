"""The 10-way classification surface and constrained-fill schemas.

Grounded in ``agentalloy.install.mcp_server``: the 8 ``agentalloy_query``
actions plus ``get_skill_for`` (phase enum) give exactly 10 classes. The
argument schemas mirror the tool inputSchema in that module (phase enum,
k bounds, required fields).
"""

from __future__ import annotations

import json

ACTIONS = [
    "code_search",
    "symbols",
    "knowledge_why",
    "knowledge_related",
    "knowledge_entities",
    "artifact_body",
    "contract_detail",
    "telemetry",
    "get_skill_for",
    "none",
]

# Phase vocabulary from api.compose_models.Phase (MCP_PHASES minus intake).
PHASES = [
    "spec",
    "design",
    "plan",
    "build",
    "qa",
    "ship",
    "sdd-fast",
    "add-skill",
    "sdd-flow",
]

# Entity edge kinds (knowledge_entities).
KINDS = ["CONSTRAINTS", "TOUCHES", "REQUIRES", "COMMAND", "STAKEHOLDER"]

ACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ACTIONS}},
    "required": ["action"],
    "additionalProperties": False,
}


def _obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_K = {"type": "integer", "minimum": 1, "maximum": 50}

ARG_SCHEMAS: dict[str, dict] = {
    "code_search": _obj({"query": {"type": "string", "minLength": 1}, "k": _K}, ["query"]),
    "symbols": _obj({"query": {"type": "string", "minLength": 1}}, ["query"]),
    "knowledge_why": _obj({"query": {"type": "string", "minLength": 1}}, ["query"]),
    "knowledge_related": _obj({"query": {"type": "string", "minLength": 1}, "k": _K}, ["query"]),
    "knowledge_entities": _obj(
        {
            "query": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": KINDS},
        },
        ["query"],
    ),
    "artifact_body": _obj(
        {
            "phase": {"type": "string", "enum": PHASES},
            "slug": {"type": "string", "minLength": 1},
            "query": {"type": "string", "minLength": 1},
        },
        ["phase", "slug", "query"],
    ),
    "contract_detail": _obj({"slug": {"type": "string", "minLength": 1}}, ["slug"]),
    "telemetry": _obj(
        {
            "k": {"type": "integer", "minimum": 1, "maximum": 100},
            "phase": {"type": "string", "enum": PHASES},
        },
        [],
    ),
    "get_skill_for": _obj(
        {
            "task": {"type": "string", "minLength": 1},
            "phase": {"type": "string", "enum": PHASES},
        },
        ["task"],
    ),
    "none": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}

SYSTEM_PROMPT = """\
You are the tool router for the AgentAlloy agent system. For each user request, \
choose exactly ONE action. Only the 9 lookup actions below are supported. \
If the request is not one of them, choose "none".

- code_search: semantic code search across the indexed codebase. Args: query (search text); optional k (number of results, default 10).
- symbols: look up a symbol by fully-qualified name. Args: query (FQN like agentalloy.retrieval.rerank._FailureLatch).
- knowledge_why: read the design decision governing a specific symbol. Args: query (FQN).
- knowledge_related: find design decisions related to a topic. Args: query (topic text); optional k (default 10).
- knowledge_entities: list typed entity edges touching a symbol. Args: query (FQN or short name); optional kind (CONSTRAINTS|TOUCHES|REQUIRES|COMMAND|STAKEHOLDER).
- artifact_body: read the full body of a recorded phase artifact. Args: phase (spec|design|plan|build|qa|ship|sdd-fast|add-skill|sdd-flow), slug (the CONTRACT name it was recorded under, like sdd-build), query (the ARTIFACT name within that contract, like spec.artifact or test-plan).
- contract_detail: read the full detail of a contract. Args: slug (contract slug like sdd-build).
- telemetry: recent composition traces with token savings. Optional args: k (number of traces to show, e.g. "last 5" -> 5), phase (filter).
- get_skill_for: get the skill pack for a coding task and lifecycle phase. Args: task (one-sentence task description); optional phase (spec|design|plan|build|qa|ship|sdd-fast).
- none: the request is not one of the supported lookups above.

When to choose "none":
- The request asks you to DO something (run, write, create, delete, restart, \
refactor, fix, summarize, convert, compute, send) rather than look something up.
- The request is general knowledge (weather, definitions, math) or chit-chat.
- The request asks about the current session state (for example which phase the agent is in).
Choosing "none" is correct even when the request mentions codebase words like \
modules, phases, tests, or papers, as long as it asks to act, not to look up.

For get_skill_for, set phase from the verb that describes the work: \
scope/spec -> spec, design -> design, plan -> plan, implement/build/fix -> build, \
verify/QA -> qa, ship -> ship; or from a named phase. \
Use sdd-fast ONLY when the request explicitly says "quick fix" or "hotfix". \
If no phase is named or implied, omit phase.

Examples:
- "Run the linter on this file." -> {"action": "none"}
- "What is 17 in hexadecimal?" -> {"action": "none"}
- "Write a commit message for this change." -> {"action": "none"}
- "Find the code that parses the MCP config." -> {"action": "code_search"}
- "Get the skill pack for verifying the login flow in QA." -> {"action": "get_skill_for"}

Respond with a single JSON object only. No prose.
"""

# Per-action guidance appended to the stage-B fill message.
FILL_HINTS: dict[str, str] = {
    "get_skill_for": (
        "Set 'phase' when the request names a lifecycle phase or the verb that "
        "describes the work maps to one (scope/spec -> spec, design -> design, "
        "plan -> plan, implement/build/fix -> build, verify/QA -> qa, ship -> "
        "ship). Use sdd-fast only for an explicit 'quick fix'/'hotfix' request. "
        "Otherwise omit 'phase'."
    ),
    "artifact_body": (
        "slug is the contract name the artifact was recorded under (e.g. "
        "sdd-plan-and-contracts); query is the artifact name within it (e.g. "
        "tasks, qa, spec.artifact). Do not swap them."
    ),
    "telemetry": (
        "Set 'k' when the request names a count ('last 5' -> k=5); otherwise "
        "omit 'k'. Set 'phase' only when the request names a phase filter."
    ),
    "knowledge_entities": (
        "Set 'kind' ONLY if the request names one of the edge kinds in uppercase "
        "(CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER). Otherwise omit 'kind'."
    ),
}

_ACTION_ENUM = ", ".join(ACTIONS)


def stage_a_messages(request: str) -> list[dict]:
    """Step 1: classify the request into one of the 10 actions."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": request},
    ]


def stage_a_retry_messages(msgs: list[dict], raw: str) -> list[dict]:
    out = list(msgs)
    out.append({"role": "assistant", "content": raw})
    out.append(
        {
            "role": "user",
            "content": (
                f"Invalid. Reply with exactly one JSON object: "
                f'{{"action": "<one of: {_ACTION_ENUM}>"}}'
            ),
        }
    )
    return out


def stage_b_messages(msgs: list[dict], raw_action: str, action: str) -> list[dict]:
    """Step 2: fill the arguments for the classified action."""
    out = list(msgs)
    out.append({"role": "assistant", "content": raw_action})
    content = (
        f"Action: {action}. Reply with the arguments for '{action}' "
        "as one JSON object. No prose."
    )
    if action in FILL_HINTS:
        content += f" {FILL_HINTS[action]}"
    out.append({"role": "user", "content": content})
    return out


def stage_b_retry_messages(msgs: list[dict], raw: str, action: str, err: str) -> list[dict]:
    out = list(msgs)
    out.append({"role": "assistant", "content": raw})
    out.append(
        {
            "role": "user",
            "content": (
                f"Arguments failed validation: {err} "
                f"Reply with the corrected arguments for '{action}' as one JSON object. No prose."
            ),
        }
    )
    return out


def loop_step_messages(task: str, prev_call_json: str, observation: str) -> list[dict]:
    """Classify the NEXT action of a multi-step request, given the previous
    tool call and its observation."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
        {"role": "assistant", "content": prev_call_json},
        {
            "role": "user",
            "content": (
                f"Observation:\n{observation}\n\n"
                f"Continue. Reply with the next action as one JSON object: "
                f'{{"action": "<one of: {_ACTION_ENUM}>"}} No prose.'
            ),
        },
    ]


def parse_json(raw: str) -> dict | None:
    """Best-effort parse of a model JSON response (handles stray prose)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            return val if isinstance(val, dict) else None
        except json.JSONDecodeError:
            return None
    return None
