"""Tests for the state-independent host sanitizer (``cleanup --deep`` engine).

Driven at the subprocess / ``runtime_artifacts`` / filesystem seams so nothing
touches the real host. The load-bearing assertions are the foreign-safety ones:
a llama-server, shim, or model that predates AgentAlloy is never removed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentalloy.install import host_sanitize as hs


class _Completed:
    """Stand-in for ``subprocess.CompletedProcess`` (only the fields we read)."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Foreign-safety: the deep llama-server catch-all
# ---------------------------------------------------------------------------


def test_orphan_scan_only_matches_our_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = Path("/home/u/.local/share/agentalloy")
    self_pid = os.getpid()
    ps_out = "\n".join(
        [
            f"111 /usr/bin/llama-server --model {data_dir}/models/nomic.gguf --port 47951",  # ours
            "222 /usr/bin/llama-server --model /opt/models/foreign.gguf --port 8080",  # FOREIGN
            f"{self_pid} /usr/bin/llama-server --model {data_dir}/models/x.gguf",  # our own pid
            "333 /usr/bin/uvicorn agentalloy.app",  # not a llama-server
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed(stdout=ps_out))

    pids = hs._orphan_llama_servers(data_dir)

    # Foreign server (no data-dir path) and our own process are excluded; only the
    # orphan referencing our data dir is returned.
    assert pids == [111]


def test_orphan_scan_best_effort_on_ps_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> _Completed:
        raise OSError("ps not found")

    monkeypatch.setattr(subprocess, "run", boom)
    assert hs._orphan_llama_servers(Path("/x/agentalloy")) == []


def test_sanitize_reaps_runtimes_and_spares_foreign_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentalloy.install.runtime_artifacts import Action

    # reap is the foreign-safe primitive; a warn_foreign action must surface as a
    # warning (reported, never killed), real actions must surface as actions.
    reaped = [
        Action("remove_shim", "/bin/llama-server", "removed shim", True),
        Action("warn_foreign", "pid://9", ":47951 held by foreign pid 9 — left running", False),
    ]
    monkeypatch.setattr(
        hs.runtime_artifacts, "reap", lambda scope, *, dry_run, stale_only=False: list(reaped)
    )
    monkeypatch.setattr(hs, "_orphan_llama_servers", lambda data_dir: [])
    monkeypatch.setattr(hs, "_teardown_containers", lambda *, dry_run: ([], []))
    monkeypatch.setattr(hs, "_sweep_wiring", lambda *, dry_run, scan_home: [])
    monkeypatch.setattr(hs, "_cli_hint", lambda: None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    report = hs.sanitize(dry_run=True, scan_home=False)

    assert any(a.op == "remove_shim" for a in report.actions)
    assert "foreign pid 9" in " ".join(report.warnings)
    assert all(a.op != "warn_foreign" for a in report.actions)


# ---------------------------------------------------------------------------
# Container teardown (state-independent, by fixed name)
# ---------------------------------------------------------------------------


def test_teardown_removes_present_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}" if c == "podman" else None)
    issued: list[list[str]] = []

    def fake_run(cmd: list[str], **k: Any) -> _Completed:
        issued.append(cmd)
        return _Completed(returncode=0)  # inspect present + rm succeeds

    monkeypatch.setattr(subprocess, "run", fake_run)

    actions, warnings = hs._teardown_containers(dry_run=False)

    rm_cmds = [c for c in issued if "inspect" not in c]
    assert ["podman", "rm", "-f", "agentalloy"] in rm_cmds
    assert ["podman", "volume", "rm", "-f", "agentalloy-data"] in rm_cmds
    assert ["podman", "rmi", "-f", hs._IMAGE] in rm_cmds
    assert warnings == []
    assert {a.op for a in actions} == {"container_rm", "volume_rm", "image_rm"}


def test_teardown_skips_absent_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}" if c == "podman" else None)

    def fake_run(cmd: list[str], **k: Any) -> _Completed:
        if "inspect" in cmd:
            return _Completed(returncode=1, stderr="no such object")
        raise AssertionError(f"must not issue a remove for an absent artifact: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    actions, warnings = hs._teardown_containers(dry_run=False)
    assert actions == [] and warnings == []


def test_teardown_no_runtime_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: None)
    actions, warnings = hs._teardown_containers(dry_run=False)
    assert actions == [] and warnings == []


