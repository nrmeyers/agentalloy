"""Run the real ingest validation over every bundled skill YAML.

The manifest-consistency test (test_bundled_pack_manifests) catches
manifest drift; this catches skill files the ingester itself would
reject (bad skill_type, invalid category for the type, missing required
fields). A bundled skill that fails ingest validation fails the whole
pack install for every user, so it must fail CI first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.ingest import _load_yaml, _validate

PACKS_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentalloy" / "_packs"

SKILL_FILES = sorted(p for p in PACKS_ROOT.glob("*/*.yaml") if p.name != "pack.yaml")


@pytest.mark.parametrize("skill_path", SKILL_FILES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_bundled_skill_passes_ingest_validation(skill_path: Path) -> None:
    record = _load_yaml(skill_path)
    errors = _validate(record)
    assert not errors, f"{skill_path.parent.name}/{skill_path.name}: {errors}"


def test_all_bundled_skills_discovered() -> None:
    assert len(SKILL_FILES) >= 300, f"only found {len(SKILL_FILES)} bundled skills"
