"""Per-repo upstream resolution: ``.agentalloy/upstream`` adoption + global fallback."""

from __future__ import annotations

import types
from pathlib import Path

from agentalloy.api.anthropic_passthrough import AnthropicPassthroughClient
from agentalloy.api.proxy_context import (
    Upstream,
    read_upstream,
    resolve_chat_upstream_key,
)
from agentalloy.api.proxy_router import (
    _get_or_create_upstream_client,
    _passthrough_base_url,
    _resolve_upstream,
    resolve_passthrough_client,
)


def _write_upstream(root: Path, text: str) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "upstream").write_text(text)


class TestReadUpstream:
    def test_parses_url_model_keyenv(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\nkey_env: OPENAI_API_KEY\n")
        result = read_upstream(tmp_path)
        assert result.kind == "valid"
        assert result.upstream == Upstream(
            url="http://h:9000/v1", model="m1", key_env="OPENAI_API_KEY"
        )

    def test_strips_trailing_slash_and_optional_keyenv(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1/\nmodel: m1\n")
        result = read_upstream(tmp_path)
        assert result.kind == "valid"
        assert result.upstream == Upstream(url="http://h:9000/v1", model="m1", key_env=None)

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        result = read_upstream(tmp_path)
        assert result.kind == "absent"

    def test_missing_required_keys_is_none(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1\n")  # no model
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_malformed_yaml_is_none(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: [unclosed\n")
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_empty_file_is_error(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "")
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_non_dict_yaml_is_error(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "just a string\n")
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_empty_url_and_model_is_error(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, 'url: ""\nmodel: ""\n')
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_missing_url_only_is_error(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "model: m1\n")
        result = read_upstream(tmp_path)
        assert result.kind == "error"
        assert result.detail is not None

    def test_namespaced_reads_own_harness(self, tmp_path: Path) -> None:
        _write_upstream(
            tmp_path,
            "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
            "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n  key_env: OPENAI_API_KEY\n",
        )
        claude = read_upstream(tmp_path, harness="claude-code")
        assert claude.kind == "valid" and claude.upstream is not None
        assert claude.upstream.url == "https://api.anthropic.com"
        assert claude.upstream.model == "c1"

        qwen = read_upstream(tmp_path, harness="qwen-code")
        assert qwen.kind == "valid" and qwen.upstream is not None
        assert qwen.upstream.url == "http://h:9000/v1"
        assert qwen.upstream.key_env == "OPENAI_API_KEY"

        # Chat scope resolves the first non-passthrough harness's entry.
        chat = read_upstream(tmp_path)  # harness=None => chat scope
        assert chat.kind == "valid" and chat.upstream is not None
        assert chat.upstream.model == "m1"

    def test_passthrough_harness_needs_its_own_entry(self, tmp_path: Path) -> None:
        # Only a chat harness is present — the passthrough must NOT see it.
        _write_upstream(tmp_path, "qwen-code:\n  url: http://h:9000/v1\n  model: mannix\n")
        claude = read_upstream(tmp_path, harness="claude-code")
        assert claude.kind == "absent"
        codex = read_upstream(tmp_path, harness="codex")
        assert codex.kind == "absent"

    def test_legacy_flat_is_chat_scope_only(self, tmp_path: Path) -> None:
        # A pre-namespacing flat file satisfies the chat surface but never a passthrough.
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: mannix\n")
        assert read_upstream(tmp_path).kind == "valid"
        assert read_upstream(tmp_path, harness="claude-code").kind == "absent"
        assert read_upstream(tmp_path, harness="codex").kind == "absent"


class TestResolveChatUpstreamKey:
    """The write path needs the *key* the chat scope resolves to — read_upstream
    returns only the values. This must mirror read_upstream(harness=None) exactly
    so the UI and the proxy can never disagree on which entry is 'active'."""

    def test_namespaced_first_non_passthrough(self, tmp_path: Path) -> None:
        _write_upstream(
            tmp_path,
            "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
            "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n",
        )
        assert resolve_chat_upstream_key(tmp_path) == "qwen-code"

    def test_namespaced_skips_leading_passthrough(self, tmp_path: Path) -> None:
        # claude-code first, but it's a passthrough — the chat key is the next one.
        _write_upstream(
            tmp_path,
            "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
            "codex:\n  url: https://api.openai.com\n  model: o1\n"
            "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n",
        )
        assert resolve_chat_upstream_key(tmp_path) == "qwen-code"

    def test_legacy_flat_is_top_level(self, tmp_path: Path) -> None:
        # A flat file's chat scope is the top level, signalled by the empty key.
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\n")
        assert resolve_chat_upstream_key(tmp_path) == ""

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        assert resolve_chat_upstream_key(tmp_path) is None

    def test_passthrough_only_is_none(self, tmp_path: Path) -> None:
        _write_upstream(
            tmp_path,
            "claude-code:\n  url: https://api.anthropic.com\n  model: c1\n"
            "codex:\n  url: https://api.openai.com\n  model: o1\n",
        )
        assert resolve_chat_upstream_key(tmp_path) is None

    def test_malformed_is_none(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: [unclosed\n")
        assert resolve_chat_upstream_key(tmp_path) is None

    def test_agrees_with_read_upstream(self, tmp_path: Path) -> None:
        # The invariant: whenever read_upstream(harness=None) is valid, the
        # resolver names the key whose entry read_upstream would return.
        _write_upstream(
            tmp_path,
            "codex:\n  url: https://api.openai.com\n  model: o1\n"
            "qwen-code:\n  url: http://h:9000/v1\n  model: m1\n  key_env: OPENAI_API_KEY\n",
        )
        key = resolve_chat_upstream_key(tmp_path)
        chat = read_upstream(tmp_path)
        assert key == "qwen-code"
        assert chat.kind == "valid" and chat.upstream is not None
        assert chat.upstream.model == "m1"


def _fake_app() -> types.SimpleNamespace:
    return types.SimpleNamespace(state=types.SimpleNamespace())


class TestResolveUpstream:
    def test_per_repo_wins_and_targets_absolute_chat_url(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: qwen\n")
        app = _fake_app()
        sentinel = object()  # the global default client; must NOT be chosen here
        resolved = _resolve_upstream(app, tmp_path, sentinel, "global-model")  # type: ignore[arg-type]
        assert resolved is not None
        client, chat_url, model = resolved
        assert client is not sentinel
        assert chat_url == "http://h:9000/v1/chat/completions"
        assert model == "qwen"
        assert "http://h:9000/v1" in app.state.upstream_client_cache

    def test_falls_back_to_global_default(self, tmp_path: Path) -> None:
        app = _fake_app()
        sentinel = object()
        resolved = _resolve_upstream(app, tmp_path, sentinel, "global-model")  # type: ignore[arg-type]
        assert resolved == (sentinel, "/v1/chat/completions", "global-model")

    def test_none_when_neither_resolves(self, tmp_path: Path) -> None:
        app = _fake_app()
        assert _resolve_upstream(app, tmp_path, None, "") is None


class TestClientCache:
    def test_same_base_url_reuses_client(self) -> None:
        app = _fake_app()
        c1 = _get_or_create_upstream_client(app, "http://h:9000", None)
        c2 = _get_or_create_upstream_client(app, "http://h:9000", None)
        c3 = _get_or_create_upstream_client(app, "http://other:1", None)
        assert c1 is c2
        assert c3 is not c1


class TestPassthroughBaseUrl:
    def test_strips_trailing_v1(self) -> None:
        # `.agentalloy/upstream` is documented (and written by `agentalloy add`)
        # with a /v1 suffix -- the chat-completions shape. The passthrough
        # surfaces' _UPSTREAM_PATH already carries /v1/messages or
        # /v1/responses, so verbatim reuse would double it (#505 note).
        assert _passthrough_base_url("http://h:9000/v1") == "http://h:9000"

    def test_leaves_bare_host_unchanged(self) -> None:
        assert _passthrough_base_url("http://h:9000") == "http://h:9000"

    def test_only_strips_trailing_occurrence(self) -> None:
        assert _passthrough_base_url("http://h:9000/v1/proxy") == "http://h:9000/v1/proxy"


class TestResolvePassthroughClient:
    def test_per_repo_wins_and_strips_v1(self, tmp_path: Path) -> None:
        # An explicit claude-code scoped entry opts into chaining.
        _write_upstream(tmp_path, "claude-code:\n  url: http://h:9000/v1\n  model: qwen\n")
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        resolved = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert resolved is not None
        assert resolved is not default
        assert resolved.upstream_base_url == "http://h:9000"
        assert "http://h:9000" in app.state.test_client_cache

    def test_flat_chat_upstream_never_captures_passthrough(self, tmp_path: Path) -> None:
        # A legacy flat (chat-scope) upstream must NOT redirect Claude Code.
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: qwen\n")
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        resolved = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert resolved is default
        assert app.state.__dict__.get("test_client_cache") in (None, {})

    def test_other_harness_upstream_never_captures_passthrough(self, tmp_path: Path) -> None:
        # A chat harness's local upstream must never redirect the Claude passthrough.
        _write_upstream(tmp_path, "qwen-code:\n  url: http://h:9000/v1\n  model: mannix\n")
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        resolved = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert resolved is default

    def test_falls_back_to_default_client(self, tmp_path: Path) -> None:
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        resolved = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert resolved is default

    def test_none_when_neither_resolves(self, tmp_path: Path) -> None:
        app = _fake_app()
        assert (
            resolve_passthrough_client(
                app, tmp_path, None, "test_client_cache", harness="claude-code"
            )
            is None
        )

    def test_reuses_cached_client_for_same_base_url(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "claude-code:\n  url: http://h:9000/v1\n  model: qwen\n")
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        c1 = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        c2 = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert c1 is c2

    def test_key_env_never_used_to_build_a_credential(self, tmp_path: Path) -> None:
        # Auth-transparent by design: a per-repo key_env must play no role on
        # the passthrough surfaces -- they forward the caller's own header
        # verbatim. Resolving must not raise or attempt to read the env var,
        # and the resulting client carries no bearer of its own.
        _write_upstream(
            tmp_path,
            "claude-code:\n  url: http://h:9000/v1\n  model: qwen\n  key_env: SOME_UNSET_VAR\n",
        )
        app = _fake_app()
        resolved = resolve_passthrough_client(
            app, tmp_path, None, "test_client_cache", harness="claude-code"
        )
        assert resolved is not None
        assert resolved.upstream_base_url == "http://h:9000"

    def test_error_returns_upstream_file(self, tmp_path: Path) -> None:
        """Malformed upstream yields UpstreamFile(kind="error"), not default_client."""
        from agentalloy.api.proxy_context import UpstreamFile

        _write_upstream(tmp_path, "claude-code:\n  url: http://h/v1\n")  # no model
        app = _fake_app()
        default = AnthropicPassthroughClient(upstream_base_url="http://default-upstream")
        resolved = resolve_passthrough_client(
            app, tmp_path, default, "test_client_cache", harness="claude-code"
        )
        assert isinstance(resolved, UpstreamFile)
        assert resolved.kind == "error"
        assert resolved.detail is not None
        # Crucially: the default client is NOT returned
        assert resolved is not default


class TestAnthropicPassthroughError:
    """End-to-end error handling for Anthropic Messages passthrough.

    The router routes are already ``/proj/{token}/v1/messages`` — we include
    the router without a prefix so the token is extracted by the route itself,
    not doubled by a prefix.
    """

    def test_malformed_upstream_returns_503(self, tmp_path: Path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from agentalloy.api.proxy_context import encode_proj_token
        from agentalloy.api.proxy_passthrough_router import router as passthrough_router

        _write_upstream(tmp_path, "url: [unclosed\n")

        app = FastAPI()
        app.include_router(passthrough_router)

        token = encode_proj_token(tmp_path)
        client = TestClient(app)
        response = client.post(
            f"/proj/{token}/v1/messages",
            json={"model": "claude-sonnet-4-20250514", "messages": []},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["type"] == "error"
        assert body["error"]["code"] == "upstream_parse_error"


class TestResponsesPassthroughError:
    """End-to-end error handling for OpenAI Responses passthrough.

    Same prefix fix as the Anthropic test above — the router already owns the
    ``/proj/{token}`` segment.
    """

    def test_malformed_upstream_returns_503(self, tmp_path: Path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from agentalloy.api.proxy_context import encode_proj_token
        from agentalloy.api.proxy_responses_router import router as responses_router

        _write_upstream(tmp_path, "url: [unclosed\n")

        app = FastAPI()
        app.include_router(responses_router)

        token = encode_proj_token(tmp_path)
        client = TestClient(app)
        response = client.post(
            f"/proj/{token}/v1/responses",
            json={"model": "o3"},
        )
        assert response.status_code == 503
        body = response.json()
        assert "error" in body
        assert body["error"]["code"] == "upstream_parse_error"
