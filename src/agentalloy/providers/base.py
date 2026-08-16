"""Provider registry base types.

Defines the core data structures used by the provider registry:
- Capability: the integration mode a harness supports
- Protocol: the LLM protocol the harness speaks
- HarnessSpec: the full specification for a single harness
- WireRecord: a single file-write action performed by an install writer

These are frozen dataclasses / enums so registry entries are immutable
once registered.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from agentalloy.api.proxy_context import Upstream


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------


class Capability(Enum):
    """Integration capability for a harness.

    Exactly three values:
    - PROXY:     the harness uses the AgentAlloy proxy path (base-url rewrite)
    - MARKDOWN_ONLY: the harness only gets markdown injection (no tool-use)
    - MCP_ONLY:  the harness uses the MCP fallback path only
    """

    PROXY = "proxy"
    MARKDOWN_ONLY = "markdown_only"
    MCP_ONLY = "mcp_only"


# ---------------------------------------------------------------------------
# Protocol enum
# ---------------------------------------------------------------------------


class Protocol(Enum):
    """LLM protocol the harness speaks.

    Exactly three values:
    - ANTHROPIC: the harness speaks the Anthropic Messages API
    - OPENAI:    the harness speaks the OpenAI Chat Completions API
    - EITHER:    the harness can work with either protocol
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    EITHER = "either"


# ---------------------------------------------------------------------------
# HarnessSpec dataclass
# ---------------------------------------------------------------------------

HarnessSpecEnvBuilder = Callable[[int], dict[str, str]]
"""Build an environment dict for a child process given a port number."""

HarnessSpecInstallWriter = Callable[[int, "Path", bool], "list[WireRecord]"]
"""Run the full install/wire for a harness, returning the records of files touched."""

HarnessSpecUpstreamExtractor = Callable[["Path"], "Upstream | None"]
"""Read a harness's own config and return the upstream LLM it points at, if any.

Used by ``agentalloy add <harness>`` so the proxy can *adopt* the harness's
existing upstream (forwarding there transparently) instead of making the user
re-declare it. Returns None when no upstream can be determined."""


@dataclass(frozen=True)
class HarnessSpec:
    """Immutable specification for a single harness.

    Fields:
        name:                lowercase registry key (e.g. ``"claude-code"``).
        binary:              name of the executable to spawn (e.g. ``"claude"``).
        capabilities:        tuple of ``Capability`` values this harness supports.
        protocol:            the LLM protocol the harness speaks.
        env_builder:         callable that returns env vars for a given port.
        install_writer:      callable that runs full wiring; returns WireRecords.
        upstream_extractor:  callable that reads the harness's own config and
                             returns its upstream LLM, for ``agentalloy add``.
    """

    name: str
    binary: str
    capabilities: tuple[Capability, ...]
    protocol: Protocol
    env_builder: HarnessSpecEnvBuilder
    install_writer: HarnessSpecInstallWriter | None = None
    # HTTP header this harness sends carrying a stable per-session id, if any
    # (e.g. claude-code → ``x-claude-code-session-id``). The proxy probes the
    # union of these across the registry to key per-session orientation; None
    # means this harness sends no session header and the proxy falls back to a
    # conversation fingerprint. See ``agentalloy.api.proxy_session``.
    session_header: str | None = None
    # Reads the harness's own config to recover the upstream LLM it points at,
    # so ``agentalloy add <harness>`` can adopt it without the user re-declaring
    # it. None means this harness exposes no discoverable upstream.
    upstream_extractor: HarnessSpecUpstreamExtractor | None = None


# ---------------------------------------------------------------------------
# WireRecord dataclass
# ---------------------------------------------------------------------------

_VALID_ACTIONS = frozenset({"wrote_new_file", "injected_block", "replaced_file", "env_export"})


