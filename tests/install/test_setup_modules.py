"""Setup-wizard module selection → env overrides for write-env."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from agentalloy.install.subcommands import simple_setup


class TestModuleOverrides:
    def test_injector_default(self) -> None:
        assert simple_setup._module_env_overrides("injector") == [  # pyright: ignore[reportPrivateUsage]
            "COMPOSE_ENABLED=1",
            "CODE_INDEX_ENABLED=0",
        ]

    def test_code_index_only(self) -> None:
        assert simple_setup._module_env_overrides("code-index") == [  # pyright: ignore[reportPrivateUsage]
            "COMPOSE_ENABLED=0",
            "CODE_INDEX_ENABLED=1",
        ]

    def test_both(self) -> None:
        assert simple_setup._module_env_overrides("both") == [  # pyright: ignore[reportPrivateUsage]
            "COMPOSE_ENABLED=1",
            "CODE_INDEX_ENABLED=1",
        ]

    def test_overrides_are_valid_write_env_keys(self) -> None:
        from agentalloy.install.subcommands.write_env import (
            _parse_overrides,  # pyright: ignore[reportPrivateUsage]
        )

        for modules in ("injector", "code-index", "both"):
            parsed = _parse_overrides(simple_setup._module_env_overrides(modules))  # pyright: ignore[reportPrivateUsage]
            assert set(parsed) == {"COMPOSE_ENABLED", "CODE_INDEX_ENABLED"}


class TestModulePrompt:
    def test_non_tty_defaults_to_injector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # pytest stdin is not a TTY — _prompt_numbered returns the default value.
        assert simple_setup._prompt_modules() == "injector"  # pyright: ignore[reportPrivateUsage]

    def test_setup_config_default_is_injector(self) -> None:
        assert simple_setup.SetupConfig().modules == "injector"

    def test_cli_flag_threads_into_config(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        simple_setup.add_parser(sub)
        args = parser.parse_args(["setup", "--modules", "both", "-n"])
        assert args.modules == "both"

    def test_cli_flag_default_is_none_maps_to_injector(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        simple_setup.add_parser(sub)
        args = parser.parse_args(["setup", "-n"])
        assert args.modules is None


class TestImportCheck:
    def test_importable_in_this_env(self) -> None:
        assert simple_setup._verify_code_index_importable() is True  # pyright: ignore[reportPrivateUsage]

    def test_import_error_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        real_import_module = importlib.import_module

        def _boom(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "agentalloy.code_index.api":
                raise ImportError("No module named 'tree_sitter'")
            return real_import_module(name, *args, **kwargs)

        monkeypatch.setattr(importlib, "import_module", _boom)
        assert simple_setup._verify_code_index_importable() is False  # pyright: ignore[reportPrivateUsage]
