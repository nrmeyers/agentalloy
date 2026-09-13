"""Utility for resolving the pack tier for a skill YAML file.

Walks up from the skill YAML's directory looking for a sibling pack.yaml and
returns the tier string declared in that manifest.  No retrieval behavior —
scaffolding only (Phase A).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def resolve_skill_tier(yaml_path: Path | str) -> tuple[str | None, str]:
    """Return ``(tier, source)`` for the skill at *yaml_path*.

    Walks up from the directory containing *yaml_path* looking for a
    ``pack.yaml`` in that same directory (i.e. a sibling to the skill YAML).
    Stops after 10 levels or at the filesystem root.

    Returns
    -------
    (tier_value, source_label)

    * ``("foundation", "pack.yaml")``        — found, valid tier string
    * ``(None, "pack.yaml:missing")``         — found, no tier key (or non-string value)
    * ``(None, "pack.yaml:parse_error")``     — found, YAML is corrupt / unreadable
    * ``(None, "not_found")``                 — no pack.yaml anywhere in walk

    """
    path = Path(yaml_path).resolve()
    current = path.parent

    for _ in range(10):
        candidate = current / "pack.yaml"
        if candidate.is_file():
            try:
                manifest: dict[str, Any] = (
                    yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                )
            except yaml.YAMLError:
                return (None, "pack.yaml:parse_error")
            tier_val = manifest.get("tier")
            if isinstance(tier_val, str) and tier_val:
                return (tier_val, "pack.yaml")
            return (None, "pack.yaml:missing")

        parent = current.parent
        if parent == current:
            # Reached filesystem root
            break
        current = parent

    return (None, "not_found")
