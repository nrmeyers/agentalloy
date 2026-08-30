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
        assert _parse(["code", "prune", "--yes"]).func is code_mod._run_prune  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "prune", "--all", "--yes"]).func is code_mod._run_prune  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "watch", "status"]).func is code_mod._run_watch  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "watch", "enable"]).func is code_mod._run_watch_enable  # pyright: ignore[reportPrivateUsage]
        assert _parse(["code", "watch", "disable", "/x"]).func is code_mod._run_watch_disable  # pyright: ignore[reportPrivateUsage]

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
        assert "uv sync" in capsys.readouterr().err


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
                            "indexed_head": "abc",
                            "current_head": "abc",
                            "is_stale": False,
                            "watch_enabled": False,
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
        assert set(payload) == {"repos", "active_jobs", "recent_failures"}

    def test_status_surfaces_latest_failed_job(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A repo whose latest attempt failed shows a Recent failures line with
        the error — instead of the failure being silently discarded."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(200, json=[])
            if request.url.path == "/code/index/jobs":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "jf",
                            "slug": "org__repo",
                            "state": "failed",
                            "phase": "markdown",
                            "progress": 75.0,
                            "error": "LMUnavailable: [Errno 111] Connection refused",
                        },
                        {
                            "id": "jok",
                            "slug": "other__repo",
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
        assert "Recent failures (1)" in out
        assert "org__repo" in out
        assert "Connection refused" in out
        # A slug whose latest attempt succeeded is not reported as a failure.
        assert "other__repo" not in out.split("Recent failures")[1]

    def test_status_failure_shadowed_by_later_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An older failure must not surface once a newer run for the same slug
        succeeded (jobs arrive newest-first)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(200, json=[])
            if request.url.path == "/code/index/jobs":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "jok", "slug": "org__repo", "state": "done", "progress": 100.0},
                        {
                            "id": "jf",
                            "slug": "org__repo",
                            "state": "failed",
                            "phase": "markdown",
                            "progress": 75.0,
                            "error": "boom",
                        },
                    ],
                )
            raise AssertionError(f"unexpected path {request.url.path}")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "status", "--port", "47950"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Recent failures" not in out


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


