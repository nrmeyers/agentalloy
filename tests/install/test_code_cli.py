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
        return {
            "slug": "org__repo",
            "repo_path": path,
            "last_indexed_at": 1,
            "head_sha": sha,
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
