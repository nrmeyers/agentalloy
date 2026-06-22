"""Claude Code install module — apply_persistent_config / install_writer.

Writes ~/.agentalloy/claude-code-env.sh with a sentinel-bounded block containing
**only** ANTHROPIC_BASE_URL (auth-transparent — never ANTHROPIC_API_KEY), pointing
at the proxy's per-repo /proj/<token> discriminator.

The primary carrier, though, is ``<root>/.claude/settings.local.json``: Claude
Code natively reads its ``env`` block, so writing ANTHROPIC_BASE_URL there makes
the proxy auto-load with no ``source``/direnv step. settings.local.json is
gitignored by Claude Code convention, so the machine-specific /proj/<token> URL
never lands in git. See :func:`apply_claude_settings_env`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from agentalloy.api.proxy_context import encode_proj_token
from agentalloy.install.sentinel_utils import replace_marked_block
from agentalloy.providers.base import WireRecord

_SENTINEL_BEGIN = "# <!-- BEGIN agentalloy install -->"
_SENTINEL_END = "# <!-- END agentalloy install -->"


def apply_claude_settings_env(root: Path, proxy_url: str) -> tuple[Path, str | None]:
    """Merge ``env.ANTHROPIC_BASE_URL`` into ``<root>/.claude/settings.local.json``.

    Claude Code reads the ``env`` map from settings.local.json on every session,
    so this is the auto-load carrier — no shell ``source`` or direnv needed. The
    file is gitignored by Claude Code convention, so the machine-specific
    ``/proj/<token>`` URL stays out of git.

    Auth transparency: sets **only** ``ANTHROPIC_BASE_URL`` — never
    ``ANTHROPIC_API_KEY`` (a dummy key would force API-key mode and break
    account/OAuth auth for Pro/Max/Team users). The merge preserves every other
    key the user already has (permissions, MCP toggles, …).

    Returns ``(settings_path, original_text)`` where ``original_text`` is the
    file's prior contents (``None`` if it did not exist). Idempotent: re-running
    just rewrites the same URL.

    Raises ``json.JSONDecodeError`` if the file exists but is not valid JSON, so
    the caller can fall back to the shell-env carrier rather than clobber it.
    """
    settings_dir = root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"

    original_text: str | None = None
    data: dict[str, Any] = {}
    if settings_path.exists():
        original_text = settings_path.read_text(encoding="utf-8")
        if original_text.strip():
            parsed = json.loads(original_text)  # may raise — caller falls back
            if isinstance(parsed, dict):
                data = cast("dict[str, Any]", parsed)

    env_raw = data.get("env")
    env = cast("dict[str, Any]", env_raw) if isinstance(env_raw, dict) else {}
    env["ANTHROPIC_BASE_URL"] = proxy_url
    data["env"] = env

    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings_path, original_text


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

    # Per-repo /proj/<token> discriminator (no /v1 suffix: the Anthropic SDK
    # appends /v1/messages). Only ANTHROPIC_BASE_URL is set — never an API key —
    # so the proxy forwards the caller's own credential (account/OAuth safe).
    token = encode_proj_token(root)
    proxy_url = f"http://localhost:{port}/proj/{token}"

    block_lines = [
        _SENTINEL_BEGIN,
        f'export ANTHROPIC_BASE_URL="{proxy_url}"',
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

    # Primary carrier: settings.local.json `env` block (auto-loaded by Claude
    # Code). The shell env file above stays as the direnv/shell fallback. A
    # malformed settings file is left untouched — the shell carrier still works.
    with contextlib.suppress(json.JSONDecodeError):
        apply_claude_settings_env(root, proxy_url)

    return [
        WireRecord(
            path=str(env_path),
            action="wrote_new_file" if original_content is None else "injected_block",
            content_sha256=content_sha,
            original_content=original_content,
            marker_key="claude-code.env",
        )
    ]
