"""tests/install/test_knowledge_related.py

Tests for the `agentalloy knowledge related` CLI subcommand.
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

    monkeypatch.setattr(code_mod, "_make_client", _factory)


# ---------------------------------------------------------------------------
# Subcommand parsing
# ---------------------------------------------------------------------------


def test_related_subcommand_registered() -> None:
    """`knowledge related` is registered and parses."""
    assert "knowledge" in build_parser().format_help()
    args = _parse(["knowledge", "related", "dependency injection"])
    assert args.knowledge_cmd == "related"
    assert args.query == "dependency injection"
    assert args.json is False


def test_related_parses_repo_flag() -> None:
    """With --repo flag."""
    args = _parse(["knowledge", "related", "test query", "--repo", "my-repo"])
    assert args.query == "test query"
    assert args.repo == "my-repo"


def test_related_parses_json_flag() -> None:
    """With --json flag."""
    args = _parse(["knowledge", "related", "query", "--json"])
    assert args.json is True


# ---------------------------------------------------------------------------
# Output format — mocked HTTP round-trip
# ---------------------------------------------------------------------------


def test_related_queries_endpoint_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-empty results print formatted lines, hits the right endpoint."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=_HEALTH_ENABLED)
        seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                {
                    "qualified_name": "docs/solutions/di.md::anchor",
                    "file_path": "docs/solutions/di.md",
                    "start_line": 10,
                    "kind": "MarkdownDoc",
                    "score": 0.9,
                    "snippet": "DI is a pattern that provides dependencies from outside.",
                }
            ],
        )

    _mock_client(monkeypatch, handler)
    args = _parse(
        ["knowledge", "related", "dependency injection", "--repo", "org__repo", "--port", "1"]
    )
    assert args.func(args) == 0
    assert seen[0]["q"] == "dependency injection"
    assert seen[0]["repo"] == "org__repo"
    captured = capsys.readouterr()
    assert "docs/solutions/di.md::anchor" in captured.out
    assert "docs/solutions/di.md:10" in captured.out
    assert "DI is a pattern" in captured.out


def test_related_empty_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty results prints '(no related decisions)'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=_HEALTH_ENABLED)
        return httpx.Response(200, json=[])

    _mock_client(monkeypatch, handler)
    args = _parse(["knowledge", "related", "no match", "--repo", "org__repo", "--port", "1"])
    assert args.func(args) == 0
    captured = capsys.readouterr()
    assert "no related decisions" in captured.out


def test_related_json_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json flag produces valid JSON output."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json=_HEALTH_ENABLED)
        return httpx.Response(
            200,
            json=[
                {
                    "qualified_name": "docs/solutions/x.md::a",
                    "file_path": "docs/solutions/x.md",
                    "start_line": 5,
                    "kind": "MarkdownDoc",
                    "score": 0.8,
                    "snippet": "Topic A.",
                }
            ],
        )

    _mock_client(monkeypatch, handler)
    args = _parse(
        ["knowledge", "related", "topic a", "--repo", "org__repo", "--port", "1", "--json"]
    )
    assert args.func(args) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert len(parsed) == 1
    assert parsed[0]["qualified_name"] == "docs/solutions/x.md::a"


def test_related_service_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Service unavailable returns non-zero exit code."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, handler)
    args = _parse(["knowledge", "related", "test", "--repo", "org__repo", "--port", "1"])
    assert args.func(args) == 1
    assert "Cannot reach the agentalloy service" in capsys.readouterr().err
