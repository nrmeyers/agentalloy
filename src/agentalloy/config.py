"""Environment-driven configuration.

Two layers coexist (v11 merge — v2 execution stack + v10 product shell):

- ``Config`` (frozen dataclass, ``AGENTALLOY_*`` env prefix): the v2
  execution-stack surface — service/embed/proxy/model ports, model names,
  interpreter budgets, retrieval sizes, state paths.
- ``Settings`` (pydantic-settings, bare-name env): the v10 product-shell
  surface — corpus store, telemetry, logging, upstream/proxy, module
  toggles. Loaded via ``get_settings()``; logging via ``configure_logging``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from os import environ
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Config", "Settings", "configure_logging", "get_settings"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v2 execution stack (AGENTALLOY_* env prefix)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """All ports/paths overridable via environment variables."""

    # Service ports (final — the v1 :47950-2 flip never happened)
    service_port: int = 48950
    embed_port: int = 48951
    rerank_port: int = 48952
    model_port: int = 50001

    # Model names. `model` is the MAIN session model the proxy forwards to
    # (upstream); `interp_model` is the local orchestrator sidecar on
    # model_port. llama-server ignores the name in single-model mode, so
    # interp_model is mostly cosmetic — but keep them distinct: they are
    # different models with different roles.
    model: str = "lfm2.5-2.6b"
    interp_model: str = "lfm2.5-2.6b-compressor"
    embed_model: str = "LFM2.5-Embedding-350M"
    rerank_model: str = "LFM2.5-ColBERT-350M"

    # Interpreter budgets (4 = skill selection flow: pack catalog →
    # skills+types → assemble_skill, then the brief round)
    max_steps: int = 6
    hard_cap: int = 6
    max_tokens: int = 2048
    thinking_budget: int = 1024

    # Retrieval
    search_k: int = 10
    skill_candidates: int = 30
    instruction_budget_chars: int = 24000

    # Proxy
    proxy_port: int = 48953
    upstream_url: str = "http://localhost:50001"
    # Placeholder for the local llama server (which ignores auth). Real keys
    # come from AGENTALLOY_UPSTREAM_KEY only — never commit one as a default.
    upstream_key: str = "sk-local"

    # Derived URLs
    @property
    def embed_url(self) -> str:
        return f"http://localhost:{self.embed_port}"

    @property
    def model_key(self) -> str:
        return self.upstream_key

    # Paths
    repo_root: str = "."
    extra_repos: str = ""  # comma-separated list of additional repos
    state_duck: str = "./state.duck"
    usage_duck: str = "./usage.duck"
    index_dir: str = "./index"
    corpus_dir: str = "./corpus"

    @classmethod
    def from_env(cls) -> Config:
        """Load config from environment variables (AGENTALLOY_* prefix)."""
        return cls(
            service_port=int(environ.get("AGENTALLOY_SERVICE_PORT", "48950")),
            embed_port=int(environ.get("AGENTALLOY_EMBED_PORT", "48951")),
            rerank_port=int(environ.get("AGENTALLOY_RERANK_PORT", "48952")),
            model_port=int(environ.get("AGENTALLOY_MODEL_PORT", "50001")),
            model=environ.get("AGENTALLOY_MODEL", "lfm2.5-2.6b"),
            interp_model=environ.get("AGENTALLOY_INTERP_MODEL", "lfm2.5-2.6b-compressor"),
            embed_model=environ.get("AGENTALLOY_EMBED_MODEL", "LFM2.5-Embedding-350M"),
            rerank_model=environ.get("AGENTALLOY_RERANK_MODEL", "LFM2.5-ColBERT-350M"),
            max_steps=int(environ.get("AGENTALLOY_MAX_STEPS", "6")),
            hard_cap=int(environ.get("AGENTALLOY_HARD_CAP", "6")),
            max_tokens=int(environ.get("AGENTALLOY_MAX_TOKENS", "2048")),
            thinking_budget=int(environ.get("AGENTALLOY_THINKING_BUDGET", "1024")),
            search_k=int(environ.get("AGENTALLOY_SEARCH_K", "10")),
            skill_candidates=int(environ.get("AGENTALLOY_SKILL_CANDIDATES", "30")),
            instruction_budget_chars=int(
                environ.get("AGENTALLOY_INSTRUCTION_BUDGET_CHARS", "24000")
            ),
            proxy_port=int(environ.get("AGENTALLOY_PROXY_PORT", "48953")),
            upstream_url=environ.get("AGENTALLOY_UPSTREAM_URL", "http://localhost:50001"),
            upstream_key=environ.get("AGENTALLOY_UPSTREAM_KEY", "sk-local"),
            repo_root=environ.get("AGENTALLOY_REPO_ROOT", "."),
            extra_repos=environ.get("AGENTALLOY_EXTRA_REPOS", ""),
            state_duck=environ.get("AGENTALLOY_STATE_DUCK", "./state.duck"),
            usage_duck=environ.get("AGENTALLOY_USAGE_DUCK", "./usage.duck"),
            index_dir=environ.get("AGENTALLOY_INDEX_DIR", "./index"),
            corpus_dir=environ.get("AGENTALLOY_CORPUS_DIR", "./corpus"),
        )


# ---------------------------------------------------------------------------
# v10 product shell (bare-name env, pydantic-settings)
# ---------------------------------------------------------------------------


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
    # OverGraph unified corpus store: skills, versions, fragments, dependencies,
    # and fragment embeddings (HNSW) in one embedded graph DB, plus a Tantivy
    # BM25 sidecar for keyword search. The serving process opens it READ-ONLY
    # so reembed can hold its single writer lock without stopping the service.
    # Telemetry lives in its own service-owned ``telemetry.duck`` so runtime
    # trace writes never contend with the reembed writer.
    # Env: CORPUS_STORE_PATH, TELEMETRY_DB_PATH.
    corpus_store_path: str = Field(
        default_factory=lambda: str(_user_corpus_dir() / "agentalloy.overgraph")
    )
    telemetry_db_path: str = Field(
        default_factory=lambda: str(_user_corpus_dir() / "telemetry.duck"),
    )
    log_level: str = "INFO"

    # Runtime serving (retrieve / compose). The runtime path holds zero
    # generative LLM dependency — only an embedding service.
    # D2 (v11 merge): the embedding service moved to v2's :48951; v1's
    # :47951 retires.
    runtime_embed_base_url: str = "http://localhost:48951"
    runtime_embedding_model: str = "nomic-embed-text-v1.5.Q8_0.gguf"
    embedding_provider: str = "openai_compat"
    dedup_hard_threshold: float = 0.92
    dedup_soft_threshold: float = 0.80

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
    # tree-sitter. The Knowledge module (decision-graph linkage, `agentalloy
    # knowledge why`) rides the same router and store as code_index — there is
    # no independent toggle for it; CODE_INDEX_ENABLED covers both. Env:
    # COMPOSE_ENABLED, CODE_INDEX_ENABLED.
    compose_enabled: bool = True
    code_index_enabled: bool = False
    # Per-repo index data (graph.overgraph per slug, jobs.sqlite) lives outside
    # corpus/ — corpus is the global skill store, code_index is per-repo derived
    # data that is rebuilt, never migrated. Env: CODE_INDEX_DATA_DIR.
    code_index_data_dir: str = Field(default_factory=lambda: str(_user_code_index_dir()))
    # Watchdog-driven incremental reindex (off by default). Env: CODE_INDEX_WATCH.
    code_index_watch: bool = False
    # Periodic staleness-driven incremental reindex, in seconds; 0 disables. A
    # background lifespan task compares each registry repo's HEAD to its indexed
    # sha and kicks a non-force job for the drifted ones, so the index self-heals
    # between manual runs. Off in code/dev/tests; the container entrypoint opts in
    # (300). Env: CODE_INDEX_REFRESH_SECONDS.
    code_index_refresh_seconds: int = 0
    # JIT push phase 2: merge related (thematic) decisions via
    # ``related_decisions(task_title)`` into the decision block.  Measured
    # median overhead ≈ 4 ms (p95 ≈ 5 ms) — well within the 300 ms compose
    # budget. Env: KNOWLEDGE_RELATED_ENABLED.
    knowledge_related_enabled: bool = True

    # Artifact extraction: parse <!-- agentalloy:artifact --> markers from LLM
    # responses and write them to the store. Off by default during rollout.
    # Env: ARTIFACT_EXTRACTION_ENABLED.
    artifact_extraction_enabled: bool = False

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

    def ensure_data_dirs(self) -> None:
        """Create the corpus directory if missing.

        OverGraph creates its own store directory on first open; we only
        ensure the parent corpus dir exists (the telemetry DuckDB file needs
        its parent present before open).
        """
        Path(self.corpus_store_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.telemetry_db_path).parent.mkdir(parents=True, exist_ok=True)
        # Only materialize the code-index root when the module is on — a
        # disabled module should leave no trace on disk.
        if self.code_index_enabled:
            Path(self.code_index_data_dir).mkdir(parents=True, exist_ok=True)


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
