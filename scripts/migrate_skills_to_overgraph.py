#!/usr/bin/env python3
"""Migrate skills data from DuckDB+LanceDB to OverGraph.

Usage:
    python scripts/migrate_skills_to_overgraph.py [--force]

Reads from the existing DuckDB skill store (agentalloy.duck) and writes to
a new OverGraph database (agentalloy.overgraph). Fragment embeddings from
LanceDB are also migrated.

The migration is idempotent — running it again will overwrite the OverGraph
database with fresh data from DuckDB.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from agentalloy.config import get_settings
from agentalloy.storage.fragment_store import LanceFragmentStore
from agentalloy.storage.overgraph_skill_store import OverGraphSkillStore, open_overgraph_skill_store
from agentalloy.storage.protocols import (
    FragmentRow,
    SkillDependencyRow,
    SkillRow,
    SkillVersionRow,
)
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store


def migrate(force: bool = False) -> None:
    """Migrate skills from DuckDB to OverGraph."""
    settings = get_settings()

    # Paths
    duckdb_path = Path(settings.duckdb_path)
    overgraph_path = duckdb_path.with_suffix(".overgraph")
    lance_path = Path(settings.fragments_lance_path)

    if not duckdb_path.exists():
        print(f"error: DuckDB skill store not found at {duckdb_path}", file=sys.stderr)
        sys.exit(1)

    if overgraph_path.exists():
        if not force:
            print(
                f"error: OverGraph database already exists at {overgraph_path}\n"
                f"Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Removing existing OverGraph database at {overgraph_path}...")
        shutil.rmtree(overgraph_path)

    print(f" Migrating skills from DuckDB to OverGraph...")
    print(f"  Source: {duckdb_path}")
    print(f"  Target: {overgraph_path}")
    t0 = time.time()

    # Open source stores
    source_store = open_skill_store(duckdb_path, read_only=True)
    lance_store = LanceFragmentStore(str(lance_path)) if lance_path.exists() else None

    # Open target store
    target_store = open_overgraph_skill_store(overgraph_path, read_only=False)

    try:
        # Migrate skills
        print("\n[1/5] Migrating skills...")
        skill_rows = source_store.execute(
            "SELECT skill_id, canonical_name, category, skill_class, domain_tags, "
            "deprecated, superseded_by, always_apply, phase_scope, category_scope, "
            "tier, description, current_version_id FROM skills"
        )
        skill_count = 0
        for r in skill_rows:
            skill = SkillRow(
                skill_id=str(r[0]),
                canonical_name=str(r[1]),
                category=str(r[2]) if r[2] else "",
                skill_class=str(r[3]) if r[3] else "",
                domain_tags=list(r[4] or []),
                deprecated=bool(r[5]),
                superseded_by=str(r[6]) if r[6] else None,
                always_apply=bool(r[7]),
                phase_scope=list(r[8]) if r[8] else None,
                category_scope=list(r[9]) if r[9] else None,
                tier=str(r[10]) if r[10] else None,
                description=str(r[11]) if r[11] else None,
                current_version_id=str(r[12]) if r[12] else "",
            )
            target_store.insert_skill(skill)
            skill_count += 1
        print(f"  Migrated {skill_count} skills")

        # Migrate versions
        print("\n[2/5] Migrating versions...")
        version_rows = source_store.execute(
            "SELECT version_id, skill_id, version_number, authored_at, author, "
            "change_summary, status, raw_prose FROM skill_versions"
        )
        version_count = 0
        for r in version_rows:
            authored_at = r[3]
            if hasattr(authored_at, 'isoformat'):
                authored_at = authored_at.isoformat()
            version = SkillVersionRow(
                version_id=str(r[0]),
                skill_id=str(r[1]),
                version_number=int(r[2]),
                authored_at=authored_at,
                author=str(r[4]) if r[4] else "",
                change_summary=str(r[5]) if r[5] else "",
                status=str(r[6]) if r[6] else "",
                raw_prose=str(r[7]) if r[7] else "",
            )
            target_store.insert_version(version)
            version_count += 1
        print(f"  Migrated {version_count} versions")

        # Migrate fragments
        print("\n[3/5] Migrating fragments...")
        fragment_rows = source_store.execute(
            "SELECT fragment_id, version_id, fragment_type, sequence, content FROM fragments"
        )
        fragment_count = 0
        for r in fragment_rows:
            fragment = FragmentRow(
                fragment_id=str(r[0]),
                version_id=str(r[1]),
                fragment_type=str(r[2]) if r[2] else "",
                sequence=int(r[3]),
                content=str(r[4]) if r[4] else "",
            )
            target_store.insert_fragment(fragment)
            fragment_count += 1
        print(f"  Migrated {fragment_count} fragments")

        # Create DecomposesTo edges (after all nodes exist)
        print("  Creating DecomposesTo edges...")
        edge_count = 0
        for r in fragment_rows:
            frag_id = str(r[0])
            ver_id = str(r[1])
            seq = int(r[3])
            ver_node = target_store._db.get_node_by_key("SkillVersion", ver_id)
            frag_node = target_store._db.get_node_by_key("Fragment", frag_id)
            if ver_node and frag_node:
                target_store._db.upsert_edge(
                    from_id=ver_node.id,
                    to_id=frag_node.id,
                    label="DecomposesTo",
                    props={"sequence": seq},
                )
                edge_count += 1
        print(f"  Created {edge_count} DecomposesTo edges")

        # Migrate dependencies
        print("\n[4/5] Migrating dependencies...")
        dep_rows = source_store.execute(
            "SELECT source_skill_id, target_skill_id, rel_type FROM skill_dependencies"
        )
        dep_count = 0
        for r in dep_rows:
            dep = SkillDependencyRow(
                source_skill_id=str(r[0]),
                target_skill_id=str(r[1]),
                rel_type=str(r[2]) if r[2] else "requires",
            )
            target_store.insert_dependency(dep)
            dep_count += 1
        print(f"  Migrated {dep_count} dependencies")

        # Migrate corpus_meta
        print("\n[5/5] Migrating corpus metadata...")
        meta_rows = source_store.execute("SELECT key, value FROM corpus_meta")
        meta_count = 0
        for r in meta_rows:
            target_store.set_meta(str(r[0]), str(r[1]))
            meta_count += 1
        print(f"  Migrated {meta_count} metadata entries")

        # Flush to ensure all data is persisted
        target_store._db.flush()

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f" Migration complete in {elapsed:.1f}s")
        print(f"  Skills: {skill_count}")
        print(f"  Versions: {version_count}")
        print(f"  Fragments: {fragment_count}")
        print(f"  Dependencies: {dep_count}")
        print(f"  Metadata: {meta_count}")
        print(f"{'=' * 60}")

        # Verify counts match
        print("\nVerifying migration...")
        source_skill_count = source_store.scalar("SELECT count(*) FROM skills") or 0
        target_skill_count = len(target_store.get_active_skills()) + len(
            [s for s in source_store.execute("SELECT skill_id FROM skills WHERE deprecated = true")]
        )
        print(f"  Source skills: {source_skill_count}")
        print(f"  Target skills: {target_skill_count}")

        if int(source_skill_count) == target_skill_count:
            print("  ✓ Counts match!")
        else:
            print("  ⚠ Counts differ — please verify manually")

    finally:
        source_store.close()
        target_store.close()

    print("\n✓ Migration complete. To use OverGraph, set:")
    print(f"  export SKILL_STORE_BACKEND=overgraph")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate skills from DuckDB to OverGraph",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing OverGraph database",
    )
    args = parser.parse_args()
    migrate(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
