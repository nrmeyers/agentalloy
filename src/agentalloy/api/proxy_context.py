# pyright: reportPrivateUsage=false
"""Proxy context — working directory resolution and phase reading.

Determines the project root per request (used for signal evaluation, etc.)
and provides helpers to read the current phase from the state store.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from agentalloy.api.proxy_models import ProxyRequest

logger = logging.getLogger(__name__)

UPSTREAM_FILE = Path(".agentalloy") / "upstream"

# Per-harness upstream scoping. Each harness records its own forwarding target in
# ``.agentalloy/upstream``, stored as a YAML map keyed by harness name. The native
# passthrough surfaces read ONLY their own harness's entry, so an upstream adopted
# for an OpenAI-compatible harness (qwen/cline/aider/…) can never redirect Claude
# Code or Codex away from their protocol-destination defaults.
ANTHROPIC_PASSTHROUGH_HARNESS = "claude-code"
RESPONSES_PASSTHROUGH_HARNESS = "codex"
# Harness keys that own their own upstream and are excluded from the shared
# chat-completions scope.
_PASSTHROUGH_HARNESS_KEYS = frozenset(
    {ANTHROPIC_PASSTHROUGH_HARNESS, RESPONSES_PASSTHROUGH_HARNESS}
)
# Sentinel key for the OpenAI chat-completions surface. Every non-passthrough
# harness in a repo shares one chat forwarding target; a legacy flat
# ``.agentalloy/upstream`` (pre-namespacing) is folded here on read/migrate so
# it keeps satisfying the chat surface while never capturing a passthrough.
CHAT_UPSTREAM_HARNESS = "__chat__"


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

    When both the token and ``metadata.cwd`` are present and they disagree,
    ``metadata.cwd`` (the per-request signal) wins over the token (the
    session-start snapshot), and a warning is logged. The token is captured
    once at session start from the ``settings.json`` resolved against the
    session's *initial* cwd, so a session that started in the wrong directory
    (or a continued session that predates a re-wire) carries a stale token.
    Preferring the per-request signal prevents that stale token from silently
    anchoring the proxy to the wrong repo (anomalies B1/B4/B5).
    """
    # Extract metadata.cwd (harness-supplied) up front so it can be cross-checked
    # against the token below.
    metadata_cwd: Path | None = None
    if request.metadata is not None:
        cwd = request.metadata.get("cwd")
        if cwd:
            metadata_cwd = Path(cwd)

    # 0. Highest precedence: the decoded per-repo discriminator token. Resolving
    #    from the URL means the proxy never depends on its own cwd.
    if project_dir_override is not None:
        # When both the token and metadata.cwd are present and they disagree,
        # prefer metadata.cwd (the per-request signal) over the token (the
        # session-start snapshot) and surface the mismatch. A stale token must
        # not silently override the session's actual working directory
        # (anomalies B1/B4/B5).
        if metadata_cwd is not None:
            token_real = os.path.realpath(project_dir_override)
            metadata_real = os.path.realpath(metadata_cwd)
            if metadata_real != token_real:
                logger.warning(
                    "/proj token (%s) disagrees with metadata.cwd (%s); "
                    "preferring metadata.cwd (per-request signal) over the "
                    "session-start token. The token may be stale — re-wire the "
                    "harness or restart the session in the correct directory.",
                    token_real,
                    metadata_real,
                )
                return metadata_cwd
        return project_dir_override

    # 1. metadata.cwd (harness-supplied)
    if metadata_cwd is not None:
        return metadata_cwd

    # 2. Check env var
    env_dir = os.environ.get("AGENTALLOY_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)

    # 3. Fall back to process cwd
    return Path.cwd()


def read_phase(cwd: Path) -> str | None:
    """Read the current phase for *cwd* from the SDD state store.

    Returns the phase string (e.g. "build"), or ``None`` when the repo has no
    phase recorded or the store is out of reach — see
    :func:`agentalloy.signals.skill_loader._read_phase`.
    """
    from agentalloy.signals.skill_loader import (
        _read_phase,
    )

    return _read_phase(cwd)


@dataclass(frozen=True)
class Upstream:
    """A harness's captured upstream LLM, read from ``.agentalloy/upstream``.

    ``url`` and ``model`` are what the proxy forwards to; ``key_env`` is the
    *name* of the environment variable holding the upstream API key (never the
    secret itself — the proxy resolves it from its own process env at request
    time, so no credential is written into the repo).

    ``normalize_system`` overrides the Anthropic passthrough's system-message
    normalization (see ``_should_normalize_system``): ``None`` means "decide by
    upstream host", ``True``/``False`` force it on/off.
    """

    url: str
    model: str
    key_env: str | None = None
    normalize_system: bool | None = None


@dataclass(frozen=True)
class UpstreamFile:
    """Result of reading a per-repo ``.agentalloy/upstream`` file.

    The ``kind`` discriminant distinguishes three states:

    * ``"absent"`` — no per-repo upstream configured (caller may fall back to
      the global upstream).
    * ``"valid"`` — the file parsed successfully; :attr:`upstream` holds the
      parsed :class:`Upstream`.
    * ``"error"`` — the file exists but could not be used; :attr:`detail`
      describes the specific failure (useful for error responses and logs).
    """

    kind: Literal["absent", "valid", "error"]
    upstream: Upstream | None = None
    detail: str | None = None


def _parse_upstream_entry(entry: dict[str, object], path: Path) -> UpstreamFile:
    """Parse a single harness's upstream mapping into an ``UpstreamFile``.

    Mirrors the pre-namespacing validation: ``url`` and ``model`` are required,
    ``key_env``/``normalize_system`` are optional. A missing/invalid ``url`` or
    ``model`` is an error (never silently absent).
    """
    url = entry.get("url")
    model = entry.get("model")
    if not isinstance(url, str) or not url or not isinstance(model, str) or not model:
        return UpstreamFile(kind="error", detail=f"{path} missing required url/model")

    key_env_raw = entry.get("key_env")
    key_env = key_env_raw if isinstance(key_env_raw, str) and key_env_raw else None
    normalize_raw = entry.get("normalize_system")
    normalize_system = normalize_raw if isinstance(normalize_raw, bool) else None

    return UpstreamFile(
        kind="valid",
        upstream=Upstream(
            url=url.rstrip("/"),
            model=model,
            key_env=key_env,
            normalize_system=normalize_system,
        ),
    )


def read_upstream(cwd: Path, *, harness: str | None = None) -> UpstreamFile:
    """Read the captured upstream for *harness* from ``cwd/.agentalloy/upstream``.

    The file is YAML, written by ``agentalloy add <harness>`` as a map keyed by
    harness so each harness carries its own forwarding target::

        claude-code:                 # native Anthropic passthrough (optional)
          url: https://api.anthropic.com
          model: claude-3-sonnet
        qwen-code:                   # OpenAI chat-completions harness
          url: http://100.115.181.90:60011/v1
          model: mannix-coder-q6
          key_env: OPENAI_API_KEY

    Args:
        cwd: The project (repo) directory.
        harness: The harness whose upstream is wanted. ``None`` (or
            :data:`CHAT_UPSTREAM_HARNESS`) selects the repo's shared *chat*
            scope — the first non-passthrough-harness entry. A legacy flat file
            (``url``/``model`` at the top level) is read as the chat scope and is
            **never** returned for a passthrough harness, so an upstream adopted
            by a chat harness can't redirect Claude Code / Codex.

    Returns an :class:`UpstreamFile`:
    * ``kind == "absent"`` — no entry for *harness* (use the default).
    * ``kind == "valid"`` — *harness* has ``upstream``.
    * ``kind == "error"`` — the file (or the harness's entry) is malformed.

    Never raises on a bad file — a per-repo override must never take down the
    proxy.
    """
    path = cwd / UPSTREAM_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return UpstreamFile(kind="absent")
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
        return UpstreamFile(kind="error", detail=f"could not read {path}: {e}")

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("malformed %s: %s", path, e)
        return UpstreamFile(kind="error", detail=f"malformed {path}: {e}")
    if not isinstance(parsed, dict):
        logger.warning("%s is not a YAML mapping", path)
        return UpstreamFile(kind="error", detail=f"{path} is not a YAML mapping")
    data = cast("dict[str, Any]", parsed)

    # Legacy flat shape (url/model at the top level) = a chat/generic upstream.
    # It satisfies the chat surface but must never override a passthrough.
    if "url" in data and isinstance(data["url"], str):
        if harness in _PASSTHROUGH_HARNESS_KEYS:
            return UpstreamFile(kind="absent")
        return _parse_upstream_entry(data, path)

    # Namespaced shape: mapping of harness -> {url, model, key_env, ...}.
    requested = harness
    if requested is None or requested == CHAT_UPSTREAM_HARNESS:
        requested = next(
            (key for key in data if key not in _PASSTHROUGH_HARNESS_KEYS), None
        )
        if requested is None:
            return UpstreamFile(kind="absent")
    entry = data.get(requested)
    if entry is None:
        return UpstreamFile(kind="absent")
    if not isinstance(entry, dict):
        return UpstreamFile(kind="error", detail=f"{path}[{requested}] is not a mapping")
    return _parse_upstream_entry(cast("dict[str, object]", entry), path)
