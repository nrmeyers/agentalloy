"""Proxy context — working directory resolution and phase reading.

Determines the project root per request (used for reading .agentalloy/phase,
signal evaluation, etc.) and provides helpers to read the current phase file.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

PHASE_FILE = Path(".agentalloy") / "phase"
UPSTREAM_FILE = Path(".agentalloy") / "upstream"


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


@dataclass(frozen=True)
class Upstream:
    """A harness's captured upstream LLM, read from ``.agentalloy/upstream``.

    ``url`` and ``model`` are what the proxy forwards to; ``key_env`` is the
    *name* of the environment variable holding the upstream API key (never the
    secret itself — the proxy resolves it from its own process env at request
    time, so no credential is written into the repo).
    """

    url: str
    model: str
    key_env: str | None = None


def read_upstream(cwd: Path) -> Upstream | None:
    """Read the captured upstream from *cwd*/.agentalloy/upstream.

    The file is YAML written by ``agentalloy add <harness>``::

        url: http://host:port/v1
        model: some-model
        key_env: OPENAI_API_KEY   # optional; env-var name, not the secret

    Returns ``None`` when the file is absent, empty, malformed, or missing the
    required ``url``/``model`` keys — callers then fall back to the global
    upstream. Never raises on a bad file; a per-repo override must never take
    down the proxy.
    """
    path = cwd / UPSTREAM_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
        return None

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("malformed %s: %s", path, e)
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast("dict[str, object]", parsed)

    url = data.get("url")
    model = data.get("model")
    if not isinstance(url, str) or not url or not isinstance(model, str) or not model:
        logger.warning("%s missing required url/model", path)
        return None

    key_env_raw = data.get("key_env")
    key_env = key_env_raw if isinstance(key_env_raw, str) and key_env_raw else None

    return Upstream(url=url.rstrip("/"), model=model, key_env=key_env)
