"""``agentalloy add <harness>`` — upstream adoption + per-repo interception wiring."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from agentalloy.api.proxy_context import decode_proj_token, read_upstream
from agentalloy.install.subcommands import add


def _global_hermes_config(home: Path, base_url: str = "http://10.0.0.9:60000/v1") -> None:
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    (home / ".hermes" / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {base_url}\n  default: qwen3.6\n"
    )


class TestCaptureUpstream:
    def test_adopts_from_hermes_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        monkeypatch.setattr(Path, "home", lambda: home)

        up = add.capture_upstream("hermes-agent", tmp_path)
        assert up is not None
        assert up.url == "http://10.0.0.9:60000/v1"
        assert up.model == "qwen3.6"
        # And it was recorded for the proxy to read.
        assert read_upstream(tmp_path) == up

    def test_cli_overrides_win(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        monkeypatch.setattr(Path, "home", lambda: home)

        up = add.capture_upstream(
            "hermes-agent", tmp_path, upstream_url="http://override:1/v1", upstream_model="m9"
        )
        assert up is not None
        assert up.url == "http://override:1/v1"
        assert up.model == "m9"

    def test_no_extractor_no_override_is_none(self, tmp_path: Path) -> None:
        # claude-code adopts nothing (auth-transparent passthrough).
        assert add.capture_upstream("claude-code", tmp_path) is None
        assert read_upstream(tmp_path) is None


class TestAddRun:
    def test_add_hermes_captures_and_wires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(repo)

        args = argparse.Namespace(
            harness="hermes-agent",
            port=47950,
            upstream_url=None,
            upstream_model=None,
            key_env=None,
        )
        rc = add._run(args)
        assert rc == 0

        # Upstream adopted from the global hermes config.
        up = read_upstream(repo)
        assert up is not None and up.url == "http://10.0.0.9:60000/v1"

        # Interception wired at the proxy with this repo's /proj token.
        cfg = yaml.safe_load((repo / ".hermes" / "config.yaml").read_text())
        base_url = cfg["model"]["base_url"]
        assert base_url.startswith("http://localhost:47950/proj/")
        token = base_url.split("/proj/")[1].split("/")[0]
        assert decode_proj_token(token) == repo.resolve()
        assert (repo / ".hermes" / ".agentalloy-env").exists()

    def test_add_unknown_harness_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        args = argparse.Namespace(
            harness="nope", port=None, upstream_url=None, upstream_model=None, key_env=None
        )
        assert add._run(args) == 1

    def test_add_default_lifecycle_writes_config_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(repo)

        args = argparse.Namespace(
            harness="hermes-agent",
            port=47950,
            upstream_url=None,
            upstream_model=None,
            key_env=None,
            lifecycle_mode=None,
        )
        assert add._run(args) == 0
        assert (repo / ".agentalloy" / "config").read_text() == "lifecycle_mode: full\n"
        # `full` used to seed the entry phase here. Adopting a harness now
        # writes configuration and nothing else — the proxy seeds the phase on
        # the repo's first request, so this never needs a running service.
        from agentalloy.install.subcommands.status import (
            _repo_phase,  # pyright: ignore[reportPrivateUsage]
        )

        assert _repo_phase(str(repo)) is None

    def test_add_lifecycle_off_leaves_an_existing_phase_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        _global_hermes_config(home)
        repo = tmp_path / "repo"
        (repo / ".agentalloy").mkdir(parents=True)
        from agentalloy.install.subcommands.phase import run_phase_set

        run_phase_set("build", root=repo, force=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.chdir(repo)

        args = argparse.Namespace(
            harness="hermes-agent",
            port=47950,
            upstream_url=None,
            upstream_model=None,
            key_env=None,
            lifecycle_mode="off",
        )
        assert add._run(args) == 0
        assert (repo / ".agentalloy" / "config").read_text() == "lifecycle_mode: off\n"
        # `off` used to clear the stale phase file. It no longer touches
        # lifecycle state at all: the mode guard short-circuits before the phase
        # is ever read, so a leftover row under `off` is inert, and clearing it
        # would mean losing the repo's place if the user turns the mode back on.
        from agentalloy.install.subcommands.status import (
            _repo_phase,  # pyright: ignore[reportPrivateUsage]
        )

        assert _repo_phase(str(repo)) == "build"
