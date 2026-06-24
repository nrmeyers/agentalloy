"""Codex install module — apply_persistent_config / install_writer for Codex CLI.

Writes ~/.codex/config.toml with an apiBaseUrl sentinel-bounded block
pointing to the AgentAlloy proxy.
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
        return path.read_text()
    return None


def _inject_sentinel_block(existing: str, block: str) -> str:
    """Insert or replace a sentinel-bounded block in existing content.

    Delegates to the shared ``replace_marked_block`` helper which
    validates BEGIN-before-END ordering and duplicate counts.
    """
    return replace_marked_block(existing, block, _SENTINEL_BEGIN, _SENTINEL_END)


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

    # Tokenless on purpose: this is a USER-scoped config (one ~/.codex/config.toml
    # for every repo), so it cannot carry a per-repo /proj/<token>. Per-repo
    # resolution comes from the env_builder instead (it bakes encode_proj_token of
    # the launch cwd into OPENAI_BASE_URL, which overrides this file). A direct
    # `codex` launch relying solely on this file is not repo-disambiguated.
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