@dataclass(frozen=True)
class WireRecord:
    """A single file-write action performed by an install/wire writer.

    Fields:
        path:                absolute path to the file written or modified.
        action:              one of ``"wrote_new_file"``, ``"injected_block"``,
                             ``"replaced_file"``, or ``"env_export"``.
        content_sha256:      SHA-256 hex digest of the *written* content.
        original_content:    the file's content before this writer ran (may be
                             ``None`` if the file did not exist).
        marker_key:          a human-readable key used by uninstall to locate
                             this record (e.g. a sentinel marker name).

    Serializes to the same dict shape as the old ``list[dict[str, Any]]``
    return from ``wire_harness.py``.
    """

    path: str
    action: str
    content_sha256: str
    original_content: str | None = None
    marker_key: str = ""
    # Optional sentinel overrides: uninstall's record walk strips by THESE
    # markers instead of the default install sentinels (used by the sidecar
    # writers, whose blocks carry the watcher's AGENTALLOY-CONTEXT markers).
    sentinel_begin: str = ""
    sentinel_end: str = ""

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"WireRecord.action must be one of {_VALID_ACTIONS}; got {self.action!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the legacy dict shape expected by callers."""
        d: dict[str, Any] = {
            "path": self.path,
            "action": self.action,
            "content_sha256": self.content_sha256,
        }
        if self.original_content is not None:
            d["original_content"] = self.original_content
        if self.marker_key:
            d["marker_key"] = self.marker_key
        if self.sentinel_begin:
            d["sentinel_begin"] = self.sentinel_begin
        if self.sentinel_end:
            d["sentinel_end"] = self.sentinel_end
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> WireRecord:
        """Reconstruct a WireRecord from a legacy dict."""
        return WireRecord(
            path=d["path"],
            action=d["action"],
            content_sha256=d["content_sha256"],
            original_content=d.get("original_content"),
            marker_key=d.get("marker_key", ""),
            sentinel_begin=d.get("sentinel_begin", ""),
            sentinel_end=d.get("sentinel_end", ""),
        )

    @staticmethod
    def _compute_sha256(content: str) -> str:
        """Compute SHA-256 hex digest of *content*."""
        return hashlib.sha256(content.encode()).hexdigest()


def sdd_instructions_markdown(port: int) -> str:
    """Shared SDD instruction block for markdown-delivered harnesses.

    One source of truth for the harnesses that wire via an instruction file
    (cursor, windsurf, github-copilot, antigravity, aider, hermes-agent), so the
    session-start / intake-front-door behaviour can't drift between them — the
    same drift that bit the ``Phases:`` line. Proxy-wired harnesses
    (claude-code and the OpenAI-compatible ones) get it via per-turn
    workflow-skill injection instead.

    hermes-agent is dual: its persistent ``SOUL.md`` block (this prose) is the
    global/unwired fallback, while ``agentalloy add hermes-agent`` makes it a
    per-repo proxy-wired harness that also gets the per-turn injection.

    Callers that need YAML frontmatter (cursor, windsurf) prepend their own;
    this returns just the body.
    """
    return (
        "## AgentAlloy — skill context\n\n"
        f"A local agentalloy service runs at `http://localhost:{port}` with a curated\n"
        "corpus of engineering skills.\n\n"
        f"**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`.\n"
        "If unreachable, ignore this block.\n\n"
        "**Open every session with intake.** Check the current phase (via\n"
        "`agentalloy phase` or proxy metadata):\n"
        "- If it names a phase other than `intake`, work is in progress — tell the user where\n"
        "  they left off (the phase + the active contract) and ask whether to resume there or\n"
        "  start something new. Resume → continue in that phase.\n"
        "  New → `agentalloy phase set intake`, then run intake.\n"
        "- Otherwise run **intake**: a brief intent interview, decide **full SDD vs the fast\n"
        "  lane**, and write a contract — which hands off to the chosen route.\n\n"
        "**When in an SDD phase, before starting work, compose skill context:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (`sdd-fast` = the fast lane).\n\n"
        "## code-index and knowledge-graph (conditional)\n\n"
        "When the local code-index service is running, a\n"
        "`<!-- BEGIN agentalloy code-index -->` block is injected into your\n"
        "harness config files.  It tells you to prefer the service for intent-\n"
        "based code search, call-graph tracing, and budgeted context assembly\n"
        "over grepping or reading files one at a time.\n\n"
        "The knowledge-graph CLI (`agentalloy knowledge why <fqn>` and\n"
        '`agentalloy knowledge related "<query>"`) rides on top of the code-\n'
        "index graph to surface design decisions behind symbols.  It is only\n"
        "useful when the code-index service is running.\n"
    )


