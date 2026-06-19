"""Hermes Agent install module — apply_persistent_config / install_writer.

Writes .hermes/SOUL.md (user scope) or AGENTS.md (repo scope) with a
sentinel-bounded block containing the AgentAlloy skill-context prose.
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


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent wiring for hermes-agent.

    Writes .hermes/SOUL.md (user scope) or AGENTS.md (repo scope) with
    a sentinel-bounded block containing the AgentAlloy skill-context prose.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    # Determine scope: use .hermes/ directory as indicator for user scope
    hermes_dir = Path.home() / ".hermes"
    target_path = hermes_dir / "SOUL.md" if hermes_dir.exists() else root / "AGENTS.md"

    target_path.parent.mkdir(parents=True, exist_ok=True)

    instruction_content = (
        "## AgentAlloy -- skill context\n\n"
        f"A local agentalloy service runs at `http://localhost:{port}`.\n\n"
        f"**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`.\n\n"
        "**Session start -- determine phase.** Check `.agentalloy/phase` for the current phase.\n\n"
        "**When in an SDD phase, before starting work, run:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "**Phase transitions.** If the user's activity clearly shifts to a different\n"
        "SDD phase, update `.agentalloy/phase` and call `/compose` with the new phase.\n\n"
        "Phases: `spec`, `design`, `build`, `qa`, `ship`.\n"
    )

    original_content = _capture_original(target_path)

    if target_path.exists():
        content = target_path.read_text(encoding="utf-8")
        content = _inject_sentinel_block(content, instruction_content)
    else:
        content = f"{_SENTINEL_BEGIN}\n{instruction_content}\n{_SENTINEL_END}\n"

    target_path.write_text(content, encoding="utf-8")

    return [
        WireRecord(
            path=str(target_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=_sha256(instruction_content),
            original_content=original_content,
            marker_key="hermes-agent.instructions",
        )
    ]
