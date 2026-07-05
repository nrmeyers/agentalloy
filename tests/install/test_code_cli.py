"""Unit tests for the ``agentalloy code`` subcommand (thin /code HTTP client)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentalloy.install.__main__ import build_parser
from agentalloy.install.subcommands import code as code_mod

_HEALTH_ENABLED = {"status": "healthy", "modules": {"compose": "enabled", "code_index": "enabled"}}


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentalloy")
    sub = parser.add_subparsers()
    code_mod.add_parser(sub)
    return parser.parse_args(argv)


def _mock_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    """Route _make_client through an httpx.MockTransport handler."""

    def _factory(port: int) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")

    monkeypatch.setattr(code_mod, "_make_client", _factory)


class TestParserRegistration:
    def test_code_registered_in_dispatcher_help(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        assert "code" in help_text

    def test_subactions_parse(self) -> None:
        assert _parse(["code", "status"]).func is code_mod._run_status  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "index", "--wait"]).func is code_mod._run_index  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "search", "q", "-k", "5"]).func is code_mod._run_search  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "symbol", "a.b"]).func is code_mod._run_symbol  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "callers", "a.b", "--depth", "3"]).func is code_mod._run_callers  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "callees", "a.b"]).func is code_mod._run_callees  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "bundle", "task"]).func is code_mod._run_bundle  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "remove", "--yes"]).func is code_mod._run_remove  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "watch", "status"]).func is code_mod._run_watch  # pyright: ignore[reportPrivateUsage]

    def test_bare_code_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = _parse(["code"])
        assert args.func(args) == 1
        assert "Usage: agentalloy code" in capsys.readouterr().err


class TestServiceDown:
    def test_status_service_down_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "ERROR: Cannot reach the agentalloy service" in err
        assert "FIX:" in err
        assert "server-start" in err


class TestModuleDisabled:
    @pytest.mark.parametrize("state", ["disabled", None])
    def test_disabled_module_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        state: str | None,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "healthy", "modules": {"code_index": state}})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "ERROR: The code-index module is disabled" in err
        assert "CODE_INDEX_ENABLED=1" in err

    def test_unavailable_module_points_at_extra(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "healthy", "modules": {"code_index": "unavailable"}}
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950"])
        assert args.func(args) == 1
        assert "agentalloy[code-index]" in capsys.readouterr().err


class TestStatus:
    def test_status_lists_repos_and_active_jobs(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "slug": "org__repo",
                            "repo_path": "/tmp/repo",
                            "last_indexed_at": 1,
                            "head_sha": "abc",
                            "symbol_count": 10,
                            "edge_count": 5,
                        }
                    ],
                )
            if request.url.path == "/code/index/jobs":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "j1",
                            "slug": "org__repo",
                            "state": "running",
                            "phase": "parse",
                            "progress": 40.0,
                        },
                        {
                            "id": "j0",
                            "slug": "org__repo",
                            "state": "done",
                            "phase": None,
                            "progress": 100.0,
                        },
                    ],
                )
            raise AssertionError(f"unexpected path {request.url.path}")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "org__repo" in out
        assert "Active jobs (1)" in out
        assert "j1" in out
        assert "j0" not in out  # terminal jobs are not "active"

    def test_status_json_shape(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950", "--json"])
        assert args.func(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"repos", "active_jobs"}


class TestSearch:
    def _handler(self, hits: list[dict[str, Any]]) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path in ("/code/search/semantic", "/code/search/lexical")
            return httpx.Response(200, json=hits)

        return handler

    def test_search_json_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        hits = [
            {
                "qualified_name": "pkg.mod.fn",
                "kind": "function",
                "file_path": "pkg/mod.py",
                "start_line": 3,
                "end_line": 9,
                "score": 0.9,
                "snippet": "def fn(): ...",
            }
        ]
        _mock_client(monkeypatch, self._handler(hits))
        args = _parse(["code", "search", "query", "--repo", "org__repo", "--json", "--port", "1"])
        assert args.func(args) == 0
        assert json.loads(capsys.readouterr().out) == hits

    def test_search_lexical_routes_to_lexical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            seen.append(request.url.path)
            return httpx.Response(200, json=[])

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "search", "q", "--lexical", "--repo", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert seen == ["/code/search/lexical"]

    def test_search_repo_not_indexed_fix(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(
                404,
                json={
                    "detail": "repo 'org__repo' is not indexed; index it via POST /code/index first"
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "search", "q", "--repo", "org__repo", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "not indexed" in err
        assert "agentalloy code index" in err


class TestCallGraph:
    def test_callers_uses_transitive_when_depth_gt_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"query": "x", "fqn": "a.b", "results": []})

        _mock_client(monkeypatch, handler)
        args = _parse(
            ["code", "callers", "a.b", "--depth", "3", "--repo", "org__repo", "--port", "1"]
        )
        assert args.func(args) == 0
        assert seen[0]["query"] == "transitive_callers"
        assert seen[0]["depth"] == "3"

    def test_callees_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"query": "callees", "fqn": "a.b", "results": []})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "callees", "a.b", "--repo", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert seen[0]["query"] == "callees"


class TestBundle:
    def test_bundle_posts_budget_and_prints_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/context-bundle"
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "repo": "org__repo",
                    "task": "do things",
                    "budget_chars": 1000,
                    "total_chars": 500,
                    "seed_count": 2,
                    "items": [
                        {
                            "qualified_name": "pkg.fn",
                            "file_path": "pkg.py",
                            "start_line": 1,
                            "end_line": 4,
                            "score": 0.5,
                            "reason": "seed",
                            "source": "def fn(): ...",
                        }
                    ],
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(
            [
                "code",
                "bundle",
                "do things",
                "--budget",
                "1000",
                "--repo",
                "org__repo",
                "--port",
                "1",
            ]
        )
        assert args.func(args) == 0
        assert bodies[0] == {"repo": "org__repo", "task": "do things", "budget_chars": 1000}
        out = capsys.readouterr().out
        assert "pkg.fn" in out


class TestIndex:
    def test_index_starts_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/index"
            body = json.loads(request.content)
            assert body["repo_path"] == str(tmp_path)
            assert body["force"] is True
            return httpx.Response(202, json={"id": "j1", "slug": "repo", "state": "queued"})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "index", str(tmp_path), "--force", "--port", "1"])
        assert args.func(args) == 0
        assert "Index job started" in capsys.readouterr().out

    def test_index_wait_polls_to_done(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(code_mod, "_POLL_INTERVAL_S", 0.0)
        states = iter(["running", "done"])

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/index":
                return httpx.Response(202, json={"id": "j1", "slug": "repo", "state": "queued"})
            assert request.url.path == "/code/index/j1/status"
            return httpx.Response(
                200,
                json={
                    "id": "j1",
                    "slug": "repo",
                    "state": next(states),
                    "phase": "embed",
                    "progress": 50.0,
                    "symbol_count": 7,
                    "edge_count": 3,
                    "embedding_count": 7,
                    "error": None,
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "index", str(tmp_path), "--wait", "--port", "1"])
        assert args.func(args) == 0
        assert "7 symbols" in capsys.readouterr().out

    def test_index_wait_failed_job_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(code_mod, "_POLL_INTERVAL_S", 0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/index":
                return httpx.Response(202, json={"id": "j1", "slug": "repo", "state": "queued"})
            return httpx.Response(
                200,
                json={
                    "id": "j1",
                    "slug": "repo",
                    "state": "failed",
                    "phase": None,
                    "progress": 10.0,
                    "error": "boom",
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "index", str(tmp_path), "--wait", "--port", "1"])
        assert args.func(args) == 1
        assert "boom" in capsys.readouterr().err


class TestRemove:
    def test_remove_requires_confirmation_non_tty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # pytest stdin is not a TTY; without --yes the command must refuse.
        args = _parse(["code", "remove", "org__repo", "--port", "1"])
        assert args.func(args) == 1
        assert "--yes" in capsys.readouterr().err

    def test_remove_yes_deletes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        deleted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.method == "DELETE"
            deleted.append(request.url.path)
            return httpx.Response(200, json={"slug": "org__repo", "removed": True})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "remove", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 0
        assert deleted == ["/code/index/org__repo"]
        assert "Removed index" in capsys.readouterr().out


class TestWatch:
    def test_watch_start_is_honest(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = _parse(["code", "watch", "start"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "CODE_INDEX_WATCH=1" in out
        assert "server-restart" in out

    def test_watch_stop_is_honest(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = _parse(["code", "watch", "stop"])
        assert args.func(args) == 0
        assert "CODE_INDEX_WATCH=0" in capsys.readouterr().out

    def test_watch_status_reports_config_and_service(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_state_dir: tuple[Path, Path],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "status", "--port", "1", "--json"])
        assert args.func(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"configured": False, "module": "unreachable"}
