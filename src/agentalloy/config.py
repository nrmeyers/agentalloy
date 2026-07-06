"""Runtime configuration loaded from environment."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AuthoringConfig", "Settings", "configure_logging", "get_settings"]

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
    embedding_model: str = "nomic-embed-text-v1.5.Q8_0.gguf"


def _user_corpus_dir() -> Path:
    """Default corpus location (XDG data dir). Mirrors install.state.corpus_dir.

    Duplicated here so the runtime service has no dependency on the install
    module — `config` is imported by every part of the service.

    Resolved per-call (not cached) so a process that adjusts XDG_DATA_HOME
    after import (e.g. tests) sees the correct location.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy" / "corpus"


def _user_code_index_dir() -> Path:
    """Default code-index data root (XDG data dir), sibling of the corpus dir."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy" / "code_index"


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
    # v5 two-engine storage. ``agentalloy.duck`` holds skill metadata (folded
    # out of the retired graph engine) + corpus_meta kv; the serving process opens
    # it READ-ONLY so ingest/reembed can hold its single writer lock without
    # stopping the service. ``fragments.lance`` is the Lance dataset (vector ANN
    # + exact-cosine dedup + native BM25). Telemetry lives in its own
    # service-owned ``telemetry.duck`` so runtime trace writes never contend with
    # the reembed writer. Env: DUCKDB_PATH, FRAGMENTS_LANCE_PATH, TELEMETRY_DB_PATH.
    duckdb_path: str = Field(default_factory=lambda: str(_user_corpus_dir() / "agentalloy.duck"))
    fragments_lance_path: str = Field(
        default_factory=lambda: str(_user_corpus_dir() / "fragments.lance")
    )
    telemetry_db_path: str = Field(
        default_factory=lambda: str(_user_corpus_dir() / "telemetry.duck")
    )
    log_level: str = "INFO"

    # Runtime serving (retrieve / compose). The runtime path holds zero
    # generative LLM dependency — only an embedding service.
    runtime_embed_base_url: str = "http://localhost:47951"
    runtime_embedding_model: str = "nomic-embed-text-v1.5.Q8_0.gguf"
    embedding_provider: str = "openai_compat"
    dedup_hard_threshold: float = 0.92
    dedup_soft_threshold: float = 0.80
    bounce_budget: int = 3

    # Human-approval gate on the sdd-fast lane (spec/design are always gated).
    # Bare-name env mapping ⇒ SDD_FAST_REQUIRE_APPROVAL. Default OFF: the fast
    # lane stays ungated unless an operator opts in.
    sdd_fast_require_approval: bool = False

    # Upstream LLM — the generative model the proxy forwards chat completions to.
    # Env vars: UPSTREAM_URL, UPSTREAM_MODEL, UPSTREAM_API_KEY (bare names, no prefix).
    upstream_url: str = ""
    upstream_model: str = ""
    upstream_api_key: str = ""

    # Native Anthropic passthrough upstream (the /proj/<token>/v1/messages path).
    # Distinct from upstream_url: this path forwards the caller's OWN credential
    # verbatim and stores none of its own. Point it at another proxy to chain
    # (Claude Code → AgentAlloy → … → Anthropic). Env: ANTHROPIC_UPSTREAM_URL.
    anthropic_upstream_url: str = "https://api.anthropic.com"

    # Native OpenAI Responses passthrough upstream (the /proj/<token>/v1/responses
    # path — codex et al.). Same auth-transparent contract as the Anthropic
    # passthrough: the caller's own credential is forwarded verbatim.
    # Env: RESPONSES_UPSTREAM_URL. Spec: docs/responses-surface.md.
    responses_upstream_url: str = "https://api.openai.com"

    # Profile root. Resolves to ~/.agentalloy by default.
    profile_root: str = Field(default_factory=lambda: str(Path.home() / ".agentalloy"))

    # When set, overrides auto-detection (useful for tests).
    forced_profile: str | None = None

    # Module toggles. AgentAlloy serves independent context modules from one
    # process: the instruction injector (compose/retrieve/proxy — the original
    # surface) and the codebase indexer (/code/*). Routers for a disabled
    # module are never registered; a disabled code_index module never imports
    # tree-sitter. Env: COMPOSE_ENABLED, CODE_INDEX_ENABLED.
    compose_enabled: bool = True
    code_index_enabled: bool = False
    # Per-repo index data (graph.duck + vectors.lance per slug, jobs.sqlite)
    # lives outside corpus/ — corpus is the global skill store, code_index is
    # per-repo derived data that is rebuilt, never migrated. Env: CODE_INDEX_DATA_DIR.
    code_index_data_dir: str = Field(default_factory=lambda: str(_user_code_index_dir()))
    # Watchdog-driven incremental reindex (off by default). Env: CODE_INDEX_WATCH.
    code_index_watch: bool = False

    @field_validator("upstream_url")
    @classmethod
    def _normalize_upstream_url(cls, v: str) -> str:
        """Normalize UPSTREAM_URL to the server root.

        The proxy client posts the relative path ``/v1/chat/completions``
        against this URL, so a value that already ends in ``/v1`` (the common
        OpenAI-style base URL) would silently produce ``/v1/v1/...`` → 404.
        Accept both forms by stripping a trailing ``/v1``.
        """
        stripped = v.rstrip("/")
        if stripped.endswith("/v1"):
            old = v
            stripped = stripped[: -len("/v1")].rstrip("/")
            logger.warning(
                "UPSTREAM_URL %r ends in /v1; using %r — the proxy appends /v1/... itself.",
                old,
                stripped,
            )
        return stripped

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
        """Create the corpus directory holding both storage engines if missing.

        Lance creates its own dataset directory on first write; we only ensure
        its parent corpus dir exists. The two DuckDB files need their parent
        present before open.
        """
        Path(self.duckdb_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.telemetry_db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.fragments_lance_path).parent.mkdir(parents=True, exist_ok=True)
        # Only materialize the code-index root when the module is on — a
        # disabled module should leave no trace on disk.
        if self.code_index_enabled:
            Path(self.code_index_data_dir).mkdir(parents=True, exist_ok=True)

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


def configure_logging(level: str | None = None) -> None:
    """Install a root handler and pin the ``agentalloy`` namespace to LOG_LEVEL.

    Called at the top of ``create_app`` (and ``__main__``) so every entrypoint —
    ``python -m agentalloy``, ``uvicorn agentalloy.app:app`` (systemd/launchd),
    and the container's ``uv run uvicorn`` — applies ``LOG_LEVEL`` to the
    ``agentalloy.*`` loggers. uvicorn's ``--log-level`` only touches the
    ``uvicorn.*`` loggers; this fills the missing piece.

    Idempotent: ``basicConfig`` installs at most one root handler (at NOTSET, so
    it passes every record it receives); the explicit ``setLevel`` re-applies on
    each call so a later ``create_app`` with a changed ``LOG_LEVEL`` still takes
    effect, and wins even when uvicorn or pytest installed a handler first
    (uvicorn's dictConfig has no ``root`` key and ``disable_existing_loggers=
    False``, so it never touches the ``agentalloy`` logger).
    """
    name = (level or get_settings().log_level).upper()
    lvl = getattr(logging, name, logging.INFO)
    logging.basicConfig(level=lvl, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("agentalloy").setLevel(lvl)


def get_settings() -> Settings:
    """Load settings and log which values came from defaults."""
    s = Settings()
    env_keys = _env_keys()
    for field in Settings.model_fields:
        source = "env" if field.upper() in env_keys else "default"
        logger.debug("config %s=%r source=%s", field, getattr(s, field), source)
    return s


def _env_keys() -> set[str]:
    return set(os.environ.keys())
