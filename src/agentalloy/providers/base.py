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
from typing import TYPE_CHECKING, Any

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

_VALID_ACTIONS = frozenset({"wrote_new_file", "injected_block", "env_export"})


@dataclass(frozen=True)
class WireRecord:
    """A single file-write action performed by an install/wire writer.

    Fields:
        path:                absolute path to the file written or modified.
        action:              one of ``"wrote_new_file"``, ``"injected_block"``,
                             or ``"env_export"``.
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

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(
                f"WireRecord.action must be one of {_VALID_ACTIONS}; got {self.action!r}"
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
        "**Open every session with intake.** Check `.agentalloy/phase`:\n"
        "- If it names a phase other than `intake`, work is in progress — tell the user where\n"
        "  they left off (the phase + the active contract under `.agentalloy/contracts/`) and\n"
        "  ask whether to resume there or start something new. Resume → continue in that phase.\n"
        "  New → `agentalloy phase set intake`, then run intake.\n"
        "- Otherwise run **intake**: a brief intent interview, decide **full SDD vs the fast\n"
        "  lane**, and write a contract — which hands off to the chosen route.\n\n"
        "**When in an SDD phase, before starting work, compose skill context:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (`sdd-fast` = the fast lane).\n"
    )
