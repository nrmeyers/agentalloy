"""Aider install module — apply_persistent_config / install_writer.

Wires aider via two files:
1. .agentalloy-aider-instructions.md — dedicated instructions file
2. .aider.conf.yml — sentinel-bounded block configuring the proxy

Also handles legacy markdown-injection mode (GEMINI.md equivalent).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentalloy.install.sentinel_utils import replace_marked_block
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


def _inject_sentinel_block(existing: str, block: str) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    Delegates to the shared ``replace_marked_block`` helper which
    validates BEGIN-before-END ordering and duplicate counts.
    """
    return replace_marked_block(existing, block, _SENTINEL_BEGIN, _SENTINEL_END)


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for aider.

    Writes two files:
    1. .agentalloy-aider-instructions.md — dedicated instructions file
    2. .aider.conf.yml — sentinel-bounded block with proxy config

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: If True, skip tamper detection.

    Returns:
        List of WireRecord describing files written.
    """
    files: list[WireRecord] = []

    # 1. Write .agentalloy-aider-instructions.md (dedicated file)
    instructions_path = root / ".agentalloy-aider-instructions.md"
    instructions_path.parent.mkdir(parents=True, exist_ok=True)

    template_content = (
        "## AgentAlloy — skill context\n\n"
        f"A local agentalloy service runs at `http://localhost:{port}` with a curated\n"
        "corpus of engineering skills.\n\n"
        f"**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`.\n"
        "If unreachable, ignore this block.\n\n"
        "**Session start — determine phase.** Check `.agentalloy/phase` for the current\n"
        "phase. If it exists, use that phase.\n\n"
        "**When in an SDD phase, before starting work, run:**\n"
        "```bash\n"
        f"curl -s -X POST http://localhost:{port}/compose/text \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"task": "<task>", "phase": "<phase>"}\'\n'
        "```\n\n"
        "Phases: `spec`, `design`, `build`, `qa`, `ops`.\n"
    )

    original_instructions = _capture_original(instructions_path)
    instructions_path.write_text(template_content, encoding="utf-8")
    files.append(
        WireRecord(
            path=str(instructions_path),
            action="wrote_new_file",
            content_sha256=_sha256(template_content),
            original_content=original_instructions,
            marker_key="aider.instructions",
        )
    )

    # 2. Write/update .aider.conf.yml with proxy config
    conf_path = root / ".aider.conf.yml"
    original_conf = _capture_original(conf_path)

    proxy_url = f"http://localhost:{port}/v1"
    block_lines = [
        _SENTINEL_BEGIN,
        f"openai-api-base: {proxy_url}",
        "openai-api-key: agentalloy",
        "model: agentalloy-proxy",
        _SENTINEL_END,
    ]
    block = "\n".join(block_lines)

    if conf_path.exists():
        content = conf_path.read_text(encoding="utf-8")
        content = _inject_sentinel_block(content, block)
    else:
        content = block + "\n"

    conf_path.write_text(content, encoding="utf-8")
    files.append(
        WireRecord(
            path=str(conf_path),
            action="wrote_new_file" if original_conf is None else "injected_block",
            content_sha256=_sha256(block),
            original_content=original_conf,
            marker_key="aider.conf.proxy",
        )
    )

    return files