# ---------------------------------------------------------------------------
# Enforcement posture (D1–D9)
# ---------------------------------------------------------------------------

# Phases that get deny rules on source/test writes.  Explicit allow-list —
# not "deny unless build" — so QA (which edits src/tests) and unknown phases
# stay open.  An unknown/unreadable phase fails open (writes nothing).
DENIED_PHASES: frozenset[str] = frozenset({"intake", "spec", "design", "sdd-flow"})

# Patterns denied by claude-code during pre-build phases.
DENY_PATTERNS: tuple[str, ...] = (
    "Write(src/**)",
    "Edit(src/**)",
    "Write(tests/**)",
    "Edit(tests/**)",
)

# Codex writable_roots during pre-build phases (docs stay writable).
CODEX_ALLOWED_ROOTS: tuple[str, ...] = ("docs/", ".agentalloy/")

# Harnesses that support Tier A enforcement (harness-level permission config).
TIER_A_HARNESSES: frozenset[str] = frozenset({"claude-code", "codex"})

# Per-phase artifact labels for denial messages.
_PHASE_ARTIFACT: dict[str, str] = {
    "intake": "a contract",
    "spec": "docs/spec/<slug>.md",
    "design": "docs/design/<slug>/{approach,tasks,test-plan}.md",
    "sdd-flow": "clarity (not code)",
}

# ---------------------------------------------------------------------------
# Proxy-level tool gating (harness-agnostic)
# ---------------------------------------------------------------------------

# Tool names that produce writes to src/ or tests/.  Checked against the
# Anthropic ``name`` field and the OpenAI ``function.name`` field.  During
# denied phases these tools are stripped from the upstream request so the
# LLM cannot call them regardless of the harness's own permission config.
GATED_TOOL_NAMES: frozenset[str] = frozenset({"write_file", "edit", "notebook_edit"})


def _tool_name(tool: dict[str, Any]) -> str | None:
    """Extract the tool name from an Anthropic or OpenAI tool definition."""
    # Anthropic format: {"name": "write_file", "description": ..., "input_schema": ...}
    if "name" in tool and "function" not in tool:
        return tool.get("name")
    # OpenAI format: {"type": "function", "function": {"name": "write_file", ...}}
    fn = tool.get("function")
    if not isinstance(fn, dict):
        # Built-in tools (e.g. web_search) carry neither name nor function.
        return None
    fn = cast(dict[str, Any], fn)
    return cast(str | None, fn.get("name"))


def filter_tools_for_phase(
    tools: list[dict[str, Any]],
    phase: str,
    *,
    pause_mode: bool = False,
) -> list[dict[str, Any]]:
    """Return *tools* with code-writing tools removed during denied phases.

    During ``intake``, ``spec``, and ``design`` the LLM is stripped of
    ``write_file``, ``edit``, and ``notebook_edit`` so it cannot produce
    source or test code.  All other tools (read_file, glob, grep_search,
    shell, ask_user_question, etc.) remain available.

    When *pause_mode* is ``True`` (repo is in pause mode), write gating
    is skipped entirely so the LLM retains all tool access regardless of
    phase.

    Handles both Anthropic tool format (``name`` at top level) and OpenAI
    format (``function.name``).
    """
    if pause_mode:
        return tools
    if phase not in DENIED_PHASES:
        return tools
    return [t for t in tools if _tool_name(t) not in GATED_TOOL_NAMES]


def is_tier_a_enforced(harness: str) -> bool:
    """Return True if *harness* supports Tier A (harness-level deny rules)."""
    return harness in TIER_A_HARNESSES


