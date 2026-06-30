"""AC-4: config defaults applied when env vars missing; env-var values override."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.config import Settings

_ENV_KEYS = (
    "RUNTIME_EMBED_BASE_URL",
    "DUCKDB_PATH",
    "FRAGMENTS_LANCE_PATH",
    "TELEMETRY_DB_PATH",
    "RUNTIME_EMBEDDING_MODEL",
    "UPSTREAM_URL",
    "UPSTREAM_MODEL",
    "UPSTREAM_API_KEY",
)


def test_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # XDG_DATA_HOME is read per-instantiation via default_factory, so no
    # module reload is needed.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_xdg_data"))
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.runtime_embed_base_url == "http://localhost:47951"
    # v5 two-engine storage: ladybug_db_path is gone; the skill DuckDB is now
    # agentalloy.duck, with the Lance fragment dataset + telemetry DuckDB alongside.
    expected_corpus = str(tmp_path / "_xdg_data" / "agentalloy" / "corpus")
    assert s.duckdb_path == f"{expected_corpus}/agentalloy.duck"
    assert s.fragments_lance_path == f"{expected_corpus}/fragments.lance"
    assert s.telemetry_db_path == f"{expected_corpus}/telemetry.duck"
    assert s.runtime_embedding_model == "nomic-embed-text-v1.5.Q8_0.gguf"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNTIME_EMBED_BASE_URL", "http://embed.internal:52625")
    monkeypatch.setenv("DUCKDB_PATH", "/var/lib/agentalloy.duck")
    s = Settings()
    assert s.runtime_embed_base_url == "http://embed.internal:52625"
    assert s.duckdb_path == "/var/lib/agentalloy.duck"


# ---------------------------------------------------------------------------
# Upstream LLM configuration tests
# ---------------------------------------------------------------------------


def test_upstream_defaults_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Upstream fields default to empty strings when env vars are unset."""
    for key in ("UPSTREAM_URL", "UPSTREAM_MODEL", "UPSTREAM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.upstream_url == ""
    assert s.upstream_model == ""
    assert s.upstream_api_key == ""


def test_upstream_configured_false_when_all_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """upstream_configured() returns False when all three vars are unset."""
    for key in ("UPSTREAM_URL", "UPSTREAM_MODEL", "UPSTREAM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.upstream_configured() is False


def test_upstream_configured_false_when_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API key is optional — URL + model is enough for upstream_configured()."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTREAM_URL", "http://localhost:2099")
    monkeypatch.setenv("UPSTREAM_MODEL", "my-model")
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    s = Settings()
    # API key is optional — URL + model is sufficient
    assert s.upstream_configured() is True


def test_upstream_configured_false_when_url_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """upstream_configured() returns False when URL is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UPSTREAM_URL", raising=False)
    monkeypatch.setenv("UPSTREAM_MODEL", "my-model")
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-test")
    s = Settings()
    assert s.upstream_configured() is False


def test_upstream_configured_false_when_model_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """upstream_configured() returns False when model is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTREAM_URL", "http://localhost:2099")
    monkeypatch.delenv("UPSTREAM_MODEL", raising=False)
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-test")
    s = Settings()
    assert s.upstream_configured() is False


def test_upstream_configured_true_when_all_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """upstream_configured() returns True when all three vars are set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTREAM_URL", "http://localhost:2099")
    monkeypatch.setenv("UPSTREAM_MODEL", "qwen3-14b")
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-test")
    s = Settings()
    assert s.upstream_configured() is True


def test_upstream_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Upstream fields are read correctly from env vars."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTREAM_URL", "http://llm.internal:8080/v1")
    monkeypatch.setenv("UPSTREAM_MODEL", "llama3")
    monkeypatch.setenv("UPSTREAM_API_KEY", "bearer-token-abc")
    s = Settings()
    assert s.upstream_url == "http://llm.internal:8080/v1"
    assert s.upstream_model == "llama3"
    assert s.upstream_api_key == "bearer-token-abc"
    assert s.upstream_configured() is True


def test_upstream_configured_false_when_api_key_empty_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API key is optional — upstream_configured() returns True with just URL + model."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTREAM_URL", "http://localhost:2099")
    monkeypatch.setenv("UPSTREAM_MODEL", "my-model")
    monkeypatch.setenv("UPSTREAM_API_KEY", "")
    s = Settings()
    # API key is optional — URL + model is enough
    assert s.upstream_configured() is True
