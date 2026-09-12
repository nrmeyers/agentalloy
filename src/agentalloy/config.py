"""Environment-driven configuration."""

from dataclasses import dataclass
from os import environ


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
    interp_model: str = "minicpm5-2b"
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
    def from_env(cls) -> "Config":
        """Load config from environment variables (AGENTALLOY_* prefix)."""
        return cls(
            service_port=int(environ.get("AGENTALLOY_SERVICE_PORT", "48950")),
            embed_port=int(environ.get("AGENTALLOY_EMBED_PORT", "48951")),
            rerank_port=int(environ.get("AGENTALLOY_RERANK_PORT", "48952")),
            model_port=int(environ.get("AGENTALLOY_MODEL_PORT", "50001")),
            model=environ.get("AGENTALLOY_MODEL", "lfm2.5-2.6b"),
            interp_model=environ.get("AGENTALLOY_INTERP_MODEL", "minicpm5-2b"),
            embed_model=environ.get("AGENTALLOY_EMBED_MODEL", "LFM2.5-Embedding-350M"),
            rerank_model=environ.get("AGENTALLOY_RERANK_MODEL", "LFM2.5-ColBERT-350M"),
            max_steps=int(environ.get("AGENTALLOY_MAX_STEPS", "6")),
            hard_cap=int(environ.get("AGENTALLOY_HARD_CAP", "6")),
            max_tokens=int(environ.get("AGENTALLOY_MAX_TOKENS", "2048")),
            thinking_budget=int(environ.get("AGENTALLOY_THINKING_BUDGET", "1024")),
            search_k=int(environ.get("AGENTALLOY_SEARCH_K", "10")),
            skill_candidates=int(environ.get("AGENTALLOY_SKILL_CANDIDATES", "30")),
            instruction_budget_chars=int(environ.get("AGENTALLOY_INSTRUCTION_BUDGET_CHARS", "24000")),
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
