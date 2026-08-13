"""Output-shape guard for the install-lifecycle verbs (NXS — stdout noise).

Each user-facing verb must:
  * print concise human text by default (NOT a raw JSON dump),
  * emit valid JSON under ``--json`` (the machine contract upgrade relies on),
  * print nothing under ``--quiet``.

These tests drive each verb's ``_run`` with its underlying action mocked, so
they assert the output-mode dispatch without touching host state. They lock the
fix for the `unwire`/`update`/`reset` raw-JSON dumps and prevent regressions.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from agentalloy.install.output import progress_activity, render_lifecycle_result
from agentalloy.install.subcommands import reset as reset_cmd
from agentalloy.install.subcommands import unwire as unwire_cmd
from agentalloy.install.subcommands import update as update_cmd

# (label, module, action-attr, canned result, extra namespace kwargs, title, a JSON-only key)
_CASES = [
    (
        "unwire",
        unwire_cmd,
        "uninstall",
        {
            "schema_version": 1,
            "files_modified": [{"path": "/x/settings.json", "action": "restored_original"}],
            "files_removed": [{"path": "/x/hook.sh", "action": "deleted_dedicated_file"}],
            "data_kept": ["/x/corpus"],
            "warnings": [],
        },
        {"force": False},
        "Unwire",
        "schema_version",
    ),
    (
        "update",
        update_cmd,
        "update",
        {
            "schema_version": 1,
            "corpus": {},
            "migrations": [],
            "warnings": ["Corpus predates the schema_version marker; harmless."],
        },
        {},
        "Update",
        "schema_version",
    ),
    (
        "reset",
        reset_cmd,
        "reset",
        {
            "profile": "default",
            "deleted_overrides": ["/x/a.yaml"],
            "reingested_defaults": True,
        },
        {"profile": "default", "all_profiles": False, "include_domain": False, "yes": True},
        "Reset",
        "deleted_overrides",
    ),
]


def _ns(json_mode: bool, quiet: bool, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(json=json_mode, quiet=quiet, **extra)


@pytest.mark.parametrize("label,module,action,result,ns_kwargs,title,json_key", _CASES)
def test_human_by_default(
    label: str,
    module: Any,
    action: str,
    result: dict[str, Any],
    ns_kwargs: dict[str, Any],
    title: str,
    json_key: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, action, lambda *a, **k: result)
    module._run(_ns(False, False, **ns_kwargs))  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert title in out, f"{label}: human output should name the verb"
    # The human render must not be a raw JSON dump of the result.
    assert f'"{json_key}"' not in out, f"{label}: default output leaked raw JSON"
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


@pytest.mark.parametrize("label,module,action,result,ns_kwargs,title,json_key", _CASES)
def test_json_flag_emits_valid_json(
    label: str,
    module: Any,
    action: str,
    result: dict[str, Any],
    ns_kwargs: dict[str, Any],
    title: str,
    json_key: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, action, lambda *a, **k: result)
    module._run(_ns(True, False, **ns_kwargs))  # pyright: ignore[reportPrivateUsage]
    out = capsys.readouterr().out
    assert json.loads(out) == result, f"{label}: --json must round-trip the result"


@pytest.mark.parametrize("label,module,action,result,ns_kwargs,title,json_key", _CASES)
def test_quiet_is_silent(
    label: str,
    module: Any,
    action: str,
    result: dict[str, Any],
    ns_kwargs: dict[str, Any],
    title: str,
    json_key: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, action, lambda *a, **k: result)
    module._run(_ns(False, True, **ns_kwargs))  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().out == "", f"{label}: --quiet must be silent"


# --- render_lifecycle_result unit coverage ---------------------------------


def test_render_no_changes(capsys: pytest.CaptureFixture[str]) -> None:
    render_lifecycle_result({"schema_version": 1, "warnings": []}, "Noop")
    out = capsys.readouterr().out
    assert "Noop" in out
    assert "no changes" in out


def test_render_file_actions_and_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    render_lifecycle_result(
        {
            "files_written": [{"path": "/a"}],
            "files_removed": [{"path": "/b"}],
            "warnings": ["heads up"],
        },
        "Wire",
    )
    out = capsys.readouterr().out
    assert "/a" in out and "/b" in out
    assert "heads up" in out
    assert "no changes" not in out


def test_render_error_shown(capsys: pytest.CaptureFixture[str]) -> None:
    render_lifecycle_result({"error": "boom"}, "Reset")
    out = capsys.readouterr().out
    assert "boom" in out
    assert "no changes" not in out


# --- progress_activity (live spinner for silent long steps) ----------------


def test_progress_activity_disabled_is_pure_noop(capsys: pytest.CaptureFixture[str]) -> None:
    # enabled=False must yield with zero output — keeps --json/--quiet byte-clean.
    ran = False
    with progress_activity("doing a thing", enabled=False):
        ran = True
    assert ran
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_progress_activity_non_tty_prints_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    # Under pytest stdout isn't a TTY, so even enabled=True falls to the no-op
    # path — but it logs one `-> message…` line so non-interactive runs still
    # record what is running instead of a blank wait.
    with progress_activity("installing v9.9.9", enabled=True):
        pass
    out = capsys.readouterr().out
    assert out.count("installing v9.9.9") == 1
    assert "->" in out


def test_progress_activity_reraises(capsys: pytest.CaptureFixture[str]) -> None:
    # The wrapper must not swallow the wrapped step's failure.
    with pytest.raises(RuntimeError, match="swap failed"):
        with progress_activity("installing", enabled=True):
            raise RuntimeError("swap failed")
