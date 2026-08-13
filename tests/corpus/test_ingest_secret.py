"""T3 — ingest-secret provisioning.

The shared secret that authenticates ``POST /corpus/ingest-pack`` (AC-7). Both
the service (compare) and the CLI (send) resolve it through one module; native
persists it under ``${XDG_CONFIG_HOME}/agentalloy/ingest-secret`` and the
container receives the same value via the ``AGENTALLOY_INGEST_SECRET`` env var.
"""

from __future__ import annotations

import os
import stat

import pytest

from agentalloy.install import ingest_secret
from agentalloy.install.state import user_config_dir


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point XDG_CONFIG_HOME at a temp dir and clear the env override."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(ingest_secret.SECRET_ENV, raising=False)
    return tmp_path


def test_native_mint_and_read_roundtrip():
    """Mint persists a secret; both a service read and a CLI read return it."""
    minted = ingest_secret.mint_ingest_secret()
    assert minted
    # Two independent resolves (service side, CLI side) see the same value.
    assert ingest_secret.resolve_ingest_secret() == minted
    assert ingest_secret.resolve_ingest_secret() == minted


def test_secret_file_is_0600():
    ingest_secret.mint_ingest_secret()
    path = user_config_dir() / ingest_secret.SECRET_FILE_NAME
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_reuse_existing_secret_not_overwritten():
    first = ingest_secret.mint_ingest_secret()
    second = ingest_secret.mint_ingest_secret()
    assert first == second, "mint must reuse an existing secret, never rotate it"


def test_env_var_overrides_file():
    """Container path: AGENTALLOY_INGEST_SECRET in the env wins over any file."""
    ingest_secret.mint_ingest_secret()  # a file secret exists...
    os.environ[ingest_secret.SECRET_ENV] = "container-injected-value"
    try:
        assert ingest_secret.resolve_ingest_secret() == "container-injected-value"
    finally:
        del os.environ[ingest_secret.SECRET_ENV]


def test_resolve_no_mint_returns_none_when_absent():
    """Absent secret + mint=False → None (fail-closed; caller decides)."""
    assert ingest_secret.resolve_ingest_secret(mint=False) is None


def test_resolve_mint_true_generates_when_absent():
    got = ingest_secret.resolve_ingest_secret(mint=True)
    assert got
    assert ingest_secret.resolve_ingest_secret(mint=False) == got


def test_constant_time_compare():
    """The endpoint's guard uses this; it must accept the match and reject others."""
    secret = "abc123"
    assert ingest_secret.secret_matches(secret, secret) is True
    assert ingest_secret.secret_matches(secret, "abc124") is False
    assert ingest_secret.secret_matches(secret, None) is False
    assert ingest_secret.secret_matches(None, "anything") is False
