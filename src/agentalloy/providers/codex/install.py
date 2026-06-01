"""Codex install module — apply_persistent_config / install_writer for Codex CLI.

Writes ~/.codex/config.toml with an apiBaseUrl sentinel-bounded block
pointing to the AgentAlloy proxy.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from agentalloy.providers.base import WireRecord

_SENTINEL_BEGIN = "# <!-- BEGIN agentalloy install -->"
_SENTINEL_END = "# <!-- END agentalloy install -->"


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text()
    return None


def _detect_line_ending(content: str) -> str:
    """Detect whether file uses CRLF or LF."""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def _inject_sentinel_block(existing: str, block: str) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    If sentinels already exist, replaces the content between them.
    If not, appends the full sentinel block at the end.

    The ``block`` argument should contain the INNER content (without
    sentinel markers). This function wraps it with sentinels.
    """
    nl = _detect_line_ending(existing) if existing else "\n"

    full_block = f"{_SENTINEL_BEGIN}{nl}{block}{nl}{_SENTINEL_END}"

    begin_count = existing.count(_SENTINEL_BEGIN)
    end_count = existing.count(_SENTINEL_END)
    if begin_count > 1 or end_count > 1:
        print(
            f"ERROR: target file contains {begin_count} BEGIN and {end_count} END "
            f"agentalloy sentinels (expected at most 1 of each). Refusing to write.",
            file=sys.stderr,
        )
        print(
            "FIX:   Remove duplicate sentinel blocks manually, leaving at most one pair.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if _SENTINEL_BEGIN in existing and _SENTINEL_END in existing:
        # Replace existing block — find the FIRST pair of markers
        begin_idx = existing.index(_SENTINEL_BEGIN)
        end_idx = existing.index(_SENTINEL_END) + len(_SENTINEL_END)
        # Consume trailing newline if present
        if end_idx < len(existing) and existing[end_idx] in ("\n", "\r"):
            if existing[end_idx : end_idx + 2] == "\r\n":
                end_idx += 2
            else:
                end_idx += 1
        return existing[:begin_idx] + full_block + nl + existing[end_idx:]

    # Append at end
    if existing and not existing.endswith(nl):
        existing += nl
    if existing:
        existing += nl  # blank line separator
    return existing + full_block + nl


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for codex by writing ~/.codex/config.toml.

    Creates a TOML config file with an apiBaseUrl sentinel-bounded block
    pointing to the AgentAlloy proxy.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (used for path resolution).
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    config_path = Path.home() / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    proxy_url = f"http://localhost:{port}/v1"

    # Build the TOML config block WITHOUT sentinel markers.
    # _inject_sentinel_block will add them.
    block_lines = [
        "[codex]",
        f'apiBaseUrl = "{proxy_url}"',
        'apiKey = "agentalloy"',
    ]
    block = "\n".join(block_lines)

    original_content = _capture_original(config_path)

    if config_path.exists():
        content = config_path.read_text()
        content = _inject_sentinel_block(content, block)
    else:
        # Write with sentinels for new files
        content = f"{_SENTINEL_BEGIN}\n{block}\n{_SENTINEL_END}\n"

    content_sha = _sha256(block)

    config_path.write_text(content)

    return [
        WireRecord(
            path=str(config_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=content_sha,
            original_content=original_content,
            marker_key="codex.apiBaseUrl",
        )
    ]
