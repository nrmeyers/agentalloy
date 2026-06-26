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

from agentalloy.install import host_sanitize
from agentalloy.install import runtime_artifacts as ra
from agentalloy.install.host_sanitize import SanitizeReport
from agentalloy.install.runtime_artifacts import Action, Orphan
from agentalloy.install.subcommands import cleanup


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"dry_run": False, "yes": False, "json": True, "deep": False}
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


# ---------------------------------------------------------------------------
# --deep (state-independent host sanitize) — driven at the host_sanitize seam
# ---------------------------------------------------------------------------


def _fake_sanitize(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan: list[Action],
    live: list[Action],
    cli_hint: str | None = "uv tool uninstall agentalloy",
) -> list[bool]:
    """Patch ``host_sanitize.sanitize`` and record the ``dry_run`` of each call."""
    calls: list[bool] = []

    def fake(*, dry_run: bool, scan_home: bool) -> SanitizeReport:
        calls.append(dry_run)
        actions = plan if dry_run else live
        return SanitizeReport(actions=list(actions), warnings=[], cli_hint=cli_hint)

    monkeypatch.setattr(host_sanitize, "sanitize", fake)
    return calls


def test_deep_dry_run_plans_without_executing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_sanitize(
        monkeypatch,
        plan=[Action("remove_dir", "/d/agentalloy", "would remove /d/agentalloy", False)],
        live=[],
    )

    rc = cleanup._run(_args(deep=True, dry_run=True))
    out = _out(capsys)

    assert rc == 0
    assert out["deep"] is True and out["dry_run"] is True
    assert out["plan"][0]["op"] == "remove_dir"
    assert out["cli_hint"] == "uv tool uninstall agentalloy"
    assert calls == [True]  # only the dry-run pass — nothing executed


def test_deep_confirm_executes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_sanitize(
        monkeypatch,
        plan=[Action("remove_dir", "/d/agentalloy", "would remove", False)],
        live=[Action("remove_dir", "/d/agentalloy", "removed /d/agentalloy", True)],
    )
    monkeypatch.setattr(cleanup, "_confirm", lambda: True)

    rc = cleanup._run(_args(deep=True, yes=False))
    out = _out(capsys)

    assert rc == 0
    assert out["cancelled"] is False
    assert [a["op"] for a in out["executed"]] == ["remove_dir"]
    assert calls == [True, False]  # plan, then live execute


def test_deep_declined_confirm_does_not_execute(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_sanitize(
        monkeypatch,
        plan=[Action("remove_dir", "/d/agentalloy", "would remove", False)],
        live=[Action("remove_dir", "/d/agentalloy", "removed", True)],
    )
    monkeypatch.setattr(cleanup, "_confirm", lambda: False)

    rc = cleanup._run(_args(deep=True, yes=False))
    out = _out(capsys)

    assert rc == 0
    assert out["cancelled"] is True
    assert calls == [True]  # the live pass never ran


def test_deep_yes_skips_confirm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_sanitize(
        monkeypatch,
        plan=[Action("remove_dir", "/d/agentalloy", "would remove", False)],
        live=[Action("remove_dir", "/d/agentalloy", "removed", True)],
    )

    def boom() -> bool:
        raise AssertionError("must not prompt with --yes")

    monkeypatch.setattr(cleanup, "_confirm", boom)

    rc = cleanup._run(_args(deep=True, yes=True))
    out = _out(capsys)

    assert rc == 0
    assert [a["op"] for a in out["executed"]] == ["remove_dir"]
    assert calls == [True, False]


def test_deep_nothing_to_do_skips_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _fake_sanitize(monkeypatch, plan=[], live=[])

    def boom() -> bool:
        raise AssertionError("must not prompt when host is already clean")

    monkeypatch.setattr(cleanup, "_confirm", boom)

    rc = cleanup._run(_args(deep=True, yes=False))
    out = _out(capsys)

    assert rc == 0
    assert out["cancelled"] is False
    assert out["executed"] == []
    assert calls == [True]  # planned clean → no live pass
