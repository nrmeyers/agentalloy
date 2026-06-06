"""Compatibility helpers for replacing deprecated wire_harness() calls.

The old wire_harness() function is deprecated. All test code should use
agentalloy.providers.REGISTRY directly. This module provides a compatibility
wrapper that returns the same dict shape as the old wire_harness() function
for easy migration.

Usage:
    from tests._wire_compat import wire_compat

    result = wire_compat("claude-code", port=7070, root=tmp_path)
"""

from __future__ import annotations

import pathlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.wire_harness import SCHEMA_VERSION
from agentalloy.install.subcommands.wire_harness import wire_harness as _deprecated_wire_harness
from agentalloy.providers import REGISTRY
from agentalloy.providers.base import Capability

# Harnesses whose REGISTRY providers do not match the legacy wire_harness()
# proxy behavior (or are stubs). These are handled by delegating to the
# deprecated wire_harness() function which has the correct legacy behavior.
_REGISTRY_PROVIDERS_USING_HOME = frozenset(
    {
        "claude-code",
        "hermes-agent",
        "opencode",
        "codex",
        "openclaw",
        # REGISTRY providers don't match legacy proxy behavior
        "aider",
        "continue-closed",
        "continue-local",
        "cursor",
    }
)


def wire_compat(
    harness: str,
    port: int | None = None,
    root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility wrapper that replaces wire_harness().

    Uses agentalloy.providers.REGISTRY[harness].install_writer() and returns
    the same dict shape as the old wire_harness() function.

    Args:
        harness: Harness name (e.g. "claude-code", "aider").
        port: Proxy port number (required for non-legacy calls).
        root: Repository root path (required for non-legacy calls).
        **kwargs: Additional kwargs (force, scope, legacy, mcp_fallback) passed
            through to the underlying implementation.

    Returns:
        Dict with keys: schema_version, harness, integration_vector, files_written
    """
    import warnings

    legacy = kwargs.pop("legacy", False)
    mcp_fallback = kwargs.pop("mcp_fallback", False)
    scope = kwargs.get("scope", "user")

    # Validate scope early — mirrors wire_harness() validation
    if scope not in ("user", "repo"):
        print(f"ERROR: --scope must be 'user' or 'repo', got '{scope}'", file=sys.stderr)
        raise SystemExit(1)

    # Unknown harnesses: delegate to deprecated wire_harness() which raises
    # SystemExit with the same error messages the CLI would produce.
    # "mcp-only" is exempted here — it has a dedicated handler below that
    # prints a specific migration message instead.
    if harness not in REGISTRY and harness != "mcp-only":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            _deprecated_wire_harness(harness, port=port, root=root, **kwargs)
        # Should never reach here — wire_harness raises SystemExit
        raise SystemExit(1)

    # For legacy=True or mcp_fallback=True, use the deprecated wire_harness()
    # to preserve the old hooks/markdown-injection/MCP behavior that the
    # REGISTRY providers don't support.
    if legacy or mcp_fallback:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return _deprecated_wire_harness(
                harness,
                port=port,
                root=root,
                legacy=legacy,
                mcp_fallback=mcp_fallback,
                **kwargs,
            )

    # Special cases that don't have REGISTRY entries or need special handling
    if harness == "mcp-only":
        print(
            "ERROR: --harness mcp-only is no longer a standalone harness.",
            file=sys.stderr,
        )
        print(
            "FIX:   Pick a real harness and add --mcp-fallback. Example:",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if harness == "manual":
        # Manual harness prints to stderr, doesn't write files
        if port is None:
            raise TypeError("wire_compat() missing 1 required positional argument: 'port'")
        block = (
            "<!-- BEGIN agentalloy install -->\n"
            f"AgentAlloy proxy at http://localhost:{port}/v1\n"
            "<!-- END agentalloy install -->\n"
        )
        print(block, file=sys.stderr)
        return {
            "schema_version": SCHEMA_VERSION,
            "harness": harness,
            "integration_vector": "proxy",
            "files_written": [],
            "manual_block": block,
        }

    # Ensure root and port are provided for non-legacy calls
    if root is None:
        root = Path.cwd()
    if port is None:
        raise TypeError("wire_compat() missing 1 required positional argument: 'port'")

    spec = REGISTRY[harness]
    force = kwargs.get("force", False)
    install_writer = spec.install_writer
    if install_writer is None:
        raise ValueError(f"Harness {harness!r} has no install_writer")

    # Providers that use Path.home() for file paths instead of respecting root
    # need to be delegated to the deprecated wire_harness() function.
    if harness in _REGISTRY_PROVIDERS_USING_HOME:
        with __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore", DeprecationWarning)
            return _deprecated_wire_harness(
                harness,
                port=port,
                root=root,
                **kwargs,
            )

    # For other providers, use REGISTRY directly with Path.home() mocked.
    # This handles providers that use root directly for file paths.
    with patch.object(pathlib.Path, "home", return_value=root):
        # pyright: ignore[reportCallIssue] - Callable type annotation mismatch
        records = install_writer(port, root, force=force)  # type: ignore[call-arg]

    # Determine vector type from capabilities
    vector = "proxy" if Capability.PROXY in spec.capabilities else "markdown_injection"

    # Build file entries in the same shape as wire_harness._build_result
    files_written: list[dict[str, Any]] = []
    for r in records:
        entry = r.to_dict()
        entry.setdefault("harness", harness)
        entry.setdefault("repo_root", str(root))
        files_written.append(entry)

    # Record state (same as wire_harness._build_result)
    st = install_state.load_state(root)
    prior = st.get("harness_files_written") or []
    new_paths = {f.get("path") for f in files_written}
    prior_by_path = {e.get("path"): e for e in prior}
    for new_entry in files_written:
        prior_entry = prior_by_path.get(new_entry.get("path"))
        if prior_entry and "original_content" in prior_entry:
            new_entry.setdefault("original_content", prior_entry["original_content"])
    merged = [e for e in prior if e.get("path") not in new_paths] + files_written
    st["harness_files_written"] = merged
    install_state.save_state(st, root)

    return {
        "schema_version": SCHEMA_VERSION,
        "harness": harness,
        "integration_vector": vector,
        "files_written": files_written,
    }
