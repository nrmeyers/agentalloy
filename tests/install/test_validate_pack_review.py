"""validate-pack's Gate 1.5 dry-run reporting.

validate-pack predicts install-pack's outcome under the *current* environment:
it always reports the review status, but only flips the pack to `invalid` when
the gate is active (AGENTALLOY_INSTALL_REQUIRE_REVIEW=1). Pure — zero side effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentalloy.install.subcommands import validate_pack as vp
from agentalloy.pack_validation import skill_file_sha256
from tests.install.test_install_local_pack import _write_pack_manifest, _write_skill_yaml

REQUIRE = "AGENTALLOY_INSTALL_REQUIRE_REVIEW"


def _clean_pack(tmp_path: Path) -> None:
    _write_skill_yaml(tmp_path, "a")
    _write_pack_manifest(tmp_path, "x", [{"skill_id": "a", "file": "a.yaml", "fragment_count": 3}])


def _write_review(tmp_path: Path, **overrides: Any) -> None:
    entry: dict[str, Any] = {
        "skill_id": "a",
        "target_hash": skill_file_sha256(tmp_path, "a.yaml"),
        "verdict": "approve",
        "blocking_issues": [],
        "checks": {"R1": "pass"},
        "reviewer": {"mode": "independent"},
    }
    entry.update(overrides)
    (tmp_path / "review.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "reviews": [entry]}), encoding="utf-8"
    )


def test_review_disabled_by_default_reports_but_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(REQUIRE, raising=False)
    _clean_pack(tmp_path)  # no review.yaml
    result = vp.validate_pack(tmp_path, strict=True)
    assert result["action"] == "valid"  # Gate 1 clean; review disabled → no block
    assert result["review"]["status"] == "disabled"
    assert result["review"]["ok"] is False  # raw result still surfaced in JSON


def test_review_enabled_missing_verdict_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _clean_pack(tmp_path)  # no review.yaml
    result = vp.validate_pack(tmp_path, strict=True)
    assert result["action"] == "invalid"
    assert result["ok"] is False
    assert result["review"]["status"] == "failed"
    assert result["review"]["blocks"] is True


def test_review_enabled_valid_verdict_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _clean_pack(tmp_path)
    _write_review(tmp_path)
    result = vp.validate_pack(tmp_path, strict=True)
    assert result["action"] == "valid"
    assert result["review"]["status"] == "passed"


def test_review_enabled_rejecting_verdict_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _clean_pack(tmp_path)
    _write_review(tmp_path, verdict="reject")
    result = vp.validate_pack(tmp_path, strict=True)
    assert result["action"] == "invalid"
    assert result["review"]["status"] == "failed"


def test_zero_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE, "1")
    _clean_pack(tmp_path)
    _write_review(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    vp.validate_pack(tmp_path, strict=True)
    vp.validate_pack(tmp_path, strict=True)  # idempotent
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after  # no store/db/lock files created


def test_module_has_no_llm_or_network_imports() -> None:
    src = Path(vp.__file__).read_text(encoding="utf-8")
    assert "lm_client" not in src
    assert "httpx" not in src
    assert "requests" not in src
