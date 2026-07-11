"""Shared secret for the service-mediated corpus-ingest endpoint (T3).

``POST /corpus/ingest-pack`` mutates the live corpus, and on a container
deployment the service is published on ``0.0.0.0`` — reachable across the
LAN/tailscale. The proxy ``/proj/{token}`` scheme is *not* auth (the token is
``base64url(realpath(project_dir))``, publicly derivable), so the endpoint needs
a real secret. This module is the single source of truth for it, read by both
sides:

- the **service** reads it to compare against the request's
  ``X-AgentAlloy-Ingest-Token`` header (:func:`secret_matches`);
- the **CLI** reads it to send that header.

The host is authoritative: the secret is minted once and persisted under
``${XDG_CONFIG_HOME:-~/.config}/agentalloy/ingest-secret`` (0600). A container
receives the *same* value via the ``AGENTALLOY_INGEST_SECRET`` env var injected
at ``podman run`` (so the in-container service and the host CLI converge without
the host needing to read inside the volume). The env var therefore always wins
over the file.
"""

from __future__ import annotations

import hmac
import os
import secrets

from agentalloy.install.state import user_config_dir

SECRET_ENV = "AGENTALLOY_INGEST_SECRET"
SECRET_FILE_NAME = "ingest-secret"
_TOKEN_BYTES = 32  # token_urlsafe(32) -> ~43 url-safe chars


def _secret_path():
    return user_config_dir() / SECRET_FILE_NAME


def _read_file_secret() -> str | None:
    path = _secret_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return value or None


def mint_ingest_secret() -> str:
    """Return the ingest secret, generating and persisting it if absent.

    Idempotent: an existing secret is reused, never rotated — rotating would
    401 every already-configured client. Written atomically at 0600.
    """
    existing = _read_file_secret()
    if existing:
        return existing

    path = _secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(_TOKEN_BYTES)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # 0600 from creation — never briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)  # replace preserves tmp's mode; explicit for clarity
    return value


def resolve_ingest_secret(*, mint: bool = False) -> str | None:
    """Resolve the secret both sides share.

    Order: ``AGENTALLOY_INGEST_SECRET`` env (the container-injected value) →
    the on-disk file → ``None``. With ``mint=True`` a missing file secret is
    generated and persisted (host source-of-truth path); ``mint=False`` is
    fail-closed and returns ``None`` so the caller decides.
    """
    env_value = os.environ.get(SECRET_ENV)
    if env_value and env_value.strip():
        return env_value.strip()
    file_value = _read_file_secret()
    if file_value:
        return file_value
    if mint:
        return mint_ingest_secret()
    return None


def secret_matches(expected: str | None, provided: str | None) -> bool:
    """Constant-time equality for the endpoint guard.

    Both-present required: a ``None`` on either side is a non-match (an
    unconfigured service must not accept an empty token, and an absent header
    must not match).
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)
