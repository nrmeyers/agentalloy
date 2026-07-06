"""Wiring ↔ proxy route contract.

The harness wirings hand SDKs a base URL; each SDK appends its own request
path. These tests assert that (base URL path) + (SDK-appended path) lands on
a route the app actually serves — the failure mode being guarded was
ANTHROPIC_BASE_URL ending in /v1, which produced /v1/v1/messages (404).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from agentalloy.providers import REGISTRY


def _app_route_paths() -> set[str]:
    from agentalloy.app import create_app

    app = create_app()
    return {getattr(r, "path", "") for r in app.routes}


def _base_path(env: dict[str, str], key: str) -> str:
    return urlparse(env[key]).path.rstrip("/")


def test_claude_code_base_url_resolves_to_messages_route() -> None:
    env = REGISTRY["claude-code"].env_builder(47950)
    base = _base_path(env, "ANTHROPIC_BASE_URL")  # /proj/<token>
    assert base.startswith("/proj/"), f"expected a /proj/<token> base, got {base!r}"
    # The Anthropic SDK requests {base}/v1/messages. The native passthrough route
    # is templated on the discriminator, so normalize the concrete token segment
    # back to {token} before matching (guards the old /v1/v1/messages 404 bug).
    parts = (base + "/v1/messages").split("/")
    parts[2] = "{token}"
    assert "/".join(parts) in _app_route_paths()


def test_claude_code_env_builder_is_auth_transparent() -> None:
    # Never set ANTHROPIC_API_KEY — that would force API-key mode and break
    # account/OAuth auth. The proxy forwards the caller's own credential.
    env = REGISTRY["claude-code"].env_builder(47950)
    assert "ANTHROPIC_API_KEY" not in env


def test_openai_style_base_urls_resolve_to_chat_completions_route() -> None:
    routes = _app_route_paths()
    for harness, key in (
        ("openclaw", "OPENAI_BASE_URL"),
        ("copilot-cli", "COPILOT_PROVIDER_BASE_URL"),
    ):
        env = REGISTRY[harness].env_builder(47950)
        base = _base_path(env, key)  # /proj/<token>/v1
        assert base.startswith("/proj/"), f"{harness}: expected /proj/<token> base, got {base!r}"
        # OpenAI-style SDKs request {base}/chat/completions. The tokenized route is
        # templated on the discriminator, so normalize the concrete token to {token}.
        parts = (base + "/chat/completions").split("/")
        parts[2] = "{token}"
        full = "/".join(parts)
        assert full in routes, f"{harness}: {full} is not a served route"


def test_codex_config_base_url_resolves_to_responses_route(tmp_path: Path) -> None:
    """codex wiring (repo-local config.toml) points at a served Responses route.

    codex has no base-URL env vector — its carrier is the CODEX_HOME config —
    so the contract is checked against the rendered config file instead of
    env_builder output. The Responses SDK requests {base_url}/responses.
    """
    import toml

    from agentalloy.providers.codex.install import render_config

    config = toml.loads(render_config(47950, tmp_path))
    base = urlparse(config["model_providers"]["agentalloy"]["base_url"]).path.rstrip("/")
    assert base.startswith("/proj/"), f"expected /proj/<token> base, got {base!r}"
    assert config["model_providers"]["agentalloy"]["wire_api"] == "responses"
    parts = (base + "/responses").split("/")
    parts[2] = "{token}"
    assert "/".join(parts) in _app_route_paths()
