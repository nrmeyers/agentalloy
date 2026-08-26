"""OrientDB migration integration for ``agentalloy upgrade``.

Detects existing DuckDB graph files and offers to migrate them to OrientDB
as a post-upgrade step. This is opt-in — users can decline or run the
migration manually later.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentalloy.config import get_settings
from agentalloy.install import state as install_state


def detect_duckdb_graphs() -> list[Path]:
    """Find all existing DuckDB graph files in the code index data directory.

    Returns a list of paths to graph.duck files.
    """
    settings = get_settings()
    data_dir = Path(settings.code_index_data_dir)
    if not data_dir.exists():
        return []

    # Find all graph.duck files under repos/
    graphs = list(data_dir.rglob("repos/*/graph.duck"))
    # Also check the legacy layout (repos/*/default/graph.duck)
    graphs.extend(data_dir.rglob("repos/*/*/graph.duck"))

    # Deduplicate and sort
    return sorted(set(graphs))


def prompt_migration(graphs: list[Path], interactive: bool, assume_yes: bool) -> bool:
    """Prompt the user to migrate DuckDB graphs to OrientDB.

    Returns True if migration should proceed, False otherwise.
    """
    if not graphs:
        return False

    if assume_yes:
        return True

    if not interactive:
        # Non-interactive mode: skip migration unless --yes is passed
        return False

    print(f"\n{'=' * 60}")
    print("OrientDB Migration Available")
    print('=' * 60)
    print(f"\nFound {len(graphs)} DuckDB graph(s):")
    for g in graphs[:5]:  # Show first 5
        print(f"  - {g}")
    if len(graphs) > 5:
        print(f"  ... and {len(graphs) - 5} more")

    print("\nMigrate to OrientDB for native graph traversal (10+ hops)?")
    print("  - DuckDB files will be backed up (.duck.bak)")
    print("  - Set CODE_INDEX_GRAPH_BACKEND=orientdb to use OrientDB")
    print("  - Run `agentalloy upgrade --rollback-orient` to revert")

    try:
        response = input("\nProceed with migration? [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\nMigration skipped.")
        return False


def run_migration(graphs: list[Path]) -> tuple[list[str], list[str]]:
    """Run the DuckDB → OrientDB migration for all detected graphs.

    Returns (actions, warnings) lists for the upgrade summary.
    """
    actions: list[str] = []
    warnings: list[str] = []

    if not graphs:
        return actions, warnings

    # Check if OrientDB is running
    settings = get_settings()
    orient_url = settings.orientdb_url

    try:
        import httpx
        response = httpx.get(f"{orient_url}/server", timeout=5.0, auth=(settings.orientdb_username, settings.orientdb_password))
        if response.status_code != 200:
            # Try root credentials
            response = httpx.get(f"{orient_url}/server", timeout=5.0, auth=("root", "admin123"))
            if response.status_code != 200:
                warnings.append(
                    f"OrientDB not reachable at {orient_url}. "
                    "Start OrientDB first, then run migration manually."
                )
                return actions, warnings
    except Exception as e:
        warnings.append(
            f"OrientDB not reachable at {orient_url}: {e}. "
            "Start OrientDB first, then run migration manually."
        )
        return actions, warnings

    # Run migration for each graph
    migration_script = Path(__file__).parent.parent.parent / "scripts" / "orientdb_migrate_from_duckdb.py"
    if not migration_script.exists():
        # Try alternate location (development)
        migration_script = Path(__file__).parent.parent.parent.parent / "scripts" / "orientdb_migrate_from_duckdb.py"

    if not migration_script.exists():
        warnings.append("Migration script not found. Run migration manually.")
        return actions, warnings

    for graph_path in graphs:
        slug = _extract_slug(graph_path)
        if not slug:
            warnings.append(f"Could not extract slug from {graph_path}")
            continue

        # Create OrientDB database for this slug (if it doesn't exist)
        db_name = slug.replace("-", "_").replace(".", "_")
        try:
            # Try to create the database (idempotent - fails if already exists)
            import httpx
            create_response = httpx.post(
                f"{orient_url}/database/{db_name}/plocal",
                timeout=10.0,
                auth=("root", "admin123"),
            )
            if create_response.status_code == 200:
                actions.append(f"Created OrientDB database: {db_name}")
                # Create admin user for the database
                httpx.post(
                    f"{orient_url}/command/{db_name}/sql",
                    json={"command": 'INSERT INTO OUser SET name = "admin", password = "admin", status = "ACTIVE"'},
                    timeout=10.0,
                    auth=("root", "admin123"),
                )
                httpx.post(
                    f"{orient_url}/command/{db_name}/sql",
                    json={"command": 'UPDATE OUser SET roles = (SELECT FROM ORole WHERE name = "admin") WHERE name = "admin"'},
                    timeout=10.0,
                    auth=("root", "admin123"),
                )
        except Exception as e:
            # Database might already exist, which is fine
            pass

        # Backup the DuckDB file
        backup_path = graph_path.with_suffix(".duck.bak")
        try:
            shutil.copy2(graph_path, backup_path)
            actions.append(f"Backed up {graph_path.name} → {backup_path.name}")
        except Exception as e:
            warnings.append(f"Failed to backup {graph_path}: {e}")
            continue

        # Run migration script with modified DUCKDB_PATH
        env = {**dict(__import__("os").environ), "MIGRATE_DUCKDB_PATH": str(graph_path)}
        try:
            result = subprocess.run(
                [sys.executable, str(migration_script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout per graph
            )
            if result.returncode == 0:
                actions.append(f"Migrated {slug} to OrientDB")
            else:
                warnings.append(f"Migration failed for {slug}: {result.stderr[:200]}")
                # Restore from backup
                shutil.copy2(backup_path, graph_path)
                actions.append(f"Restored {graph_path.name} from backup")
        except subprocess.TimeoutExpired:
            warnings.append(f"Migration timeout for {slug}")
            shutil.copy2(backup_path, graph_path)
            actions.append(f"Restored {graph_path.name} from backup")
        except Exception as e:
            warnings.append(f"Migration error for {slug}: {e}")
            shutil.copy2(backup_path, graph_path)
            actions.append(f"Restored {graph_path.name} from backup")

    return actions, warnings


def _extract_slug(graph_path: Path) -> str | None:
    """Extract the slug from a graph.duck path.

    Layout: repos/{slug}/graph.duck or repos/{slug}/{path_key}/graph.duck
    """
    parts = graph_path.parts
    try:
        repos_idx = parts.index("repos")
        if repos_idx + 1 < len(parts):
            return parts[repos_idx + 1]
    except ValueError:
        pass
    return None


def post_upgrade_migration(interactive: bool, assume_yes: bool) -> tuple[list[str], list[str]]:
    """Post-upgrade hook: detect DuckDB graphs and offer migration.

    Returns (actions, warnings) for the upgrade summary.
    """
    graphs = detect_duckdb_graphs()
    if not graphs:
        return [], []

    should_migrate = prompt_migration(graphs, interactive, assume_yes)
    if not should_migrate:
        return (
            ["OrientDB migration skipped (run manually with scripts/orientdb_migrate_from_duckdb.py)"],
            [],
        )

    return run_migration(graphs)
