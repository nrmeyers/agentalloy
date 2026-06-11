"""Wiring ↔ proxy route contract.

The harness wirings hand SDKs a base URL; each SDK appends its own request
path. These tests assert that (base URL path) + (SDK-appended path) lands on
a route the app actually serves — the failure mode being guarded was
ANTHROPIC_BASE_URL ending in /v1, which produced /v1/v1/messages (404).
"""

from __future__ import annotations

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
    # The Anthropic SDK requests {base}/v1/messages.
    assert _base_path(env, "ANTHROPIC_BASE_URL") + "/v1/messages" in _app_route_paths()


def test_openai_style_base_urls_resolve_to_chat_completions_route() -> None:
    routes = _app_route_paths()
    for harness, key in (("codex", "OPENAI_BASE_URL"), ("openclaw", "OPENAI_BASE_URL")):
        env = REGISTRY[harness].env_builder(47950)
        # OpenAI-style SDKs request {base}/chat/completions (base includes /v1).
        full = _base_path(env, key) + "/chat/completions"
        assert full in routes, f"{harness}: {full} is not a served route"
