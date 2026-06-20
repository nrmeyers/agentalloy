"""Windsurf install module — apply_persistent_config / install_writer.

Writes .windsurf/rules/agentalloy.md (modern, dedicated file) or
.windsurfrules (legacy, sentinel-bounded) with the AgentAlloy skill context.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentalloy.install.sentinel_utils import replace_marked_block
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


def _inject_sentinel_block(existing: str, block: str) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    Delegates to the shared ``replace_marked_block`` helper which
    validates BEGIN-before-END ordering and duplicate counts.
    """
    return replace_marked_block(existing, block, _SENTINEL_BEGIN, _SENTINEL_END)


def _resolve_windsurf_path(root: Path) -> tuple[str, bool]:
    """Resolve Windsurf target path.

    Returns (relative_path, is_dedicated_file).
    Modern: .windsurf/rules/agentalloy.md (dedicated file)
    Legacy: .windsurfrules (shared, sentinel-bounded)
    """
    if (root / ".windsurf").is_dir():
        return ".windsurf/rules/agentalloy.md", True
    return ".windsurfrules", False


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for windsurf.

    Writes .windsurf/rules/agentalloy.md (dedicated) or .windsurfrules (shared)
    with the AgentAlloy skill-context instruction block.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    rel_path, dedicated = _resolve_windsurf_path(root)
    target_path = root / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    instruction_content = (
        "---\n"
        "description: Fetch skill context before starting any SDD coding task\n"
        'globs: ["**/*"]\n'
        "---\n\n"
        "# AgentAlloy -- skill context\n\n"
        f"A local agentalloy service runs at `http://localhost:{port}`.\n\n"
        f"**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`.\n\n"
        "**Session start -- determine phase.** Check `.agentalloy/phase` for the current phase.\n\n"
        "**When in an SDD phase, before starting work:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "Phases: `spec`, `design`, `build`, `qa`, `ship`.\n"
    )

    original_content = _capture_original(target_path)

    if dedicated:
        # Dedicated file -- we own it entirely
        target_path.write_text(instruction_content, encoding="utf-8")
        return [
            WireRecord(
                path=str(target_path),
                action="wrote_new_file",
                content_sha256=_sha256(instruction_content),
                original_content=original_content,
                marker_key="windsurf.rules",
            )
        ]
    else:
        # Shared file -- sentinel-bounded injection
        existing = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        result_content = _inject_sentinel_block(existing, instruction_content)
        target_path.write_text(result_content, encoding="utf-8")
        return [
            WireRecord(
                path=str(target_path),
                action="injected_block",
                content_sha256=_sha256(instruction_content),
                original_content=original_content,
                marker_key="windsurf.rules.sentinel",
            )
        ]
