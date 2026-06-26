"""Per-repo upstream resolution: ``.agentalloy/upstream`` adoption + global fallback."""

from __future__ import annotations

import types
from pathlib import Path

from agentalloy.api.proxy_context import Upstream, read_upstream
from agentalloy.api.proxy_router import _get_or_create_upstream_client, _resolve_upstream


def _write_upstream(root: Path, text: str) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "upstream").write_text(text)


class TestReadUpstream:
    def test_parses_url_model_keyenv(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1\nmodel: m1\nkey_env: OPENAI_API_KEY\n")
        up = read_upstream(tmp_path)
        assert up == Upstream(url="http://h:9000/v1", model="m1", key_env="OPENAI_API_KEY")

    def test_strips_trailing_slash_and_optional_keyenv(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1/\nmodel: m1\n")
        up = read_upstream(tmp_path)
        assert up == Upstream(url="http://h:9000/v1", model="m1", key_env=None)

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        assert read_upstream(tmp_path) is None

    def test_missing_required_keys_is_none(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: http://h:9000/v1\n")  # no model
        assert read_upstream(tmp_path) is None

    def test_malformed_yaml_is_none(self, tmp_path: Path) -> None:
        _write_upstream(tmp_path, "url: [unclosed\n")
        assert read_upstream(tmp_path) is None


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
