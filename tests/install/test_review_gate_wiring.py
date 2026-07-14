"""Gate 1.5 wiring in `install_local_pack` — enforcement, escape hatch, posture.

The gate ships dormant (AGENTALLOY_INSTALL_REQUIRE_REVIEW off by default). Block
cases return early at Gate 1.5 (after Gate 1, before ingest), so they need only
`_check_embedding_dim` stubbed. Pass/disabled/bypass cases run through to a
post-gate return (`ingested_with_errors`, which now carries the `gate_1_5` block)
via the same mock harness the outcome-classification test uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentalloy.install.subcommands import install_pack as ip
from agentalloy.pack_validation import skill_file_sha256

# Reuse the lint-clean pack builders from the sibling suite.
from tests.install.test_install_local_pack import _write_pack_manifest, _write_skill_yaml

REQUIRE = "AGENTALLOY_INSTALL_REQUIRE_REVIEW"
INDEPENDENT = "AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW"


def _setup_pack(tmp_path: Path) -> None:
    _write_skill_yaml(tmp_path, "a")
    _write_pack_manifest(tmp_path, "x", [{"skill_id": "a", "file": "a.yaml", "fragment_count": 3}])


def _write_review(tmp_path: Path, **overrides: Any) -> None:
    import yaml

    entry: dict[str, Any] = {
        "skill_id": "a",
        "target_hash": skill_file_sha256(tmp_path, "a.yaml"),
        "verdict": "approve",
        "blocking_issues": [],
        "checks": {"R1": "pass"},
        "reviewer": {"model": "claude-sonnet-5", "harness": "claude-code", "mode": "independent"},
        "created_at": "2026-07-13T00:00:00Z",
    }
    entry.update(overrides)
    (tmp_path / "review.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "reviews": [entry]}), encoding="utf-8"
    )


def _run_to_post_gate(tmp_path: Path, *, allow_unreviewed: bool = False) -> dict[str, Any]:
    """Run install with a single failing ingest so it returns `ingested_with_errors`
    (which carries the gate_1_5 block) — used for pass/disabled/bypass assertions."""
    fake = [
        {"yaml": "a.yaml", "exit_code": 2, "outcome": "failed", "stdout_tail": "", "stderr_tail": "x"}
    ]
    with (
        patch.object(ip, "_check_embedding_dim", return_value=None),
        patch.object(ip, "_ingest_yaml", side_effect=fake),
        patch.object(ip.install_state, "load_state", return_value={}),
        patch.object(ip.install_state, "save_state"),
        patch.object(ip.install_state, "record_step"),
        patch("agentalloy.config.get_settings") as ms,
        patch.object(ip, "open_skills", return_value=MagicMock()),
    ):
        ms.return_value.duckdb_path = str(tmp_path / "t.duck")
        return ip.install_local_pack(
            tmp_path, root=tmp_path, run_reembed=False, allow_unreviewed=allow_unreviewed
        )


# --- dormant by default ----------------------------------------------------


def test_gate_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REQUIRE, raising=False)
    _setup_pack(tmp_path)  # no review.yaml at all
    result = _run_to_post_gate(tmp_path)
    assert result["action"] == "ingested_with_errors"  # reached ingest, gate didn't block
    assert result["gate_1_5"]["status"] == "disabled"


# --- enforcement (block cases return early, no ingest) ---------------------


def test_missing_review_blocks_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _setup_pack(tmp_path)  # no review.yaml
    with patch.object(ip, "_check_embedding_dim", return_value=None):
        result = ip.install_local_pack(tmp_path, root=tmp_path)
    assert result["action"] == "review_failed"
    assert result["errors"][0]["skill_id"] == "a"


def test_stale_review_blocks_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _setup_pack(tmp_path)
    _write_review(tmp_path, target_hash="sha256:stale")
    with patch.object(ip, "_check_embedding_dim", return_value=None):
        result = ip.install_local_pack(tmp_path, root=tmp_path)
    assert result["action"] == "review_failed"


def test_rejecting_review_blocks_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _setup_pack(tmp_path)
    _write_review(tmp_path, verdict="reject")
    with patch.object(ip, "_check_embedding_dim", return_value=None):
        result = ip.install_local_pack(tmp_path, root=tmp_path)
    assert result["action"] == "review_failed"


# --- pass / bypass / independence posture ----------------------------------


def test_valid_review_passes_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _setup_pack(tmp_path)
    _write_review(tmp_path)  # valid, mode=independent
    result = _run_to_post_gate(tmp_path)
    assert result["action"] != "review_failed"  # gate let it through
    assert result["gate_1_5"]["status"] == "passed"
    assert result["gate_1_5"]["modes"] == ["independent"]


def test_allow_unreviewed_bypass_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _setup_pack(tmp_path)  # no review.yaml
    result = _run_to_post_gate(tmp_path, allow_unreviewed=True)
    assert result["action"] != "review_failed"
    assert result["gate_1_5"]["status"] == "bypassed"
    assert result["gate_1_5"]["reason"] == "--allow-unreviewed"


def test_self_mode_blocked_when_independent_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    monkeypatch.setenv(INDEPENDENT, "1")
    _setup_pack(tmp_path)
    _write_review(tmp_path, reviewer={"mode": "self"})
    with patch.object(ip, "_check_embedding_dim", return_value=None):
        result = ip.install_local_pack(tmp_path, root=tmp_path)
    assert result["action"] == "review_failed"
    assert "independent" in result["remediation"].lower()


def test_self_mode_allowed_by_default_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    monkeypatch.delenv(INDEPENDENT, raising=False)
    _setup_pack(tmp_path)
    _write_review(tmp_path, reviewer={"mode": "self"})
    result = _run_to_post_gate(tmp_path)
    assert result["action"] != "review_failed"
    assert result["gate_1_5"]["status"] == "passed"


# --- orchestrator threads the flag -----------------------------------------


def test_install_pack_threads_allow_unreviewed(tmp_path: Path) -> None:
    (tmp_path / "pack.yaml").write_text("name: x\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_route(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"action": "ingested"}

    with patch("agentalloy.install.corpus_write_route.install_or_route", fake_route):
        ip.install_pack(str(tmp_path), root=tmp_path, allow_unreviewed=True)
    assert captured["allow_unreviewed"] is True
