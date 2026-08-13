"""Unit tests for the install CLI dispatcher."""

from __future__ import annotations

import pytest

from agentalloy.install.__main__ import EXIT_USER, build_parser, main


class TestDispatcher:
    def test_no_subcommand_returns_exit_user(self) -> None:
        assert main([]) == EXIT_USER

    def test_detect_subcommand_is_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["detect"])
        assert args.subcommand == "detect"
        assert callable(args.func)

    def test_unknown_subcommand_raises(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["bogus"])
