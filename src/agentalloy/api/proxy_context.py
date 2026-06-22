"""Proxy context — working directory resolution and phase reading.

Determines the project root per request (used for reading .agentalloy/phase,
signal evaluation, etc.) and provides helpers to read the current phase file.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

PHASE_FILE = Path(".agentalloy") / "phase"


def encode_proj_token(project_dir: Path | str) -> str:
    """Encode a project directory as the ``/proj/<token>`` URL discriminator.

    The token is ``base64url(realpath(project_dir))`` without padding, so two
    spellings of the same repo (trailing slash, a symlink) collapse to one
    token and it is a clean single URL path segment. The proxy carries it in
    ``ANTHROPIC_BASE_URL=.../proj/<token>`` and decodes it per request — repo
    resolution with zero new server state, stateless and restart-safe.
    """
    real: str = os.path.realpath(os.fspath(project_dir))
    raw = real.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_proj_token(token: str) -> Path:
    """Decode a ``/proj/<token>`` discriminator back to its project directory.

    Inverse of :func:`encode_proj_token`. Raises ``ValueError`` on a token that
    isn't valid base64url, doesn't decode to UTF-8, or doesn't yield an absolute
    path (``encode`` always realpaths, so anything relative is malformed).
    """
    pad = "=" * (-len(token) % 4)
    try:
        # binascii.Error and UnicodeDecodeError are both ValueError subclasses.
        text = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except ValueError as e:
        raise ValueError(f"malformed /proj token: {token!r}") from e
    if not text.startswith("/"):
        raise ValueError(f"/proj token did not decode to an absolute path: {token!r}")
    return Path(text)


def resolve_working_dir(request: ProxyRequest, project_dir_override: Path | None = None) -> Path:
    """Determine the project working directory for this request.

    Resolution order:
    0. ``project_dir_override`` — the decoded ``/proj/<token>`` (native passthrough)
    1. ``request.metadata["cwd"]`` — explicit harness-supplied directory
    2. ``AGENTALLOY_PROJECT_DIR`` environment variable
    3. ``Path.cwd()`` — proxy process working directory (last resort)
    """
    # 0. Highest precedence: the decoded per-repo discriminator token. Resolving
    #    from the URL means the proxy never depends on its own cwd.
    if project_dir_override is not None:
        return project_dir_override

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

    Handles both YAML format ("phase: build") and plain text ("build").

    Returns the stripped phase string (e.g. "build") or ``None`` if the file
    does not exist, is empty, or cannot be read.
    """
    from agentalloy.signals.skill_loader import (
        _read_phase,  # pyright: ignore[reportPrivateUsage]
    )

    return _read_phase(cwd)
