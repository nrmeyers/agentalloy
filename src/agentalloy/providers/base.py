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


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------


class Capability(Enum):
    """Integration capability for a harness.

    Exactly four values:
    - HOOK:      the harness uses the AgentAlloy hook path (UserPromptSubmit)
    - PROXY:     the harness uses the AgentAlloy proxy path (base-url rewrite)
    - MARKDOWN_ONLY: the harness only gets markdown injection (no tool-use)
    - MCP_ONLY:  the harness uses the MCP fallback path only
    """

    HOOK = "hook"
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

HarnessSpecHookWriter = Callable[[int, "Path"], "list[WireRecord]"]
"""Write hook configuration for a harness, returning the records of files touched."""

HarnessSpecInstallWriter = Callable[[int, "Path", bool], "list[WireRecord]"]
"""Run the full install/wire for a harness, returning the records of files touched."""


@dataclass(frozen=True)
class HarnessSpec:
    """Immutable specification for a single harness.

    Fields:
        name:                lowercase registry key (e.g. ``"claude-code"``).
        binary:              name of the executable to spawn (e.g. ``"claude"``).
        capabilities:        tuple of ``Capability`` values this harness supports.
        protocol:            the LLM protocol the harness speaks.
        env_builder:         callable that returns env vars for a given port.
        hook_writer:         callable that writes hook config; returns WireRecords.
        install_writer:      callable that runs full wiring; returns WireRecords.
    """

    name: str
    binary: str
    capabilities: tuple[Capability, ...]
    protocol: Protocol
    env_builder: HarnessSpecEnvBuilder
    hook_writer: HarnessSpecHookWriter | None = None
    install_writer: HarnessSpecInstallWriter | None = None


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