def test_teardown_real_rm_failure_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: f"/usr/bin/{c}" if c == "podman" else None)

    def fake_run(cmd: list[str], **k: Any) -> _Completed:
        if "inspect" in cmd:
            return _Completed(returncode=0)  # present
        return _Completed(returncode=125, stderr="permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _actions, warnings = hs._teardown_containers(dry_run=False)
    assert any("permission denied" in w for w in warnings)


# ---------------------------------------------------------------------------
# Carrier detection + $HOME scan
# ---------------------------------------------------------------------------


def test_is_claude_carrier_only_true_for_our_proxy(tmp_path: Path) -> None:
    ours = tmp_path / "ours.json"
    ours.write_text('{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:47950/proj/a/v1"}}')
    theirs = tmp_path / "theirs.json"
    theirs.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://api.anthropic.com"}}')
    junk = tmp_path / "junk.json"
    junk.write_text("not json at all")

    assert hs._is_claude_carrier(ours) is True
    assert hs._is_claude_carrier(theirs) is False  # user's own base URL — untouched
    assert hs._is_claude_carrier(junk) is False
    assert hs._is_claude_carrier(tmp_path / "missing.json") is False


def test_scan_home_finds_carrier_and_prunes_heavy_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    carrier_repo = home / "work" / "proj"
    other_repo = home / "work" / "other"
    pruned_repo = carrier_repo / "node_modules" / "pkg"  # must be pruned
    for repo, url in [
        (carrier_repo, "http://h/proj/a/v1"),
        (other_repo, "https://api.anthropic.com"),  # not ours
        (pruned_repo, "http://h/proj/b/v1"),  # ours, but under node_modules
    ]:
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": url}})
        )
    monkeypatch.setenv("HOME", str(home))

    found = {p.resolve() for p in hs._scan_home_carriers()}

    assert carrier_repo.resolve() in found
    assert other_repo.resolve() not in found  # non-carrier left alone
    assert pruned_repo.resolve() not in found  # node_modules never descended


# ---------------------------------------------------------------------------
# Filesystem purge ordering — config dir removed LAST (state read first)
# ---------------------------------------------------------------------------


def _seed_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    xdg_data = tmp_path / "xdgdata"
    xdg_config = tmp_path / "xdgconfig"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

    data_dir = xdg_data / "agentalloy"
    config_dir = xdg_config / "agentalloy"
    cache_dir = home / ".cache" / "agentalloy"
    dot_dir = home / ".agentalloy"
    for d in (data_dir / "models", config_dir, cache_dir, dot_dir):
        d.mkdir(parents=True)
    (data_dir / "models" / "x.gguf").write_text("blob")

    # A wired repo recorded in install-state, with a Claude Code carrier.
    repo = home / "myrepo"
    (repo / ".claude").mkdir(parents=True)
    carrier = repo / ".claude" / "settings.local.json"
    carrier.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:47950/proj/z/v1"}})
    )
    (config_dir / "install-state.json").write_text(
        json.dumps(
            {
                "schema_version": 5,
                "harness_files_written": [
                    {
                        "path": str(repo / "CLAUDE.md"),
                        "repo_root": str(repo),
                        "harness": "claude-code",
                    }
                ],
            }
        )
    )
    return {
        "data": data_dir,
        "config": config_dir,
        "cache": cache_dir,
        "dot": dot_dir,
        "carrier": carrier,
    }


def test_sanitize_live_purges_all_dirs_after_reading_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _seed_install(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hs.runtime_artifacts, "reap", lambda scope, *, dry_run, stale_only=False: []
    )
    monkeypatch.setattr(hs, "_orphan_llama_servers", lambda data_dir: [])
    monkeypatch.setattr(hs, "_teardown_containers", lambda *, dry_run: ([], []))
    monkeypatch.setattr(hs, "_cli_hint", lambda: None)

    report = hs.sanitize(dry_run=False, scan_home=False)

    # The carrier was removed — which is only possible if install-state was read
    # (and so the config dir still existed) BEFORE the config dir was purged.
    assert not paths["carrier"].exists()
    assert any(a.op == "unwire_repo" for a in report.actions)
    # Every agentalloy directory is gone.
    for key in ("data", "config", "cache", "dot"):
        assert not paths[key].exists(), f"{key} dir should be removed"


def test_sanitize_dry_run_mutates_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _seed_install(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hs.runtime_artifacts, "reap", lambda scope, *, dry_run, stale_only=False: []
    )
    monkeypatch.setattr(hs, "_orphan_llama_servers", lambda data_dir: [])
    monkeypatch.setattr(hs, "_teardown_containers", lambda *, dry_run: ([], []))
    monkeypatch.setattr(hs, "_cli_hint", lambda: None)

    report = hs.sanitize(dry_run=True, scan_home=False)

    # Plan describes removals but nothing is touched.
    assert any(a.op == "remove_dir" for a in report.actions)
    assert all(a.executed is False for a in report.actions)
    for key in ("data", "config", "cache", "dot", "carrier"):
        assert paths[key].exists(), f"{key} must be untouched by a dry run"
