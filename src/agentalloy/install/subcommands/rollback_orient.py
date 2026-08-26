"""``rollback-orient`` subcommand — restore DuckDB backups after OrientDB migration.

Restores .duck.bak files to their original .duck locations, effectively
reverting an OrientDB migration.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from agentalloy.config import get_settings


def rollback_orient(*, assume_yes: bool = False) -> dict[str, Any]:
    """Restore DuckDB backups to revert an OrientDB migration.

    Returns a summary dict with actions taken.
    """
    summary: dict[str, Any] = {
        "backups_found": 0,
        "backups_restored": 0,
        "actions": [],
        "warnings": [],
    }

    settings = get_settings()
    data_dir = Path(settings.code_index_data_dir)
    if not data_dir.exists():
        summary["warnings"].append(f"Data directory not found: {data_dir}")
        return summary

    # Find all .duck.bak files
    backups = list(data_dir.rglob("*.duck.bak"))
    summary["backups_found"] = len(backups)

    if not backups:
        summary["actions"].append("No DuckDB backups found — nothing to rollback")
        return summary

    if not assume_yes:
        print(f"\nFound {len(backups)} DuckDB backup(s):")
        for bak in backups[:5]:
            print(f"  - {bak}")
        if len(backups) > 5:
            print(f"  ... and {len(backups) - 5} more")

        try:
            response = input("\nRestore backups and revert OrientDB migration? [y/N]: ").strip().lower()
            if response not in ("y", "yes"):
                summary["actions"].append("Rollback declined by user")
                return summary
        except (EOFError, KeyboardInterrupt):
            summary["actions"].append("Rollback cancelled")
            return summary

    # Restore each backup
    for bak_path in backups:
        # Remove .bak suffix to get original path
        original_path = bak_path.with_suffix("")  # Removes .bak
        # But we need to remove the second .duck too, so:
        original_path = Path(str(bak_path).replace(".duck.bak", ".duck"))

        try:
            # If original exists, remove it first
            if original_path.exists():
                original_path.unlink()

            # Copy backup to original location
            shutil.copy2(bak_path, original_path)
            summary["backups_restored"] += 1
            summary["actions"].append(f"Restored {original_path.name}")

            # Optionally remove the backup file
            bak_path.unlink()
            summary["actions"].append(f"Removed backup {bak_path.name}")

        except Exception as e:
            summary["warnings"].append(f"Failed to restore {bak_path}: {e}")

    summary["actions"].append(
        f"\nRollback complete. Set CODE_INDEX_GRAPH_BACKEND=duckdb to use DuckDB."
    )

    return summary


def _run(args: argparse.Namespace) -> int:
    """CLI entry point for rollback-orient."""
    result = rollback_orient(assume_yes=args.yes)

    if args.json:
        import json
        print(json.dumps(result, indent=2))
    else:
        for action in result["actions"]:
            print(f"  {action}")
        for warning in result["warnings"]:
            print(f"  WARNING: {warning}", file=sys.stderr)

    return 0 if not result["warnings"] else 1


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subparsers.add_parser(
        "rollback-orient",
        help="Restore DuckDB backups to revert an OrientDB migration.",
        description=(
            "Restore .duck.bak backup files to their original .duck locations. "
            "Use this to revert an OrientDB migration and return to DuckDB."
        ),
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Non-interactive: skip confirmation and auto-restore.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON.",
    )
    p.set_defaults(func=_run)