def build_claude_code_permissions(
    phase: str,
    *,
    pause_mode: bool = False,
) -> dict[str, object]:
    """Build the ``permissions`` block for ``.claude/settings.local.json``.

    During denied phases returns ``{"deny": [...patterns]}``; during all other
    phases returns ``{"deny": []}`` (posture present but unlocked).

    When *pause_mode* is ``True`` (repo is in pause mode), deny rules are
    skipped entirely so the LLM retains full write access regardless of phase.
    """
    if pause_mode or phase not in DENIED_PHASES:
        return {"deny": []}
    return {"deny": list(DENY_PATTERNS)}


def build_codex_workspace_write(
    phase: str,
    *,
    pause_mode: bool = False,
) -> dict[str, object]:
    """Build the ``workspace-write`` block for codex's ``config.toml``.

    During denied phases narrows ``writable_roots`` to docs/.agentalloy;
    during all other phases returns an empty dict (no restriction).

    When *pause_mode* is ``True`` (repo is in pause mode), writable
    roots are unrestricted regardless of phase.
    """
    if pause_mode or phase not in DENIED_PHASES:
        return {}
    return {"writable_roots": list(CODEX_ALLOWED_ROOTS)}


def build_denial_message(phase: str, owed_artifacts: list[str] | None = None) -> str:
    """Build the denial message shown when a write is blocked.

    Names the current phase and the artifact that phase owes.
    """
    artifact = _PHASE_ARTIFACT.get(phase, "the phase's deliverable")
    if owed_artifacts:
        artifact = ", ".join(owed_artifacts)
    return f"[AgentAlloy] Write denied in phase `{phase}` — complete {artifact} before advancing."


def build_instructive_denial_message(
    phase: str,
    tool_name: str,
    owed_artifacts: list[str] | None = None,
) -> str:
    """Build an instructive denial message that tells the LLM what to do instead.

    Includes:
    1. Why it was blocked (current phase)
    2. What it should be doing (the deliverable)
    3. How to advance (gates + approval)
    """
    artifact = _PHASE_ARTIFACT.get(phase, "the phase's deliverable")
    if owed_artifacts:
        artifact = ", ".join(owed_artifacts)

    # Determine next phase and approval requirement
    next_phase = _next_phase_label(phase)
    approval_required = phase in ("spec", "design", "plan", "add-skill")

    message_parts = [
        f"[AgentAlloy] Tool `{tool_name}` denied in phase `{phase}`.",
        "",
        f"You are in the {phase} phase. Writes to src/ and tests/ are not allowed until you advance to the build phase.",
        "",
        f"Your current deliverable: {artifact}",
        "",
    ]

    if approval_required:
        message_parts.extend([
            f"To advance from {phase}, you must complete the deliverable, then run `agentalloy approve {phase}` to get human sign-off before advancing.",
            "",
            f"Next: {next_phase}",
        ])
    else:
        message_parts.extend([
            "To advance, complete the deliverable and the phase will auto-advance.",
            "",
            f"Next: {next_phase}",
        ])

    message_parts.extend([
        "",
        f"Focus on completing the {phase} artifacts. Do not attempt to write code.",
    ])

    return "\n".join(message_parts)


