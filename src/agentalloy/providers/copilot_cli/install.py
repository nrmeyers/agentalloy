"""Copilot CLI install module — apply_persistent_config / install_writer.

Proxy wiring for the standalone GitHub Copilot CLI (npm ``@github/copilot``,
GA Feb 2026). BYOK (Apr 2026) is env-var-only — Copilot CLI reads
``COPILOT_PROVIDER_*`` variables at launch and persists nothing in
``~/.copilot/config.json`` — so the persistent carrier is a repo-local env
file the user sources (or ``agentalloy wrap`` injects) before launching
``copilot``.

Distinct from the ``github-copilot`` harness, which covers the IDE/extension
surface and remains markdown-only (its model traffic routes through GitHub's
backend and cannot be intercepted).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from agentalloy.providers.base import WireRecord

ENV_FILE_RELPATH = ".copilot/.agentalloy-env"


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def build_env(port: int, root: Path) -> dict[str, str]:
    """BYOK env vars pointing Copilot CLI at the proxy's per-repo endpoint.

    ``COPILOT_PROVIDER_TYPE=openai`` selects the OpenAI-compatible wire, so the
    base URL targets the proxy's ``/proj/<token>/v1`` OpenAI surface.
    """
    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(root)
    return {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": f"http://localhost:{port}/proj/{token}/v1",
        "COPILOT_PROVIDER_API_KEY": "agentalloy",
        "COPILOT_MODEL": "agentalloy-proxy",
    }


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install persistent proxy wiring for Copilot CLI.

    Writes ``.copilot/.agentalloy-env`` exporting the ``COPILOT_PROVIDER_*``
    BYOK variables with the per-repo ``/proj/<token>`` discriminator baked in.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — the env file is a generated file we own fully.

    Returns:
        List of WireRecord describing files written.
    """
    _ = force
    env_path = root / ENV_FILE_RELPATH
    env_path.parent.mkdir(parents=True, exist_ok=True)

    original_content = env_path.read_text() if env_path.exists() else None
    env_content = "".join(
        f'export {key}="{value}"\n' for key, value in build_env(port, root).items()
    )
    env_path.write_text(env_content)

    print(
        f"[AgentAlloy] copilot-cli wired via {ENV_FILE_RELPATH} (BYOK env vars). "
        f"Launch with `agentalloy wrap copilot-cli -- copilot [args]`, or "
        f"`source {ENV_FILE_RELPATH}` before running `copilot` in this repo. "
        "Note: BYOK routes model traffic to your configured upstream (billed to "
        "your key), not your Copilot subscription.",
        file=sys.stderr,
    )

    return [
        WireRecord(
            path=str(env_path),
            action="wrote_new_file" if original_content is None else "replaced_file",
            content_sha256=_sha256(env_content),
            original_content=original_content,
            marker_key="copilot-cli.env",
        )
    ]
