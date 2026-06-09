"""Runtime configuration loaded from environment."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AuthoringConfig", "Settings", "get_settings"]

logger = logging.getLogger(__name__)


class AuthoringConfig(BaseSettings):
    """Authoring pipeline configuration loaded from environment.

    Env var prefix: ``AUTHORING_`` (e.g. ``AUTHORING_MODEL``).
    """

    model_config = SettingsConfigDict(
        env_prefix="AUTHORING_",
        env_file=".env",
        extra="ignore",
    )

    model: str = "qwen3-14b-instruct"
    critic_model: str = "qwen3.6-27b"
    lm_base_url: str = "http://localhost:11435"
    lm_studio_base_url: str = "http://localhost:11434"
    embed_base_url: str = "http://localhost:11436"
    embedding_model: str = "qwen3-embedding:0.6b"


def _user_corpus_dir() -> Path:
    """Default corpus location (XDG data dir). Mirrors install.state.corpus_dir.

    Duplicated here so the runtime service has no dependency on the install
    module — `config` is imported by every part of the service.

    Resolved per-call (not cached) so a process that adjusts XDG_DATA_HOME
    after import (e.g. tests) sees the correct location.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy" / "corpus"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # No env_file — config comes from process environment. The
        # user-scoped .env produced by `write-env` lives at
        # `${XDG_CONFIG_HOME}/agentalloy/.env`; operators source it into
        # the service's process env (or a `agentalloy serve` wrapper does
        # it for them). A project-local `.env` in cwd is intentionally
        # NOT loaded — AgentAlloy state is user-scoped, not per-repo.
        extra="ignore",
    )

    # `default_factory` defers evaluation to instantiation time so a
    # process that sets XDG_DATA_HOME after `agentalloy.config` is
    # imported (or in test environments that monkeypatch the env var)
    # gets the correct path. With a plain `default=...` the path would
    # be frozen at module import.
    ladybug_db_path: str = Field(default_factory=lambda: str(_user_corpus_dir() / "ladybug"))
    duckdb_path: str = Field(default_factory=lambda: str(_user_corpus_dir() / "skills.duck"))
    log_level: str = "INFO"

    # Runtime serving (retrieve / compose). The runtime path holds zero
    # generative LLM dependency — only an embedding service.
    runtime_embed_base_url: str = "http://localhost:11434"
    runtime_embedding_model: str = "qwen3-embedding:0.6b"
    embedding_provider: str = "openai_compat"
    dedup_hard_threshold: float = 0.92
    dedup_soft_threshold: float = 0.80
    bounce_budget: int = 3

    # Upstream LLM — the generative model the proxy forwards chat completions to.
    # Env vars: UPSTREAM_URL, UPSTREAM_MODEL, UPSTREAM_API_KEY (bare names, no prefix).
    upstream_url: str = ""
    upstream_model: str = ""
    upstream_api_key: str = ""

    # Profile root. Resolves to ~/.agentalloy by default.
    profile_root: str = Field(default_factory=lambda: str(Path.home() / ".agentalloy"))

    # When set, overrides auto-detection (useful for tests).
    forced_profile: str | None = None

    code_indexer_url: str = "http://127.0.0.1:8003"

    def upstream_configured(self) -> bool:
        """Return True when upstream URL and model are set.

        API key is optional — local runners don't need one.
        """
        return bool(self.upstream_url and self.upstream_model)

    def active_datastore_path(self, cwd: Path | None = None) -> Path:
        """Return the skills.duck for the active profile.

        Falls back to the legacy ``duckdb_path`` when the active profile has
        no datastore yet (first-run grace).
        """
        try:
            from agentalloy.profiles import detect_profile, profile_datastore_path

            if self.forced_profile:
                return profile_datastore_path(self.forced_profile)
            profile = detect_profile(cwd)
            candidate = profile.datastore_path
            if candidate.exists():
                return candidate
        except Exception:
            pass
        return Path(self.duckdb_path)

    def ensure_data_dirs(self) -> None:
        """Create parent directories for LadybugDB and DuckDB if missing."""
        Path(self.ladybug_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.duckdb_path).parent.mkdir(parents=True, exist_ok=True)

    def require_authoring_config(self) -> AuthoringConfig:
        """Extract authoring config from environment.

        Loads AuthoringConfig which reads AUTHORING_* env vars.
        Raises RuntimeError if required fields are missing or empty.
        """
        ac = AuthoringConfig()
        required = {
            "model": ac.model,
            "critic_model": ac.critic_model,
            "lm_base_url": ac.lm_base_url,
            "lm_studio_base_url": ac.lm_studio_base_url,
            "embed_base_url": ac.embed_base_url,
            "embedding_model": ac.embedding_model,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"Authoring config incomplete — missing AUTHORING_ env vars for: "
                f"{', '.join(missing)}. Source ~/.config/agentalloy/.env or set them manually."
            )
        return ac


def get_settings() -> Settings:
    """Load settings and log which values came from defaults."""
    s = Settings()
    for field in Settings.model_fields:
        source = "env" if field.upper() in _env_keys() else "default"
        logger.debug("config %s=%r source=%s", field, getattr(s, field), source)
    return s


def _env_keys() -> set[str]:
    return set(os.environ.keys())
