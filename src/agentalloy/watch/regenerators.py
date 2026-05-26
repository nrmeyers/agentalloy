"""Per-harness rules-file regenerators for sidecar harnesses.

Each regenerator takes a content string and a project_root and writes (or
updates) the harness-specific rules file. Uses marker blocks so non-AgentAlloy
content in shared files is preserved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

AGENTALLOY_MARKER = "AGENTALLOY-CONTEXT"
_BEGIN = f"<!-- BEGIN {AGENTALLOY_MARKER} -->"
_END = f"<!-- END {AGENTALLOY_MARKER} -->"


def update_block(path: Path, marker: str, body: str) -> None:
    """Replace (or append) a named marker block in ``path``.

    Preserves all content outside the block byte-for-byte.
    On first call, appends the block; subsequent calls replace in place.
    """
    begin = f"<!-- BEGIN {marker} -->"
    end = f"<!-- END {marker} -->"
    block = f"{begin}\n{body.rstrip()}\n{end}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    if begin in existing and end in existing:
        start_idx = existing.index(begin)
        end_idx = existing.index(end) + len(end)
        # Consume a single trailing newline after the END marker so the block
        # replacement doesn't leave a blank line, but only if one actually
        # exists — guards against EOF-without-newline and \r\n line endings.
        if end_idx < len(existing):
            if existing[end_idx] == "\n":
                end_idx += 1
            elif existing[end_idx : end_idx + 2] == "\r\n":
                end_idx += 2
        new_content = existing[:start_idx] + block + existing[end_idx:]
    else:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        new_content = existing + separator + block

    path.write_text(new_content, encoding="utf-8")


def regenerate_cursor(content: str, project_root: Path) -> None:
    """Write to .cursor/rules/agentalloy-context.mdc with YAML frontmatter."""
    path = project_root / ".cursor" / "rules" / "agentalloy-context.mdc"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"""---
description: AgentAlloy phase + contract context
globs: ["**/*"]
alwaysApply: true
---

{content.strip()}
"""
    path.write_text(body, encoding="utf-8")


def regenerate_windsurf(content: str, project_root: Path) -> None:
    """Marker-block replacement in .windsurfrules."""
    update_block(project_root / ".windsurfrules", AGENTALLOY_MARKER, content)


def regenerate_copilot(content: str, project_root: Path) -> None:
    """Marker-block replacement in .github/copilot-instructions.md."""
    update_block(project_root / ".github" / "copilot-instructions.md", AGENTALLOY_MARKER, content)


def regenerate_cline(content: str, project_root: Path) -> None:
    """Marker-block replacement in .clinerules."""
    update_block(project_root / ".clinerules", AGENTALLOY_MARKER, content)


def regenerate_gemini(content: str, project_root: Path) -> None:
    """Marker-block replacement in GEMINI.md."""
    update_block(project_root / "GEMINI.md", AGENTALLOY_MARKER, content)


def regenerate_aider(content: str, project_root: Path) -> None:
    """Write to .aider/agentalloy-context.txt (declared in .aider.conf.yml read:)."""
    path = project_root / ".aider" / "agentalloy-context.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


REGENERATORS: dict[str, Callable[[str, Path], None]] = {
    "cursor": regenerate_cursor,
    "windsurf": regenerate_windsurf,
    "github-copilot": regenerate_copilot,
    "cline": regenerate_cline,
    "gemini-cli": regenerate_gemini,
    "aider": regenerate_aider,
}
