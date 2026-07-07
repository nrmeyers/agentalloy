"""Codex install module — apply_persistent_config / install_writer for Codex CLI.

Proxy wiring via a repo-local ``CODEX_HOME`` (hermes pattern). Modern codex is
Responses-API-only: it ignores ``OPENAI_BASE_URL``, and a custom provider must
declare ``wire_api = "responses"`` (``"chat"`` was removed upstream). The proxy
serves that wire natively at ``/proj/<token>/v1/responses``
(docs/responses-surface.md), so wiring writes:

- ``<repo>/.codex/config.toml`` — the user's global ``~/.codex/config.toml``
  (their tuning survives) with ``model_provider = "agentalloy"`` and an
  ``[model_providers.agentalloy]`` block pointed at the proxy's per-repo
  endpoint. Auth rides ``env_key = "OPENAI_API_KEY"`` — the key is the user's
  real one, forwarded transparently to the Responses upstream; the global
  ``auth.json`` is never copied into the repo.
- ``<repo>/.codex/.agentalloy-env`` — exports ``CODEX_HOME`` (source it, or
  launch via ``agentalloy wrap codex`` which injects it from env_builder).
- ``<repo>/.codex/.gitignore`` — ``*``: codex writes session state (and would
  write auth state) under CODEX_HOME; none of it belongs in git.
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import tomli_w

from agentalloy.providers.base import WireRecord


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _capture_original(path: Path) -> str | None:
    """Read and return the file's content if it exists, else None."""
    if path.exists():
        return path.read_text()
    return None


def render_config(port: int, root: Path) -> str:
    """The repo-local ``config.toml`` content: global config + agentalloy provider."""
    from agentalloy.api.proxy_context import encode_proj_token

    token = encode_proj_token(root)
    proxy_base = f"http://localhost:{port}/proj/{token}/v1"

    config: dict[str, Any] = {}
    global_config = Path.home() / ".codex" / "config.toml"
    if global_config.exists():
        try:
            config = tomllib.loads(global_config.read_text())
        except Exception:  # noqa: BLE001 — a malformed global config must not block wiring
            config = {}

    config["model_provider"] = "agentalloy"
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        providers = {}
    providers = cast("dict[str, Any]", providers)
    providers["agentalloy"] = {
        "name": "AgentAlloy",
        "base_url": proxy_base,
        # Modern codex removed wire_api="chat"; the proxy serves the Responses
        # wire natively (docs/responses-surface.md).
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    }
    config["model_providers"] = providers
    return tomli_w.dumps(config)


def apply_persistent_config(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install proxy wiring for codex via a repo-local ``CODEX_HOME``.

    Args:
        port: The AgentAlloy proxy port.
        root: The repository root.
        force: Unused — every file under ``.codex/`` we write is owned fully.

    Returns:
        List of WireRecord describing files written.
    """
    _ = force
    codex_dir = root / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    records: list[WireRecord] = []

    config_path = codex_dir / "config.toml"
    original_config = _capture_original(config_path)
    config_text = render_config(port, root)
    config_path.write_text(config_text)
    records.append(
        WireRecord(
            path=str(config_path),
            action="wrote_new_file" if original_config is None else "replaced_file",
            content_sha256=_sha256(config_text),
            original_content=original_config,
            marker_key="codex.model_provider",
        )
    )

    env_path = codex_dir / ".agentalloy-env"
    original_env = _capture_original(env_path)
    env_text = 'export CODEX_HOME="$PWD/.codex"\n'
    env_path.write_text(env_text)
    records.append(
        WireRecord(
            path=str(env_path),
            action="wrote_new_file" if original_env is None else "replaced_file",
            content_sha256=_sha256(env_text),
            original_content=original_env,
            marker_key="codex.env",
        )
    )

    gitignore_path = codex_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_text = "*\n"
        gitignore_path.write_text(gitignore_text)
        records.append(
            WireRecord(
                path=str(gitignore_path),
                action="wrote_new_file",
                content_sha256=_sha256(gitignore_text),
                marker_key="codex.gitignore",
            )
        )

    print(
        "[AgentAlloy] codex wired via repo-local CODEX_HOME (.codex/config.toml, "
        "wire_api=responses). Launch with `agentalloy wrap codex -- codex [args]`, or "
        "`source .codex/.agentalloy-env` before running `codex` in this repo. "
        "Auth: codex reads your real OPENAI_API_KEY (env_key) and the proxy forwards "
        "it transparently to the Responses upstream.",
        file=sys.stderr,
    )

    return records
