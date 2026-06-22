"""Tests for the ``agentalloy cleanup`` subcommand.

Driven at the ``runtime_artifacts`` seam: ``reap`` / ``detect_orphans`` are
patched so these tests assert the command's orchestration (dry-run vs execute,
confirm gating, action→executed/warning mapping) without touching the host.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from agentalloy.install import runtime_artifacts as ra
from agentalloy.install.runtime_artifacts import Action, Orphan
from agentalloy.install.subcommands import cleanup


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"dry_run": False, "yes": False, "json": True}
    base.update(kw)
    return argparse.Namespace(**base)


def _out(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_dry_run_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = [Action("remove_unit", "/u/agentalloy-embed.service", "disable + remove", False)]
    reap_calls: list[tuple[str, bool]] = []

    def fake_reap(scope: str, *, dry_run: bool = False, stale_only: bool = False) -> list[Action]:
        reap_calls.append((scope, dry_run))
        return plan if dry_run else []

    monkeypatch.setattr(ra, "reap", fake_reap)
    monkeypatch.setattr(ra, "detect_orphans", lambda: [])

    rc = cleanup._run(_args(dry_run=True))
    out = _out(capsys)

    assert rc == 0
    assert out["dry_run"] is True
    assert out["plan"][0]["op"] == "remove_unit"
    # Only the dry-run reap ran — the executing one was never called.
    assert reap_calls == [("all", True)]


def test_yes_executes_and_maps_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executed = [
        Action("stop_process", "pid://1", "stopped uvicorn on :47950 (pid 1)", True),
        Action("warn_foreign", "pid://2", ":47951 held by foreign pid 2 — left running", False),
    ]

    def fake_reap(scope: str, *, dry_run: bool = False, stale_only: bool = False) -> list[Action]:
        return [] if dry_run else executed

    monkeypatch.setattr(ra, "reap", fake_reap)
    monkeypatch.setattr(ra, "detect_orphans", lambda: [])

    rc = cleanup._run(_args(yes=True))
    out = _out(capsys)

    assert rc == 0
    assert out["cancelled"] is False
    assert [a["op"] for a in out["executed"]] == ["stop_process"]
    assert [w["op"] for w in out["warnings"]] == ["warn_foreign"]


def test_declined_confirm_does_not_reap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = [Action("remove_shim", "/bin/llama-server", "remove shim", False)]
    exec_calls: list[str] = []

    def fake_reap(scope: str, *, dry_run: bool = False, stale_only: bool = False) -> list[Action]:
        if not dry_run:
            exec_calls.append(scope)
            return []
        return plan

    monkeypatch.setattr(ra, "reap", fake_reap)
    monkeypatch.setattr(ra, "detect_orphans", lambda: [])
    monkeypatch.setattr(cleanup, "_confirm", lambda: False)

    rc = cleanup._run(_args(yes=False))
    out = _out(capsys)

    assert rc == 0
    assert out["cancelled"] is True
    assert exec_calls == []  # nothing executed after a declined prompt


def test_nothing_to_do_skips_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ra, "reap", lambda scope, *, dry_run=False, stale_only=False: [])
    monkeypatch.setattr(ra, "detect_orphans", lambda: [])

    def boom() -> bool:
        raise AssertionError("must not prompt when there is nothing to do")

    monkeypatch.setattr(cleanup, "_confirm", boom)

    rc = cleanup._run(_args(yes=False))
    out = _out(capsys)

    assert rc == 0
    assert out["executed"] == []
    assert out["cancelled"] is False


def test_conflicts_surface_in_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ra, "reap", lambda scope, *, dry_run=False, stale_only=False: [])
    monkeypatch.setattr(
        ra,
        "detect_orphans",
        lambda: [Orphan("conflict", ":47950 held by foreign pid 9", port=47950, pid=9)],
    )

    rc = cleanup._run(_args(dry_run=True))
    out = _out(capsys)

    assert rc == 0
    assert out["conflicts"][0]["kind"] == "conflict"
