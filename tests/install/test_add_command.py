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