def intercept_gated_tool_calls(
    content_blocks: list[dict[str, Any]],
    phase: str,
    *,
    pause_mode: bool = False,
    owed_artifacts: list[str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Scan content blocks for gated tool_use and replace with denial messages.

    Returns (modified_blocks, was_modified). If a tool_use block is for a gated
    tool in a denied phase, it's replaced with a text block containing an
    instructive denial message.

    This is called on the RESPONSE from upstream, before forwarding to the harness.
    """
    if pause_mode or phase not in DENIED_PHASES:
        return content_blocks, False

    modified = False
    new_blocks: list[dict[str, Any]] = []

    for block in content_blocks:
        if block.get("type") == "tool_use":
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})

            # Check if it's a direct write tool
            if tool_name in GATED_TOOL_NAMES:
                # Replace with denial message
                denial = build_instructive_denial_message(
                    phase, tool_name, owed_artifacts
                )
                new_blocks.append({
                    "type": "text",
                    "text": denial,
                })
                modified = True
                continue

            # Check if it's a shell command that writes to src/ or tests/
            if tool_name in SHELL_TOOL_NAMES and isinstance(tool_input, dict):
                command = tool_input.get("command", "")
                if _command_writes_to_code(command):
                    denial = build_instructive_denial_message(
                        phase, tool_name, owed_artifacts
                    )
                    new_blocks.append({
                        "type": "text",
                        "text": denial,
                    })
                    modified = True
                    continue

                # Check if it's a phase advance without approval
                if _command_advances_phase_without_approval(command, phase):
                    denial = (
                        f"[AgentAlloy] Phase advance denied in phase `{phase}`.\n\n"
                        f"You are in the {phase} phase, which requires explicit human approval before advancing.\n\n"
                        f"To advance, you must:\n"
                        f"1. Complete all deliverables for the {phase} phase\n"
                        f"2. Run `agentalloy approve {phase}` to get human sign-off\n\n"
                        f"Do not attempt to advance the phase without approval."
                    )
                    new_blocks.append({
                        "type": "text",
                        "text": denial,
                    })
                    modified = True
                    continue

        new_blocks.append(block)

    return new_blocks, modified


# Shell tool names that can execute arbitrary commands
SHELL_TOOL_NAMES: frozenset[str] = frozenset({
    "run_shell_command", "shell", "bash", "terminal", "execute_command"
})

# Patterns that indicate writing to files
_SHELL_WRITE_PATTERNS: tuple[str, ...] = (
    r">\s*src/",
    r">\s*tests/",
    r">>\s*src/",
    r">>\s*tests/",
    r"tee\s+src/",
    r"tee\s+tests/",
    r"cat\s+.*>\s*src/",
    r"cat\s+.*>\s*tests/",
    r"echo\s+.*>\s*src/",
    r"echo\s+.*>\s*tests/",
    r"cp\s+.*\s+src/",
    r"cp\s+.*\s+tests/",
    r"mv\s+.*\s+src/",
    r"mv\s+.*\s+tests/",
)


def _command_writes_to_code(command: str) -> bool:
    """Check if a shell command writes to src/ or tests/ directories."""
    import re
    return any(re.search(pattern, command) for pattern in _SHELL_WRITE_PATTERNS)


# Phases that require explicit approval before advancing
_APPROVAL_REQUIRED_PHASES: frozenset[str] = frozenset({"spec", "design", "plan", "add-skill"})


def _command_advances_phase_without_approval(command: str, current_phase: str) -> bool:
    """Check if a shell command tries to advance phase without approval.

    Returns True if the command tries to run `agentalloy phase set <next_phase>`
    from a phase that requires approval.
    """
    import re
    # Match: agentalloy phase set <phase>
    pattern = r"agentalloy\s+phase\s+set\s+(\S+)"
    match = re.search(pattern, command)
    # If current phase requires approval, block the advance
    return bool(match) and current_phase in _APPROVAL_REQUIRED_PHASES


def end_session_instruction(phase: str) -> str:
    """Build the end-session instruction for a phase advance.

    Identical string surfaced by CLI, web, and proxy responses.
    """
    next_phase = _next_phase_label(phase)
    return (
        f"Phase advanced to `{phase}`. "
        f"End this session and restart the harness so the new posture takes effect. "
        f"Next: {next_phase}."
    )


def _next_phase_label(phase: str) -> str:
    """Human label for what to do after entering *phase*."""
    labels: dict[str, str] = {
        "intake": "run intake to capture the request",
        "spec": "write the spec document",
        "design": "produce the design artifacts",
        "plan": "decompose the design into tasks, test cases, and build contracts",
        "build": "work the design's tasks with tests",
        "qa": "record the QA review document",
        "ship": "write the ship summary and rollback plan",
        "sdd-fast": "write the fast-lane document and pass tests",
    }
    return labels.get(phase, f"work in the {phase} phase")