class TestModuleToggle:
    """`agentalloy code enable|disable` — the CODE_INDEX_ENABLED master switch
    as a single command. Deliberately NOT `write-env` (full-preset re-render,
    refuses a hand-edited .env without --force) — a surgical one-line patch
    instead, safe regardless of the .env's provenance."""

    def test_enable_creates_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = tmp_path / "config" / "agentalloy" / ".env"
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "enable"])
        assert args.func(args) == 0
        assert env_file.read_text() == "CODE_INDEX_ENABLED=1\n"
        out = capsys.readouterr().out
        assert "enabled" in out
        assert "server-restart" in out

    def test_disable_writes_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env_file = tmp_path / ".env"
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "disable"])
        assert args.func(args) == 0
        assert env_file.read_text() == "CODE_INDEX_ENABLED=0\n"
        assert "disabled" in capsys.readouterr().out

    def test_enable_replaces_existing_value_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("CODE_INDEX_ENABLED=0\nLOG_LEVEL=debug\n")
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "enable"])
        assert args.func(args) == 0
        assert env_file.read_text() == "CODE_INDEX_ENABLED=1\nLOG_LEVEL=debug\n"

    def test_enable_preserves_comments_and_other_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# my custom notes\nUPSTREAM_URL=http://localhost:8080/v1\n"
            "CODE_INDEX_ENABLED=0\nLOG_LEVEL=debug\n"
        )
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "enable"])
        assert args.func(args) == 0
        assert env_file.read_text() == (
            "# my custom notes\nUPSTREAM_URL=http://localhost:8080/v1\n"
            "CODE_INDEX_ENABLED=1\nLOG_LEVEL=debug\n"
        )

    def test_disable_appends_when_key_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("LOG_LEVEL=debug\n")
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "disable"])
        assert args.func(args) == 0
        assert env_file.read_text() == "LOG_LEVEL=debug\nCODE_INDEX_ENABLED=0\n"

    def test_enable_ignores_commented_out_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A commented-out CODE_INDEX_ENABLED must not be mistaken for a live
        value — the real key gets appended, the comment survives untouched."""
        env_file = tmp_path / ".env"
        env_file.write_text("# CODE_INDEX_ENABLED=0\nLOG_LEVEL=debug\n")
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        args = _parse(["code", "enable"])
        assert args.func(args) == 0
        assert env_file.read_text() == (
            "# CODE_INDEX_ENABLED=0\nLOG_LEVEL=debug\nCODE_INDEX_ENABLED=1\n"
        )

    def test_enable_then_disable_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        monkeypatch.setattr("agentalloy.install.state.env_path", lambda: env_file)
        enable_args = _parse(["code", "enable"])
        assert enable_args.func(enable_args) == 0
        assert env_file.read_text() == "CODE_INDEX_ENABLED=1\n"
        disable_args = _parse(["code", "disable"])
        assert disable_args.func(disable_args) == 0
        assert env_file.read_text() == "CODE_INDEX_ENABLED=0\n"


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
        assert payload == {"configured": False, "module": "unreachable", "enrolled_repos": None}

    def test_watch_status_lists_enrolled_repos(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_state_dir: tuple[Path, Path],
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/repos"
            return httpx.Response(
                200,
                json=[
                    {"slug": "org__a", "repo_path": "/src/a", "watch_enabled": True},
                    {"slug": "org__b", "repo_path": "/src/b", "watch_enabled": False},
                ],
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "status", "--port", "1"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Watch-enrolled repos (1)" in out
        assert "org__a" in out
        assert "org__b" not in out

    def test_watch_enable_posts_enrollment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/repos/org__repo/watch"
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "slug": "org__repo",
                    "watch_enabled": True,
                    "watching": True,
                    "master_switch": True,
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "enable", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert bodies == [{"enabled": True}]
        assert "Watch enabled for org__repo" in capsys.readouterr().out

    def test_watch_enable_master_off_explains(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(
                200,
                json={
                    "slug": "org__repo",
                    "watch_enabled": True,
                    "watching": False,
                    "master_switch": False,
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "enable", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert "CODE_INDEX_WATCH" in capsys.readouterr().out

    def test_watch_disable_posts_enrollment_off(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "slug": "org__repo",
                    "watch_enabled": False,
                    "watching": False,
                    "master_switch": True,
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "disable", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert bodies == [{"enabled": False}]
        assert "Watch disabled for org__repo" in capsys.readouterr().out

    def test_watch_enable_service_down(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "watch", "enable", "org__repo", "--port", "1"])
        assert args.func(args) == 1
        assert "Cannot reach the agentalloy service" in capsys.readouterr().err


class TestStatusStaleness:
    @staticmethod
    def _git(repo: Path, *argv: str) -> str:
        import subprocess

        out = subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def _make_repo(self, root: Path) -> str:
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        (root / "a.py").write_text("x = 1\n")
        self._git(root, "add", ".")
        self._git(root, "commit", "-q", "-m", "one")
        return self._git(root, "rev-parse", "HEAD")

    def _status_handler(self, repo: dict[str, Any]) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(200, json=[repo])
            return httpx.Response(200, json=[])

        return handler

    def _repo_view(self, path: str, sha: str | None) -> dict[str, Any]:
        """Return a repo view with the given indexed_head.

        Deliberately omits is_stale/current_head so the CLI falls back to a
        local staleness check against the real git HEAD — the whole point of
        these tests is to exercise that fallback path.
        """
        return {
            "slug": "org__repo",
            "repo_path": path,
            "last_indexed_at": 1,
            "indexed_head": sha,
            "watch_enabled": False,
            "symbol_count": 10,
            "edge_count": 5,
        }

    def test_moved_head_shows_stale_with_commit_count(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        first = self._make_repo(repo)
        (repo / "b.py").write_text("y = 2\n")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "two")

        _mock_client(monkeypatch, self._status_handler(self._repo_view(str(repo), first)))
        args = _parse(["code", "status", "--port", "1"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "[stale" in out
        assert "1 commits behind" in out
        assert f"agentalloy code index {repo}" in out

    def test_fresh_head_shows_no_stale_marker(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        sha = self._make_repo(repo)
        _mock_client(monkeypatch, self._status_handler(self._repo_view(str(repo), sha)))
        args = _parse(["code", "status", "--port", "1"])
        assert args.func(args) == 0
        assert "[stale" not in capsys.readouterr().out

    def test_rebased_away_sha_falls_back_to_plain_stale(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        self._make_repo(repo)
        view = self._repo_view(str(repo), "0" * 40)  # sha not in history (post-rebase)
        _mock_client(monkeypatch, self._status_handler(view))
        args = _parse(["code", "status", "--port", "1"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "[stale" in out
        assert "commits behind" not in out

    def test_non_git_and_missing_paths_stay_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        view = self._repo_view(str(plain), "abc123")
        _mock_client(monkeypatch, self._status_handler(view))
        args = _parse(["code", "status", "--port", "1"])
        assert args.func(args) == 0
        assert "[stale" not in capsys.readouterr().out

    def test_watch_enrollment_marker_in_status(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        view = self._repo_view(str(tmp_path), None)
        view["watch_enabled"] = True
        _mock_client(monkeypatch, self._status_handler(view))
        args = _parse(["code", "status", "--port", "1"])
        assert args.func(args) == 0
        assert "watch=on" in capsys.readouterr().out


class TestResolveSlug:
    """`_resolve_repo_slug` — the service registry is authoritative for a path;
    an explicit non-path slug short-circuits with zero network (Bug B)."""

    def _repos_handler(self, rows: Any) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/code/repos"
            return httpx.Response(200, json=rows)

        return handler

    def _no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make _make_client explode so a network call fails the test."""

        def _boom(port: int) -> httpx.Client:
            raise AssertionError("network call made during slug resolution")

        monkeypatch.setattr(code_mod, "_make_client", _boom)

    def test_registry_slug_wins_over_local_derivation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The service already indexed this path under a path-derived slug; the host
        # would derive something else. The registry value must win (the Bug B split).
        monkeypatch.setattr(code_mod, "repo_slug", lambda p: "host__rederived")
        root = tmp_path.resolve()
        rows = [{"repo_path": str(root), "slug": "svc_indexed"}]
        _mock_client(monkeypatch, self._repos_handler(rows))
        assert code_mod._resolve_repo_slug(str(root), 1) == "svc_indexed"

    def test_cwd_uses_registry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # No --repo → cwd; still resolves via the registry.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(code_mod, "repo_slug", lambda p: "host__rederived")
        rows = [{"repo_path": str(tmp_path.resolve()), "slug": "svc_indexed"}]
        _mock_client(monkeypatch, self._repos_handler(rows))
        assert code_mod._resolve_repo_slug(None, 1) == "svc_indexed"

    def test_unindexed_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Registry has no row for this path → local derivation.
        monkeypatch.setattr(code_mod, "repo_slug", lambda p: "local__slug")
        _mock_client(monkeypatch, self._repos_handler([]))
        assert code_mod._resolve_repo_slug(str(tmp_path.resolve()), 1) == "local__slug"

    def test_explicit_slug_short_circuits_zero_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-path --repo value is a slug; no registry lookup, no derivation.
        self._no_network(monkeypatch)
        monkeypatch.setattr(
            code_mod, "repo_slug", lambda p: pytest.fail("repo_slug must not be called")
        )
        assert code_mod._resolve_repo_slug("org__repo", 1) == "org__repo"

    def test_service_down_falls_back_to_local(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # /code/repos unreachable → best-effort None → local derivation (no crash).
        def _down(port: int) -> httpx.Client:
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused")

            return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")

        monkeypatch.setattr(code_mod, "_make_client", _down)
        monkeypatch.setattr(code_mod, "repo_slug", lambda p: "local__slug")
        assert code_mod._resolve_repo_slug(str(tmp_path.resolve()), 1) == "local__slug"

    def test_slug_from_registry_ignores_malformed_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Non-list body / non-dict rows / missing keys must not crash; return None.
        root = tmp_path.resolve()
        rows = ["nope", {"repo_path": 42, "slug": "x"}, {"slug": "no_path"}]
        _mock_client(monkeypatch, self._repos_handler(rows))
        assert code_mod._slug_from_registry(1, root) is None


class TestEnableInstallsExtraOnDemand:
    """`agentalloy code enable` installs the [code-index] extra on demand when
    it's missing (opting in *is* the request), rather than refusing."""

    def _force_missing_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        real = importlib.import_module

        def _boom(name: str, *a: Any, **k: Any) -> Any:
            if name == "agentalloy.code_index.api":
                raise ImportError("No module named 'tree_sitter'")
            return real(name, *a, **k)

        monkeypatch.setattr(importlib, "import_module", _boom)

    def test_enable_installs_then_flips_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._force_missing_extra(monkeypatch)
        patched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            code_mod, "_patch_env_key", lambda k, v: patched.append((k, v)) or Path("/tmp/.env")
        )
        monkeypatch.setattr(
            "agentalloy.install.subcommands.upgrade.ensure_code_index_extra",
            lambda **_: ("installed", "v9.9.9"),
        )
        assert code_mod._run_module_toggle(enabled=True) == 0
        assert ("CODE_INDEX_ENABLED", "1") in patched

    def test_enable_aborts_without_flipping_when_install_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._force_missing_extra(monkeypatch)
        patched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            code_mod, "_patch_env_key", lambda k, v: patched.append((k, v)) or Path("/tmp/.env")
        )
        monkeypatch.setattr(
            "agentalloy.install.subcommands.upgrade.ensure_code_index_extra",
            lambda **_: ("failed", "No solution found"),
        )
        assert code_mod._run_module_toggle(enabled=True) == 1
        assert patched == []  # flag never flipped on failure

    def test_disable_never_touches_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patched: list[tuple[str, str]] = []
        monkeypatch.setattr(
            code_mod, "_patch_env_key", lambda k, v: patched.append((k, v)) or Path("/tmp/.env")
        )

        def _fail(**_: Any) -> Any:
            raise AssertionError("disable must not install the extra")

        monkeypatch.setattr("agentalloy.install.subcommands.upgrade.ensure_code_index_extra", _fail)
        assert code_mod._run_module_toggle(enabled=False) == 0
        assert ("CODE_INDEX_ENABLED", "0") in patched


