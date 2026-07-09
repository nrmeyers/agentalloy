"""Unit tests for the ``agentalloy knowledge`` subcommand (build 04, AC 7).

A distinct CLI namespace over the shared /code/search/structural rail (DK7).
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx
import pytest

from agentalloy.install.__main__ import build_parser
from agentalloy.install.subcommands import code as code_mod
from agentalloy.install.subcommands import knowledge as knowledge_mod

_HEALTH_ENABLED = {"status": "healthy", "modules": {"compose": "enabled", "code_index": "enabled"}}


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentalloy")
    sub = parser.add_subparsers()
    knowledge_mod.add_parser(sub)
    return parser.parse_args(argv)


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    def _factory(port: int) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")

    # knowledge reuses code's _make_client seam
    monkeypatch.setattr(code_mod, "_make_client", _factory)


def test_knowledge_registered_and_parses() -> None:
    assert "knowledge" in build_parser().format_help()
    assert _parse(["knowledge", "why", "a.b"]).func is knowledge_mod._run_why  # pyright: ignore[reportPrivateUsage]


def test_why_queries_governing_decisions_and_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=_HEALTH_ENABLED)
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "query": "governing_decisions",
                "fqn": "pkg.foo",
                "results": [
                    {
                        "qualified_name": "docs/design/x/approach.md::why-foo",
                        "file_path": "docs/design/x/approach.md",
                        "start_line": 12,
                        "heading": "Why foo",
                        "snippet": "We chose pkg.foo.",
                    }
                ],
            },
        )

    _mock_client(monkeypatch, handler)
    args = _parse(["knowledge", "why", "pkg.foo", "--repo", "org__repo", "--port", "1"])
    assert args.func(args) == 0
    assert seen[0]["query"] == "governing_decisions"
    assert seen[0]["fqn"] == "pkg.foo"


def test_why_no_decisions_is_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=_HEALTH_ENABLED)
        return httpx.Response(200, json={"query": "governing_decisions", "results": []})

    _mock_client(monkeypatch, handler)
    args = _parse(["knowledge", "why", "pkg.orphan", "--repo", "org__repo", "--port", "1"])
    assert args.func(args) == 0
    assert "no governing decisions" in capsys.readouterr().out.lower()


def test_why_service_down_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, handler)
    args = _parse(["knowledge", "why", "pkg.foo", "--repo", "org__repo", "--port", "1"])
    assert args.func(args) == 1
    assert "Cannot reach the agentalloy service" in capsys.readouterr().err
