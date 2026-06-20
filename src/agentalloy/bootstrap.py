"""Bootstrap CLI for loading atomic system skills into LadybugDB.

Usage::

    python -m agentalloy.bootstrap <path.md> [--force] [--init-schema] [--yes]

Exit codes
----------
0  success
1  usage error (bad args, file not found)
2  validation error (bad skill data or duplicate skill_id)
3  DB error
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from agentalloy.config import get_settings
from agentalloy.skill_md.parser import ParsedSystemSkill, ParseError, parse_file
from agentalloy.storage.ladybug import LadybugStore

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VALIDATION = 2
EXIT_DB = 3

# Canonical SDD lifecycle (matches ingest._VALID_PHASES / gates._PHASE_GRAPH).
# Reconciled from the old {design, build, review} in Stage 3b; "sdd-fast" (the
# fast-lane phase) added so sys skills can scope to it.
_VALID_PHASES = {"intake", "spec", "design", "build", "qa", "ship", "sdd-fast"}
_VALID_CATEGORIES = {"governance", "operational", "tooling", "safety", "quality", "observability"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.bootstrap",
        description="Load an atomic system skill from a Markdown file into LadybugDB.",
    )
    parser.add_argument("path", help="Path to the system skill Markdown file")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite if skill_id already exists",
    )
    parser.add_argument(
        "--init-schema",
        action="store_true",
        dest="init_schema",
        help="Run schema migration before inserting (safe to use on existing DB)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args(argv)

    md_path = Path(args.path)
    if not md_path.exists():
        print(f"error: file not found: {md_path}", file=sys.stderr)
        return EXIT_USAGE

    # --- parse ---
    try:
        skill = parse_file(md_path)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION

    # --- validate ---
    errors = _validate(skill)
    if errors:
        for e in errors:
            print(f"validation error: {e}", file=sys.stderr)
        return EXIT_VALIDATION

    # --- open DB and check duplicate ---
    settings = get_settings()
    store = LadybugStore(settings.ladybug_db_path)
    try:
        store.open()
    except Exception as exc:
        print(
            f"error: failed to open LadybugDB at '{settings.ladybug_db_path}': {exc}",
            file=sys.stderr,
        )
        return EXIT_DB

    try:
        if args.init_schema:
            try:
                store.migrate()
            except Exception as exc:
                print(f"error: schema migration failed: {exc}", file=sys.stderr)
                return EXIT_DB

        existing_name = store.scalar(
            "MATCH (s:Skill {skill_id: $id}) RETURN s.canonical_name",
            {"id": skill.skill_id},
        )
        if existing_name is not None and not args.force:
            print(
                f"error: skill_id '{skill.skill_id}' already exists "
                f"(canonical_name: '{existing_name}'). Use --force to overwrite.",
                file=sys.stderr,
            )
            return EXIT_VALIDATION

        if existing_name is None:
            existing_id_by_name = store.scalar(
                "MATCH (s:Skill {canonical_name: $name}) RETURN s.skill_id",
                {"name": skill.canonical_name},
            )
            if existing_id_by_name is not None and not args.force:
                print(
                    f"error: canonical_name '{skill.canonical_name}' is already used by "
                    f"skill_id '{existing_id_by_name}'. Use --force to overwrite.",
                    file=sys.stderr,
                )
                return EXIT_VALIDATION

        # --- confirmation gate ---
        _print_summary(skill, existing=existing_name is not None)
        if not args.yes:
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                return EXIT_USAGE

        # --- check superseded_by reference (needs DB access) ---
        if skill.superseded_by:
            ref_exists = store.scalar(
                "MATCH (s:Skill {skill_id: $id}) RETURN s.skill_id",
                {"id": skill.superseded_by},
            )
            if ref_exists is None:
                print(
                    f"validation error: superseded_by '{skill.superseded_by}' "
                    f"references a non-existent skill_id",
                    file=sys.stderr,
                )
                return EXIT_VALIDATION

        # --- insert ---
        try:
            _insert(store, skill, force=args.force)
        except Exception as exc:
            print(f"error: DB insert failed: {exc}", file=sys.stderr)
            return EXIT_DB

    finally:
        store.close()

    print(f"ok: loaded '{skill.skill_id}' ({skill.canonical_name})")
    return EXIT_OK


def _validate(skill: ParsedSystemSkill) -> list[str]:
    errors: list[str] = []

    if not skill.skill_id.startswith("sys-"):
        errors.append(f"skill_id '{skill.skill_id}' must start with 'sys-'")

    if not skill.skill_id.replace("-", "").replace("_", "").isalnum():
        errors.append(f"skill_id '{skill.skill_id}' contains invalid characters")

    if not skill.category.strip():
        errors.append("category is required")
    elif skill.category not in _VALID_CATEGORIES:
        errors.append(
            f"category '{skill.category}' is not valid (must be one of {sorted(_VALID_CATEGORIES)})"
        )

    if not skill.canonical_name.strip():
        errors.append("canonical_name (H1 heading) is required")

    if not skill.raw_prose.strip():
        errors.append("raw_prose body is empty — the skill has no content")

    for phase in skill.phase_scope:
        if phase not in _VALID_PHASES:
            errors.append(
                f"phase_scope '{phase}' is not valid (must be one of {sorted(_VALID_PHASES)})"
            )

    if skill.always_apply and (skill.phase_scope or skill.category_scope):
        errors.append("always_apply=true is mutually exclusive with phase_scope / category_scope")

    # --- deprecation validation ---
    if skill.deprecated and not skill.superseded_by:
        errors.append(
            "deprecated: true requires 'superseded_by' to be set — "
            "a skill cannot be deprecated without a replacement"
        )

    if skill.superseded_by and not re.match(r"^[a-z0-9-]+$", skill.superseded_by):
        errors.append(f"superseded_by '{skill.superseded_by}' must be kebab-case, lowercase ASCII")

    return errors


def _print_summary(skill: ParsedSystemSkill, *, existing: bool) -> None:
    action = "OVERWRITE" if existing else "INSERT"
    print(f"\n{'=' * 60}")
    print(f"  Action:         {action}")
    print(f"  skill_id:       {skill.skill_id}")
    print(f"  canonical_name: {skill.canonical_name}")
    print(f"  category:       {skill.category}")
    print(f"  deprecated:     {skill.deprecated}")
    print(f"  superseded_by:  {skill.superseded_by or '(none)'}")
    print(f"  always_apply:   {skill.always_apply}")
    print(f"  phase_scope:    {skill.phase_scope or '(none)'}")
    print(f"  category_scope: {skill.category_scope or '(none)'}")
    print(f"  author:         {skill.author}")
    print(f"  prose length:   {len(skill.raw_prose)} chars")
    print(f"{'=' * 60}\n")


def _insert(store: LadybugStore, skill: ParsedSystemSkill, *, force: bool) -> None:
    version_id = f"{skill.skill_id}-v1"
    fragment_id = f"{skill.skill_id}-v1-f1"
    now = datetime.now(tz=UTC)

    if force:
        # Remove existing skill and all connected nodes
        store.execute(
            """
            MATCH (s:Skill {skill_id: $id})
            OPTIONAL MATCH (s)-[:HAS_VERSION]->(v:SkillVersion)
            OPTIONAL MATCH (v)-[:DECOMPOSES_TO]->(f:Fragment)
            DETACH DELETE s, v, f
            """,
            {"id": skill.skill_id},
        )

    store.execute(
        """
        CREATE (:Skill {
            skill_id: $skill_id,
            canonical_name: $canonical_name,
            category: $category,
            skill_class: 'system',
            domain_tags: [],
            deprecated: $deprecated,
            superseded_by: $superseded_by,
            always_apply: $always_apply,
            phase_scope: $phase_scope,
            category_scope: $category_scope
        })
        """,
        {
            "skill_id": skill.skill_id,
            "canonical_name": skill.canonical_name,
            "category": skill.category,
            "deprecated": skill.deprecated,
            "superseded_by": skill.superseded_by or "",
            "always_apply": skill.always_apply,
            "phase_scope": skill.phase_scope,
            "category_scope": skill.category_scope,
        },
    )

    store.execute(
        """
        CREATE (:SkillVersion {
            version_id: $version_id,
            version_number: 1,
            authored_at: $authored_at,
            author: $author,
            change_summary: $change_summary,
            status: 'active',
            raw_prose: $raw_prose
        })
        """,
        {
            "version_id": version_id,
            "authored_at": now,
            "author": skill.author,
            "change_summary": skill.change_summary,
            "raw_prose": skill.raw_prose,
        },
    )

    store.execute(
        """
        MATCH (s:Skill {skill_id: $skill_id}), (v:SkillVersion {version_id: $version_id})
        CREATE (s)-[:HAS_VERSION]->(v)
        CREATE (s)-[:CURRENT_VERSION]->(v)
        """,
        {"skill_id": skill.skill_id, "version_id": version_id},
    )

    store.execute(
        """
        CREATE (:Fragment {
            fragment_id: $fragment_id,
            fragment_type: 'guardrail',
            sequence: 1,
            content: $content
        })
        """,
        {
            "fragment_id": fragment_id,
            "content": skill.raw_prose,
        },
    )

    store.execute(
        """
        MATCH (v:SkillVersion {version_id: $version_id}), (f:Fragment {fragment_id: $fragment_id})
        CREATE (v)-[:DECOMPOSES_TO]->(f)
        """,
        {"version_id": version_id, "fragment_id": fragment_id},
    )


if __name__ == "__main__":
    sys.exit(main())