class TestMigrateLayout:
    """The CLI half of the automatic, no-opt-in upgrade migration.

    Its job is to be un-failable for reasons that are not a real migration
    failure: an upgrade must never be downgraded to "completed with warnings"
    because the module is off, the service is down, or the service predates the
    endpoint.
    """

    def test_registered_and_flags_parse(self) -> None:
        args = _parse(["code", "migrate-layout", "--wait", "--quiet", "--dry-run"])
        assert args.func is code_mod._run_migrate_layout  # pyright: ignore[reportPrivateUsage]
        assert args.wait and args.quiet and args.dry_run
        assert _parse(["code", "migrate-layout"]).keep_missing is False

    def test_sends_prune_and_dry_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "dry_run": True,
                    "total": 0,
                    "current": 0,
                    "legacy": 0,
                    "pruned": 0,
                    "busy": 0,
                    "entries": [],
                    "jobs": [],
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout", "--dry-run", "--keep-missing"])
        assert args.func(args) == 0
        assert seen == {"dry_run": True, "prune_missing": False}

    def test_prunes_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "dry_run": False,
                    "total": 0,
                    "current": 0,
                    "legacy": 0,
                    "pruned": 0,
                    "busy": 0,
                    "entries": [],
                    "jobs": [],
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout"])
        assert args.func(args) == 0
        assert seen["prune_missing"] is True

    @pytest.mark.parametrize("status", [404, 405])
    def test_older_service_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """Verified live against 7.8.0: the SPA catch-all answers 405, not 404."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(status, json={"detail": "Method Not Allowed"})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout", "--quiet"])
        assert args.func(args) == 0

    def test_disabled_module_is_not_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"modules": {"code_index": "disabled"}})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout", "--quiet"])
        assert args.func(args) == 0

    def test_service_down_is_not_a_failure_when_quiet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout", "--quiet"])
        assert args.func(args) == 0

    def test_real_server_error_still_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only version skew is forgiven — a 500 is a genuine failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(500, json={"detail": "boom"})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "migrate-layout", "--quiet"])
        assert args.func(args) == 1


class TestPrune:
    """`agentalloy code prune` — the explicit orphan path (spec R2).

    The CLI is a thin client: slug resolution is registry-authoritative (like
    remove), real deletion needs --yes or an interactive "yes", --all is a dry
    run unless --yes, and every service error maps to ERROR/CAUSE/FIX.
    """

    def _prune_handler(self, seen: dict[str, Any], body: dict[str, Any]) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/prune"
            assert request.method == "POST"
            seen.update(json.loads(request.content))
            return httpx.Response(200, json=body)

        return handler

    def test_pruned_success_prints_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": False,
                    "forced": False,
                    "total": 1,
                    "pruned": 1,
                    "stamped": 0,
                    "skipped": 0,
                    "entries": [
                        {
                            "slug": "org__repo",
                            "repo_path": "/gone",
                            "verdict": "pruned",
                            "row_deleted": True,
                            "store_dir": "/data/repos/org__repo/x",
                            "store_dir_removed": True,
                        }
                    ],
                },
            ),
        )
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 0
        assert seen == {"slug": "org__repo", "repo_path": None, "dry_run": False, "force": False}
        out = capsys.readouterr().out
        assert "Pruned org__repo" in out
        assert "store dir removed" in out

    def test_stamped_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": False,
                    "forced": False,
                    "total": 1,
                    "pruned": 0,
                    "stamped": 1,
                    "skipped": 0,
                    "entries": [{"slug": "org__repo", "repo_path": "/gone", "verdict": "stamped"}],
                },
            ),
        )
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Stamped org__repo" in out
        assert "grace period" in out

    def test_404_is_nonzero_with_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(404, json={"detail": "no such registry row: nope"})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "nope", "--yes", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "ERROR:" in err
        assert "CAUSE: no such registry row: nope" in err
        assert "FIX:" in err

    def test_400_live_checkout_points_at_remove(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(
                400, json={"detail": "repo_path still exists (/x); use `agentalloy code remove`"}
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "400" in err
        assert "code remove" in err

    @pytest.mark.parametrize(
        "detail", ["grace not elapsed; ~3.2 days remaining (use force to bypass)"]
    )
    def test_409_is_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        detail: str,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            return httpx.Response(409, json={"detail": detail})

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "CAUSE: grace not elapsed" in err

    def test_single_dry_run_skips_confirmation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # pytest stdin is not a TTY; a dry run must still go through.
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": True,
                    "forced": False,
                    "total": 1,
                    "pruned": 1,
                    "stamped": 0,
                    "skipped": 0,
                    "entries": [
                        {
                            "slug": "org__repo",
                            "repo_path": "/gone",
                            "verdict": "pruned",
                            "detail": "dry run: would prune",
                        }
                    ],
                },
            ),
        )
        args = _parse(["code", "prune", "org__repo", "--dry-run", "--port", "1"])
        assert args.func(args) == 0
        assert seen == {"slug": "org__repo", "repo_path": None, "dry_run": True, "force": False}
        assert "Would prune org__repo" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("store_dir", "expected"),
        [
            ("/data/repos/org__repo/x", "store dir would be removed"),
            (None, "store dir preserved (shared or absent)"),
        ],
    )
    def test_dry_run_reports_store_dir_disposition(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        store_dir: str | None,
        expected: str,
    ) -> None:
        # AC-4: a dry run names exactly which store dir would be removed — and
        # distinguishes it from one that would be preserved (shared/absent).
        _mock_client(
            monkeypatch,
            self._prune_handler(
                {},
                {
                    "dry_run": True,
                    "forced": False,
                    "total": 1,
                    "pruned": 1,
                    "stamped": 0,
                    "skipped": 0,
                    "entries": [
                        {
                            "slug": "org__repo",
                            "repo_path": "/gone",
                            "verdict": "pruned",
                            "store_dir": store_dir,
                            "store_dir_removed": False,
                        }
                    ],
                },
            ),
        )
        args = _parse(["code", "prune", "org__repo", "--dry-run", "--port", "1"])
        assert args.func(args) == 0
        out = capsys.readouterr().out
        assert "Would prune org__repo" in out
        assert expected in out

    def test_all_defaults_to_dry_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": True,
                    "forced": False,
                    "total": 3,
                    "pruned": 1,
                    "stamped": 1,
                    "skipped": 1,
                    "entries": [
                        {"slug": "a", "repo_path": "/a", "verdict": "pruned"},
                        {"slug": "b", "repo_path": "/b", "verdict": "stamped"},
                        {"slug": "c", "repo_path": "/c", "verdict": "live"},
                    ],
                },
            ),
        )
        args = _parse(["code", "prune", "--all", "--port", "1"])
        assert args.func(args) == 0
        assert seen == {"slug": None, "repo_path": None, "dry_run": True, "force": False}
        out = capsys.readouterr().out
        assert "Would prune summary: 1 pruned, 1 stamped, 1 skipped of 3" in out
        assert "  pruned     a" in out
        assert "  stamped    b" in out
        assert "  live       c" not in out  # live rows are omitted

    def test_all_yes_executes_with_force(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": False,
                    "forced": True,
                    "total": 1,
                    "pruned": 1,
                    "stamped": 0,
                    "skipped": 0,
                    "entries": [{"slug": "a", "repo_path": "/a", "verdict": "pruned"}],
                },
            ),
        )
        args = _parse(["code", "prune", "--all", "--yes", "--force", "--port", "1"])
        assert args.func(args) == 0
        assert seen == {"slug": None, "repo_path": None, "dry_run": False, "force": True}
        assert "Pruned summary:" in capsys.readouterr().out

    def test_all_force_alone_is_still_dry_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --force bypasses the grace gate, never the execution consent.
        seen: dict[str, Any] = {}
        _mock_client(
            monkeypatch,
            self._prune_handler(
                seen,
                {
                    "dry_run": True,
                    "forced": True,
                    "total": 0,
                    "pruned": 0,
                    "stamped": 0,
                    "skipped": 0,
                    "entries": [],
                },
            ),
        )
        args = _parse(["code", "prune", "--all", "--force", "--port", "1"])
        assert args.func(args) == 0
        assert seen == {"slug": None, "repo_path": None, "dry_run": True, "force": True}

    def test_non_tty_real_deletion_refuses_without_yes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(port: int) -> httpx.Client:
            raise AssertionError("HTTP call must not happen before the guard")

        monkeypatch.setattr(code_mod, "_make_client", _boom)
        args = _parse(["code", "prune", "org__repo", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "ERROR: Refusing to prune" in err
        assert "--yes" in err

    def test_tty_prompt_declined_cancels(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Simulate a TTY: the guard then prompts; a "n" cancels with exit 0
        # and no endpoint call.
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")

        def _boom(port: int) -> httpx.Client:
            raise AssertionError("HTTP call must not happen on a declined prompt")

        monkeypatch.setattr(code_mod, "_make_client", _boom)
        args = _parse(["code", "prune", "org__repo", "--port", "1"])
        assert args.func(args) == 0
        assert "Cancelled." in capsys.readouterr().out

    def test_module_off_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "healthy", "modules": {"code_index": "disabled"}}
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 1
        assert "ERROR: The code-index module is disabled" in capsys.readouterr().err

    def test_service_down_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 1
        err = capsys.readouterr().err
        assert "ERROR: Cannot reach the agentalloy service" in err
        assert "FIX:" in err

    def test_gone_path_resolves_via_registry(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        # The checkout is gone: the shared _resolve_repo_slug would treat a
        # non-existent path as a bare slug (404). The prune resolver must
        # resolve it against the registry instead.
        gone = tmp_path / "worktrees" / "demo"  # never created on disk
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            if request.url.path == "/code/repos":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "slug": "demo",
                            "repo_path": str(gone),
                            "is_stale": False,
                            "watch_enabled": False,
                        }
                    ],
                )
            assert request.url.path == "/code/prune"
            seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "dry_run": False,
                    "forced": False,
                    "total": 1,
                    "pruned": 0,
                    "stamped": 1,
                    "skipped": 0,
                    "entries": [{"slug": "demo", "repo_path": str(gone), "verdict": "stamped"}],
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", str(gone), "--yes", "--port", "1"])
        assert args.func(args) == 0
        assert seen["slug"] == "demo"  # registry match, not the raw path
        assert seen["repo_path"] == str(gone)  # carried to disambiguate sibling checkouts

    def test_bare_slug_short_circuits_without_registry(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No separator, not a directory: treated as a slug, no /code/repos call.
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json=_HEALTH_ENABLED)
            assert request.url.path == "/code/prune"
            return httpx.Response(
                200,
                json={
                    "dry_run": False,
                    "forced": False,
                    "total": 1,
                    "pruned": 0,
                    "stamped": 1,
                    "skipped": 0,
                    "entries": [{"slug": "org__repo", "repo_path": "/x", "verdict": "stamped"}],
                },
            )

        _mock_client(monkeypatch, handler)
        args = _parse(["code", "prune", "org__repo", "--yes", "--port", "1"])
        assert args.func(args) == 0
        assert "/code/repos" not in paths
