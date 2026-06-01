"""Cursor install module — apply_persistent_config / install_writer.

Writes .cursor/rules/agentalloy.mdc (modern, dedicated file) or
.cursorrules (legacy, sentinel-bounded) with the AgentAlloy skill context.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentalloy.providers.base import WireRecord

_SENTINEL_BEGIN = "<!-- BEGIN agentalloy install -->"
_SENTINEL_END = "<!-- END agentalloy install -->"


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
    """Insert or replace a sentinel-bounded block in existing content."""
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
        begin_idx = existing.index(_SENTINEL_BEGIN)
        end_idx = existing.index(_SENTINEL_END) + len(_SENTINEL_END)
        if end_idx < len(existing) and existing[end_idx] in ("\n", "\r"):
            if existing[end_idx : end_idx + 2] == "\r\n":
                end_idx += 2
            else:
                end_idx += 1
        return existing[:begin_idx] + full_block + nl + existing[end_idx:]

    if existing and not existing.endswith(nl):
        existing += nl
    if existing:
        existing += nl
    return existing + full_block + nl


def _resolve_cursor_path(root: Path) -> tuple[str, bool]:
    """Resolve Cursor target path.

    Returns (relative_path, is_dedicated_file).
    Modern: .cursor/rules/agentalloy.mdc (dedicated file)
    Legacy: .cursorrules (shared, sentinel-bounded)
    """
    if (root / ".cursor").is_dir():
        return ".cursor/rules/agentalloy.mdc", True
    return ".cursorrules", False


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for cursor.

    Writes .cursor/rules/agentalloy.mdc (dedicated) or .cursorrules (shared)
    with the AgentAlloy skill-context instruction block.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    rel_path, dedicated = _resolve_cursor_path(root)
    target_path = root / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    instruction_content = (
        "---\n"
        "description: Fetch skill context before starting any SDD coding task\n"
        'globs: ["**/*"]\n'
        "---\n\n"
        "# AgentAlloy — skill context\n\n"
        f"A local agentalloy service runs at `http://localhost:{port}`.\n\n"
        f"**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`.\n\n"
        "**Session start — determine phase.** Check `.agentalloy/phase` for the current phase.\n\n"
        "**When in an SDD phase, before starting work:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "Phases: `spec`, `design`, `build`, `qa`, `ops`.\n"
    )

    original_content = _capture_original(target_path)

    if dedicated:
        # Dedicated file — we own it entirely
        target_path.write_text(instruction_content, encoding="utf-8")
        return [
            WireRecord(
                path=str(target_path),
                action="wrote_new_file",
                content_sha256=_sha256(instruction_content),
                original_content=original_content,
                marker_key="cursor.rules",
            )
        ]
    else:
        # Shared file — sentinel-bounded injection
        existing = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        result_content = _inject_sentinel_block(existing, instruction_content)
        target_path.write_text(result_content, encoding="utf-8")
        return [
            WireRecord(
                path=str(target_path),
                action="injected_block",
                content_sha256=_sha256(instruction_content),
                original_content=original_content,
                marker_key="cursor.rules.sentinel",
            )
        ]
