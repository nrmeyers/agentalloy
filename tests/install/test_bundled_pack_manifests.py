"""Validate every bundled pack manifest against its skill files.

Guards against manifest drift (fragment_count / skill_id out of sync with
the YAML files) in the packs that ship inside the wheel. Drift makes
``install-packs`` fail for every user, so it must be caught at CI time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.install.subcommands.install_pack import _read_pack_manifest

PACKS_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentalloy" / "_packs"

PACK_DIRS = sorted(d for d in PACKS_ROOT.iterdir() if (d / "pack.yaml").is_file())


@pytest.mark.parametrize("pack_dir", PACK_DIRS, ids=lambda d: d.name)
def test_bundled_pack_manifest_is_valid(pack_dir: Path) -> None:
    manifest, errors = _read_pack_manifest(pack_dir)
    assert manifest is not None, f"{pack_dir.name}: manifest failed to parse"
    assert not errors, f"{pack_dir.name}: {errors}"


def test_all_bundled_packs_discovered() -> None:
    # If the packs tree moves, the parametrized test above would silently
    # collect nothing — make emptiness itself a failure.
    assert len(PACK_DIRS) >= 30, f"only found {len(PACK_DIRS)} bundled packs"
