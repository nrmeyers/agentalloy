"""Unit tests for the ``config`` subcommand (task 01-config-cli).

Maps to docs/design/knowledge-management-production/test-plan.md.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agentalloy.install import state as install_state
from agentalloy.install.subcommands import config as config_cmd


def _run(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    config_cmd.add_parser(parser.add_subparsers(dest="subcommand"))
    args = parser.parse_args(["config", *argv])
    return args


class TestStatus:
    def test_status_defaults_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = _run("status")
        assert config_cmd.run(args) == 0
        out = capsys.readouterr().out
        assert "knowledge-graph: False" in out
        assert "code-index: False" in out

    def test_status_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = _run("status", "--json")
        config_cmd.run(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"KNOWLEDGE_GRAPH_ENABLED": "False", "CODE_INDEX_ENABLED": "False"}


class TestEnableDisable:
    def test_enable_sets_env_var(self) -> None:
        args = _run("enable", "knowledge-graph")
        config_cmd.run(args)
        assert install_state.parse_env_file()["KNOWLEDGE_GRAPH_ENABLED"] == "True"

    def test_disable_sets_env_var(self) -> None:
        config_cmd.run(_run("enable", "knowledge-graph"))
        config_cmd.run(_run("disable", "knowledge-graph"))
        assert install_state.parse_env_file()["KNOWLEDGE_GRAPH_ENABLED"] == "False"

    def test_enable_is_idempotent(self, capsys: pytest.CaptureFixture[str]) -> None:
        config_cmd.run(_run("enable", "knowledge-graph"))
        capsys.readouterr()
        args = _run("enable", "knowledge-graph", "--json")
        config_cmd.run(args)
        payload = json.loads(capsys.readouterr().out)
        assert payload["changed"] is False

    def test_only_touches_its_own_key(self) -> None:
        """Toggling one feature must not disturb other configured features."""
        install_state.upsert_env_file({"CODE_INDEX_ENABLED": "True"})
        config_cmd.run(_run("enable", "knowledge-graph"))
        env = install_state.parse_env_file()
        assert env["CODE_INDEX_ENABLED"] == "True"
        assert env["KNOWLEDGE_GRAPH_ENABLED"] == "True"

    def test_preserves_comments_and_unrelated_keys(self) -> None:
        """Regression guard: the CLI must upsert, never regenerate the whole file."""
        env_path = install_state.env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("# hand-written comment\nUPSTREAM_MODEL=gpt-4\n")

        config_cmd.run(_run("enable", "knowledge-graph"))

        content = env_path.read_text()
        assert "# hand-written comment" in content
        assert "UPSTREAM_MODEL=gpt-4" in content
        assert "KNOWLEDGE_GRAPH_ENABLED=True" in content

    def test_env_file_permissions(self) -> None:
        config_cmd.run(_run("enable", "knowledge-graph"))
        mode = install_state.env_path().stat().st_mode & 0o777
        assert mode == 0o600


class TestCLIWiring:
    def test_config_registered_in_main_parser(self) -> None:
        """Regression guard: `config` must be wired into the real dispatcher.

        The subcommand module previously existed but was never added to
        install/__main__.py's _SUBCOMMANDS list, so `agentalloy config ...`
        silently failed to parse at all.
        """
        from agentalloy.install.__main__ import build_parser

        parser = build_parser()
        args = parser.parse_args(["config", "status"])
        assert args.subcommand == "config"
        assert args.func is config_cmd.run
