"""Codex provider — HarnessSpec registration for OpenAI Codex CLI.

Registers the ``codex`` harness in REGISTRY with:
- Protocol: OPENAI (modern codex speaks ONLY the OpenAI Responses API; the
  proxy serves it natively at /proj/<token>/v1/responses —
  docs/responses-surface.md)
- Capabilities: PROXY (repo-local CODEX_HOME carrier)
- env_builder: sets CODEX_HOME so codex reads the repo-local config
- install_writer: writes <repo>/.codex/{config.toml,.agentalloy-env,.gitignore}
"""

from __future__ import annotations

import os
from pathlib import Path

from agentalloy.providers import REGISTRY
from agentalloy.providers.base import (
    Capability,
    HarnessSpec,
    Protocol,
    WireRecord,
)

from . import install


def _env_builder(port: int) -> dict[str, str]:
    """Build environment dict for the codex subprocess.

    Sets CODEX_HOME to the launch cwd's ``.codex/`` so codex reads the
    repo-local config the install_writer wrote (per-repo ``/proj/<token>``
    base URL, ``wire_api="responses"``). codex ignores OPENAI_BASE_URL, so the
    config file is the only routing vector; env alone cannot wire it.
    """
    _ = port
    return {"CODEX_HOME": os.path.join(os.getcwd(), ".codex")}


def _install_writer(port: int, root: Path, force: bool = False) -> list[WireRecord]:
    """Install wiring for codex via a repo-local CODEX_HOME.

    Writes ``.codex/config.toml`` (global config + agentalloy Responses
    provider), ``.codex/.agentalloy-env`` (CODEX_HOME export), and a
    ``.codex/.gitignore`` keeping codex session/auth state out of git.
    """
    return install.apply_persistent_config(port, root, force)


# Register the harness in the global REGISTRY.
REGISTRY["codex"] = HarnessSpec(
    name="codex",
    binary="codex",
    capabilities=(Capability.PROXY,),
    protocol=Protocol.OPENAI,
    env_builder=_env_builder,
    install_writer=_install_writer,
)
