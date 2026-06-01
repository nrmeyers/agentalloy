"""Claude Code install module — apply_persistent_config / install_writer.

Writes ~/.agentalloy/claude-code-env.sh with a sentinel-bounded block
containing ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY pointing to the proxy.
"""

from __future__ import annotations

import hashlib
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
        return path.read_text(encoding="utf-8")
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
    """
    nl = _detect_line_ending(existing) if existing else "\n"

    full_block = f"{_SENTINEL_BEGIN}{nl}{block}{nl}{_SENTINEL_END}"

    begin_count = existing.count(_SENTINEL_BEGIN)
    end_count = existing.count(_SENTINEL_END)
    if begin_count > 1 or end_count > 1:
        raise RuntimeError(
            f"target file contains {begin_count} BEGIN and {end_count} END "
            f"agentalloy sentinels (expected at most 1 of each). Refusing to write."
        )

    if _SENTINEL_BEGIN in existing and _SENTINEL_END in existing:
        # Replace existing block
        begin_idx = existing.index(_SENTINEL_BEGIN)
        end_idx = existing.index(_SENTINEL_END) + len(_SENTINEL_END)
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
    """Install wiring for claude-code by writing ~/.agentalloy/claude-code-env.sh.

    Creates a shell script with sentinel-bounded environment variable exports
    pointing to the AgentAlloy proxy.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root (used for path resolution).
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    agentalloy_dir = Path.home() / ".agentalloy"
    agentalloy_dir.mkdir(parents=True, exist_ok=True)

    env_path = agentalloy_dir / "claude-code-env.sh"
    original_content = _capture_original(env_path)

    proxy_url = f"http://localhost:{port}/v1"

    block_lines = [
        _SENTINEL_BEGIN,
        f'export ANTHROPIC_BASE_URL="{proxy_url}"',
        'export ANTHROPIC_API_KEY="agentalloy"',
        _SENTINEL_END,
    ]
    block = "\n".join(block_lines)

    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        content = _inject_sentinel_block(content, block)
    else:
        content = block + "\n"

    content_sha = _sha256(block)

    env_path.write_text(content, encoding="utf-8")

    return [
        WireRecord(
            path=str(env_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=content_sha,
            original_content=original_content,
            marker_key="claude-code.env",
        )
    ]
