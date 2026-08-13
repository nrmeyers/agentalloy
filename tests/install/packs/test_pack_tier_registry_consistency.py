"""Registry consistency tests — guards against drift between install_pack,
migrate-seeds-to-packs PACK_TIERS, and PACK_METADATA.

Phase A scaffolding: these three asserts catch the 'registry drift' risk
described in the routing reform plan.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from agentalloy.install.subcommands.install_pack import (
    _VALID_PACK_TIERS,  # type: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Load migrate-seeds-to-packs.py as a module without side effects
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "migrate-seeds-to-packs.py"


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location("migrate_seeds", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_migrate = _load_migrate_module()
PACK_TIERS: dict[str, str] = _migrate.PACK_TIERS  # type: ignore[reportUnknownVariableType]
PACK_METADATA: dict[str, Any] = _migrate.PACK_METADATA  # type: ignore[reportUnknownVariableType]


class TestPackTierRegistryConsistency:
    def test_valid_pack_tiers_covers_all_pack_tier_values(self) -> None:
        """_VALID_PACK_TIERS must contain every tier value used in PACK_TIERS."""
        used_tiers = set(PACK_TIERS.values())
        missing = used_tiers - _VALID_PACK_TIERS
        assert not missing, (
            f"PACK_TIERS uses tier(s) not in _VALID_PACK_TIERS: {sorted(missing)}. "
            f"Add them to install_pack._VALID_PACK_TIERS."
        )

    def test_pack_tiers_keys_all_have_metadata(self) -> None:
        """Every pack in PACK_TIERS must have an entry in PACK_METADATA."""
        missing = set(PACK_TIERS.keys()) - set(PACK_METADATA.keys())
        assert not missing, (
            f"Pack(s) in PACK_TIERS but missing from PACK_METADATA: {sorted(missing)}. "
            f"Add metadata entries in migrate-seeds-to-packs.py."
        )

    def test_pack_metadata_keys_all_have_tiers(self) -> None:
        """Every pack in PACK_METADATA must have an entry in PACK_TIERS."""
        missing = set(PACK_METADATA.keys()) - set(PACK_TIERS.keys())
        assert not missing, (
            f"Pack(s) in PACK_METADATA but missing from PACK_TIERS: {sorted(missing)}. "
            f"Add tier entries in migrate-seeds-to-packs.py."
        )

    def test_pack_rules_targets_all_have_metadata(self) -> None:
        """Every routing target in PACK_RULES must have an entry in PACK_METADATA."""
        rule_targets = {pack for _, pack in _migrate.PACK_RULES}
        missing = rule_targets - set(PACK_METADATA.keys())
        assert not missing, (
            f"PACK_RULES routes to packs missing from PACK_METADATA: {sorted(missing)}. "
            f"Add metadata entries in migrate-seeds-to-packs.py."
        )
