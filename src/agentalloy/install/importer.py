"""Fresh-build importer: pack YAML -> agentalloy.duck (+ fragments.lance reembed).

The v5 deterministic corpus builder. Reads ``_packs/<pack>/pack.yaml`` and the
referenced skill YAMLs and writes the relational skill graph (skills /
skill_versions / fragments / skill_dependencies) into ``agentalloy.duck``,
mirroring the graph structure the old Cypher ``ingest._insert`` produced:

- ``version_id = "{skill_id}-v1"``; the version is ``status='active'`` and the
  skill's ``current_version_id`` points at it (folds HAS_VERSION/CURRENT_VERSION).
- domain skills decompose into their authored ``fragments`` list; system skills
  get a single ``guardrail`` fragment carrying the whole ``raw_prose``; workflow
  skills carry no fragments.
- ``requires`` -> ``skill_dependencies`` (rel_type='requires'); cross-pack
  forward references are resolved after all skills are inserted.

``reembed_corpus`` then reads the active fragments back and builds the Lance
``fragments`` dataset (the SQL-canonical -> derived-index step, decision D7),
embedding each fragment's content via the configured embed client.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from agentalloy.reads.active import get_active_fragments
from agentalloy.storage.fragment_store import LanceFragmentStore
from agentalloy.storage.protocols import FragmentEmbedding
from agentalloy.storage.skill_store import DuckDBSkillStore

logger = logging.getLogger(__name__)

_UTC = _dt.UTC


def _opt_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    lst = [str(x) for x in v]
    return lst or None


def import_skill(ss: DuckDBSkillStore, data: dict[str, Any], *, tier: str | None) -> list[str]:
    """Insert one parsed skill YAML. Returns its ``requires`` targets (edges).

    Idempotent: deletes any existing skill of the same id first.
    """
    skill_id = str(data["skill_id"])
    version_id = f"{skill_id}-v1"
    skill_class = str(data.get("skill_class", "domain"))
    now = _dt.datetime.now(tz=_UTC)

    ss.delete_skill(skill_id)  # idempotent re-import

    ss.execute(
        "INSERT INTO skills (skill_id, canonical_name, category, skill_class, domain_tags, "
        "deprecated, superseded_by, always_apply, phase_scope, category_scope, tier, "
        "description, current_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            skill_id,
            str(data.get("canonical_name", skill_id)),
            str(data.get("category", "")),
            skill_class,
            _opt_list(data.get("domain_tags")) or [],
            bool(data.get("deprecated", False)),
            data.get("superseded_by") or None,
            bool(data.get("always_apply", False)),
            _opt_list(data.get("phase_scope")),
            _opt_list(data.get("category_scope")),
            tier,
            (str(data["description"]).strip() or None) if data.get("description") else None,
            version_id,
        ],
    )
    ss.execute(
        "INSERT INTO skill_versions (version_id, skill_id, version_number, authored_at, "
        "author, change_summary, status, raw_prose) VALUES (?,?,?,?,?,?,?,?)",
        [
            version_id,
            skill_id,
            1,
            now,
            str(data.get("author", "")),
            str(data.get("change_summary", "")),
            "active",
            str(data.get("raw_prose", "")),
        ],
    )

    if skill_class == "system":
        ss.execute(
            "INSERT INTO fragments (fragment_id, version_id, fragment_type, sequence, content) "
            "VALUES (?,?,?,?,?)",
            [f"{skill_id}-v1-f1", version_id, "guardrail", 1, str(data.get("raw_prose", ""))],
        )
    elif skill_class == "domain":
        for frag in data.get("fragments", []) or []:
            seq = int(frag["sequence"])
            ss.execute(
                "INSERT INTO fragments (fragment_id, version_id, fragment_type, sequence, content) "
                "VALUES (?,?,?,?,?)",
                [
                    f"{skill_id}-v1-f{seq}",
                    version_id,
                    str(frag.get("fragment_type", "execution")),
                    seq,
                    str(frag.get("content", "")),
                ],
            )
    # workflow: no fragments (raw_prose injected by the SDD phase hook)

    return [str(t) for t in dict.fromkeys(data.get("requires", []) or [])]


def import_pack(ss: DuckDBSkillStore, pack_dir: Path) -> dict[str, Any]:
    """Import every skill declared in ``pack_dir/pack.yaml``.

    Returns stats including the (source, target) requires edges to resolve.
    """
    pack = yaml.safe_load((pack_dir / "pack.yaml").read_text())
    tier = pack.get("tier")
    edges: list[tuple[str, str]] = []
    n = 0
    for entry in pack.get("skills", []):
        skill_file = pack_dir / entry["file"]
        data = yaml.safe_load(skill_file.read_text())
        for target in import_skill(ss, data, tier=tier):
            edges.append((str(data["skill_id"]), target))
        n += 1
    return {"pack": pack.get("name"), "skills": n, "edges": edges}


def resolve_edges(ss: DuckDBSkillStore, edges: Sequence[tuple[str, str]]) -> int:
    """Insert requires edges whose target skill exists. Returns edges written."""
    existing = {str(r[0]) for r in ss.execute("SELECT skill_id FROM skills")}
    written = 0
    for source, target in edges:
        if target not in existing:
            logger.warning("requires edge %s -> %s: target missing, skipped", source, target)
            continue
        ss.execute(
            "INSERT INTO skill_dependencies (source_skill_id, target_skill_id, rel_type) "
            "VALUES (?,?, 'requires') ON CONFLICT DO NOTHING",
            [source, target],
        )
        written += 1
    return written


def import_packs(ss: DuckDBSkillStore, pack_dirs: Sequence[Path]) -> dict[str, Any]:
    """Import multiple packs, then resolve all requires edges (cross-pack safe)."""
    all_edges: list[tuple[str, str]] = []
    total = 0
    for pd in pack_dirs:
        stats = import_pack(ss, pd)
        all_edges.extend(stats["edges"])
        total += stats["skills"]
    written = resolve_edges(ss, all_edges)
    return {"skills": total, "edges_written": written, "packs": len(pack_dirs)}


def reembed_corpus(
    fs: LanceFragmentStore,
    ss: DuckDBSkillStore,
    *,
    embed: Callable[[list[str]], list[list[float]]],
    model: str,
    batch_size: int = 32,
) -> int:
    """Build the Lance fragments dataset from the active fragments in DuckDB.

    ``embed`` is a callable ``(texts: list[str]) -> list[list[float]]`` (e.g. an
    embed client bound to its model). Writes are atomic (one ``bulk_replace``),
    then indices are built. Returns the number of fragments embedded.
    """
    frags = get_active_fragments(ss)
    if not frags:
        fs.bulk_replace([])
        return 0
    now = int(time.time())
    items: list[FragmentEmbedding] = []
    for i in range(0, len(frags), batch_size):
        chunk = frags[i : i + batch_size]
        vecs = embed([f.content for f in chunk])
        for f, vec in zip(chunk, vecs, strict=True):
            items.append(
                FragmentEmbedding(
                    fragment_id=f.fragment_id,
                    embedding=vec,
                    skill_id=f.skill_id,
                    category=f.category,
                    fragment_type=f.fragment_type,
                    embedded_at=now,
                    embedding_model=model,
                    prose=f.content,
                    phase_scope=f.phase_scope,
                )
            )
    fs.bulk_replace(items)
    fs.optimize()
    ss.set_meta("schema_version", "1")
    ss.set_meta("card_index", "off")
    logger.info("reembed_corpus: %d fragments -> fragments.lance", len(items))
    return len(items)
