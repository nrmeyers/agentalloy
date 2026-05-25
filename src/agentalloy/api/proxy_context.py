"""Proxy context — working directory resolution and phase reading.

Determines the project root per request (used for reading .agentalloy/phase,
signal evaluation, etc.) and provides helpers to read the current phase file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

PHASE_FILE = Path(".agentalloy") / "phase"


def resolve_working_dir(request: ProxyRequest) -> Path:
    """Determine the project working directory for this request.

    Resolution order:
    1. ``request.metadata["cwd"]`` — explicit harness-supplied directory
    2. ``AGENTALLOY_PROJECT_DIR`` environment variable
    3. ``Path.cwd()`` — proxy process working directory (last resort)
    """
    # 1. Check metadata.cwd (harness-supplied)
    if request.metadata is not None:
        cwd = request.metadata.get("cwd")
        if cwd:
            return Path(cwd)

    # 2. Check env var
    env_dir = os.environ.get("AGENTALLOY_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)

    # 3. Fall back to process cwd
    return Path.cwd()


def read_phase(cwd: Path) -> str | None:
    """Read the current phase from *cwd*/.agentalloy/phase.

    Returns the stripped phase string (e.g. "build") or ``None`` if the file
    does not exist, is empty, or cannot be read.
    """
    phase_path = cwd / PHASE_FILE
    try:
        content = phase_path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("Failed to read phase file %s: %s", phase_path, exc)
        return None
    if not content:
        return None
    return content
