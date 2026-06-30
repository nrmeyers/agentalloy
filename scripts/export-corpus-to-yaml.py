#!/usr/bin/env python3
"""Export every skill from the live skill store to a YAML in seeds/_exported/.

Used to materialize the original ship-with-wheel skills (and any other
skills that exist in the DB but aren't in seeds/) into source-of-truth YAML
files, so the migrate-seeds-to-packs script can place them in the right pack.

Skips skills whose source YAML already exists anywhere under seeds/.

Usage:
  python scripts/export-corpus-to-yaml.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = REPO_ROOT / "seeds"
EXPORT_DIR = SEEDS / "_exported"


def existing_skill_ids() -> set[str]:
    """Skill IDs that already have a source YAML somewhere under seeds/."""
    out: set[str] = set()
    for path in SEEDS.rglob("*.yaml"):
        if path.name == "pack.yaml":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        sid = data.get("skill_id")
        if sid:
            out.add(str(sid))
    return out


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from contextlib import closing  # noqa: E402

    from agentalloy.config import get_settings  # noqa: E402
    from agentalloy.storage.open import open_skills  # noqa: E402

    settings = get_settings()
    have = existing_skill_ids()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0

    with closing(open_skills(settings, read_only=True)) as store:
        # active version per skill (current_version_id folds the old HAS_VERSION edge)
        rows = store.execute(
            """
            SELECT s.skill_id, s.canonical_name, s.category, s.skill_class,
                   s.domain_tags, s.always_apply, s.phase_scope, s.category_scope,
                   v.version_id, v.author, v.change_summary, v.raw_prose
            FROM skills s JOIN skill_versions v ON v.version_id = s.current_version_id
            """
        )
        for r in rows:
            sid = str(r[0])
            if sid in have:
                skipped += 1
                continue
            (
                _sid,
                name,
                cat,
                klass,
                tags,
                always,
                phase,
                catscope,
                vid,
                author,
                summary,
                raw_prose,
            ) = r

            # Fetch fragments (DECOMPOSES_TO folds into fragments.version_id)
            frag_rows = store.execute(
                """
                SELECT fragment_id, sequence, fragment_type, content
                FROM fragments WHERE version_id = ? ORDER BY sequence
                """,
                [vid],
            )
            fragments = []
            for fr in frag_rows:
                fragments.append(
                    {
                        "sequence": int(fr[1]),
                        "fragment_type": str(fr[2]),
                        "content": str(fr[3]),
                    }
                )

            doc = {
                "skill_id": sid,
                "canonical_name": str(name),
                "category": str(cat) if cat else "engineering",
                "skill_class": str(klass) if klass else "domain",
                "domain_tags": [str(t) for t in (tags or [])],
                "always_apply": bool(always) if always is not None else False,
                "phase_scope": [str(p) for p in (phase or [])] or None,
                "category_scope": [str(c) for c in (catscope or [])] or None,
                "author": str(author) if author else "ship-with-wheel",
                "change_summary": str(summary) if summary else "Exported from corpus",
                "raw_prose": str(raw_prose) if raw_prose else "",
                "fragments": fragments,
            }

            # Raw_prose-only skills (system guardrail, workflow) don't carry
            # fragments in the YAML schema — ingest derives them — so strip.
            if str(klass) in ("system", "workflow"):
                doc["fragments"] = []

            out = EXPORT_DIR / f"{sid}.yaml"
            out.write_text(
                yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=10000),
                encoding="utf-8",
            )
            written += 1

    print(f"exported: {written}")
    print(f"skipped (already in seeds/): {skipped}")
    print(f"output: {EXPORT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
