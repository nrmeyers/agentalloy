"""Tests for `agentalloy customize revalidate` and the enabled-column migration.

Re-validation re-derives each override's load-bearing invariants from the
(possibly upgraded) shipped skills and disables — never deletes — profile
overrides whose prose has gone stale. Project overrides are warn-only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import yaml
from pytest import CaptureFixture

from agentalloy.install.subcommands.customize import (
    _ingest_skill,  # pyright: ignore[reportPrivateUsage]
    _open_profile_store,  # pyright: ignore[reportPrivateUsage]
    _revalidate,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.signals.invariants import derive_invariants, load_shipped_skill

# A real shipped workflow skill with derivable invariants.
_BAD = "sdd-design-and-planning"
_GOOD = "sdd-build"
BAD_PROSE = "Reworded guidance dropping every load-bearing path and command. " * 3


def _ns() -> argparse.Namespace:
    return argparse.Namespace(json=True)


def _good_prose(skill_id: str) -> str:
    shipped = load_shipped_skill(skill_id)
    assert shipped is not None
    return "Reworded guidance retaining every token: " + " ".join(derive_invariants(shipped))


def _enabled_map(db: Path) -> dict[str, bool]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        rows = con.execute("SELECT skill_id, enabled FROM profile_skills").fetchall()
    finally:
        con.close()
    return {str(r[0]): bool(r[1]) for r in rows}


def _seed(base: Path) -> Path:
    db = base / "profiles" / "p" / "skills.duck"
    db.parent.mkdir(parents=True)
    return db


def test_revalidate_disables_only_violating_override(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    from unittest.mock import patch

    db = _seed(tmp_path)
    with (
        patch("agentalloy.profiles.list_profiles", return_value=[{"name": "p"}]),
        patch("agentalloy.profiles.profile_datastore_path", return_value=db),
    ):
        _ingest_skill(
            "p",
            {"skill_id": _GOOD, "skill_class": "workflow", "raw_prose": _good_prose(_GOOD)},
        )
        _ingest_skill(
            "p",
            {"skill_id": _BAD, "skill_class": "workflow", "raw_prose": BAD_PROSE},
        )
        rc = _revalidate(_ns())

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["disabled"] == 1
    enabled = _enabled_map(db)
    assert enabled[_GOOD] is True  # valid override untouched
    assert enabled[_BAD] is False  # stale override disabled
    assert any(_BAD in w and "disabled" in w for w in out["warnings"])


def test_revalidate_update_reenables(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from unittest.mock import patch

    db = _seed(tmp_path)
    with (
        patch("agentalloy.profiles.list_profiles", return_value=[{"name": "p"}]),
        patch("agentalloy.profiles.profile_datastore_path", return_value=db),
    ):
        _ingest_skill("p", {"skill_id": _BAD, "skill_class": "workflow", "raw_prose": BAD_PROSE})
        _revalidate(_ns())
        assert _enabled_map(db)[_BAD] is False
        # Fix the prose and re-ingest: INSERT OR REPLACE resets enabled to default true.
        _ingest_skill(
            "p", {"skill_id": _BAD, "skill_class": "workflow", "raw_prose": _good_prose(_BAD)}
        )
        assert _enabled_map(db)[_BAD] is True


def test_revalidate_orphan_warns_not_disabled(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from unittest.mock import patch

    db = _seed(tmp_path)
    with (
        patch("agentalloy.profiles.list_profiles", return_value=[{"name": "p"}]),
        patch("agentalloy.profiles.profile_datastore_path", return_value=db),
    ):
        _ingest_skill(
            "p",
            {"skill_id": "ghost-skill", "skill_class": "workflow", "raw_prose": "prose " * 30},
        )
        _revalidate(_ns())

    out = json.loads(capsys.readouterr().out)
    assert any("ghost-skill" in w and "orphan" in w for w in out["warnings"])
    assert _enabled_map(db)["ghost-skill"] is True  # retained, not disabled


def test_revalidate_project_override_warn_only(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from unittest.mock import patch

    repo = tmp_path / "repo"
    wf = repo / ".agentalloy" / "skills" / "workflow"
    wf.mkdir(parents=True)
    override = wf / f"{_BAD}.yaml"
    override.write_text(
        yaml.safe_dump({"skill_id": _BAD, "skill_class": "workflow", "raw_prose": BAD_PROSE})
    )
    state = {"harness_files_written": [{"repo_root": str(repo)}]}
    with (
        patch("agentalloy.profiles.list_profiles", return_value=[]),
        patch("agentalloy.install.state.load_state", return_value=state),
    ):
        _revalidate(_ns())

    out = json.loads(capsys.readouterr().out)
    assert any(str(override) in w for w in out["warnings"])
    assert override.exists()  # warn-only: the repo file is never mutated


def test_open_profile_store_migration_idempotent(tmp_path: Path) -> None:
    from unittest.mock import patch

    db = _seed(tmp_path)
    # Pre-feature DB: profile_skills WITHOUT the `enabled` column.
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE profile_skills (skill_id VARCHAR PRIMARY KEY, skill_class VARCHAR, "
        "canonical_name VARCHAR, raw_prose VARCHAR, updated_at BIGINT)"
    )
    con.execute("INSERT INTO profile_skills VALUES ('s', 'workflow', 's', 'prose', 0)")
    con.close()

    with patch("agentalloy.profiles.profile_datastore_path", return_value=db):
        c = _open_profile_store("p")  # runs the ALTER migration
        val = c.execute("SELECT enabled FROM profile_skills WHERE skill_id = 's'").fetchone()
        c.close()
        assert val is not None
        assert val[0] is True  # existing row defaulted to enabled
        # Second open is a no-op (ADD COLUMN IF NOT EXISTS).
        _open_profile_store("p").close()
