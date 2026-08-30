# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Fresh-build importer: pack YAML -> the unified corpus store (+ reembed).

The v5 deterministic corpus builder. Reads ``_packs/<pack>/pack.yaml`` and the
referenced skill YAMLs and writes the relational skill graph (skills /
skill_versions / fragments / skill_dependencies) into the OverGraph corpus
store, mirroring the graph structure the old Cypher ``ingest._insert`` produced:

- ``version_id = "{skill_id}-v1"``; the version is ``status='active'`` and the
  skill's ``current_version_id`` points at it (folds HAS_VERSION/CURRENT_VERSION).
- domain skills decompose into their authored ``fragments`` list; system skills
  get a single ``guardrail`` fragment carrying the whole ``raw_prose``; workflow
  skills carry no fragments.
- ``requires`` -> ``skill_dependencies`` (rel_type='requires'); cross-pack
  forward references are resolved after all skills are inserted.

``reembed_corpus`` then reads the active fragments back and builds the
fragment embedding index (the canonical -> derived-index step, decision D7),
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
from agentalloy.storage.protocols import (
    FragmentEmbedding,
    FragmentRow,
    FragmentStore,
    SkillDependencyRow,
    SkillRow,
    SkillStore,
    SkillVersionRow,
)

logger = logging.getLogger(__name__)

_UTC = _dt.UTC


def _opt_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    lst = [str(x) for x in v]
    return lst or None


def import_skill(ss: SkillStore, data: dict[str, Any], *, tier: str | None) -> list[str]:
    """Insert one parsed skill YAML. Returns its ``requires`` targets (edges).

    Idempotent: deletes any existing skill of the same id first.
    """
    skill_id = str(data["skill_id"])
    version_id = f"{skill_id}-v1"
    skill_class = str(data.get("skill_class", "domain"))
    now = _dt.datetime.now(tz=_UTC)

    ss.delete_skill(skill_id)  # idempotent re-import

    ss.insert_skill(
        SkillRow(
            skill_id=skill_id,
            canonical_name=str(data.get("canonical_name", skill_id)),
            category=str(data.get("category", "")),
            skill_class=skill_class,
            domain_tags=_opt_list(data.get("domain_tags")) or [],
            deprecated=bool(data.get("deprecated", False)),
            superseded_by=data.get("superseded_by") or None,
            always_apply=bool(data.get("always_apply", False)),
            phase_scope=_opt_list(data.get("phase_scope")),
            category_scope=_opt_list(data.get("category_scope")),
            tier=tier,
            description=(str(data["description"]).strip() or None)
            if data.get("description")
            else None,
            current_version_id=version_id,
        )
    )
    ss.insert_version(
        SkillVersionRow(
            version_id=version_id,
            skill_id=skill_id,
            version_number=1,
            authored_at=now,
            author=str(data.get("author", "")),
            change_summary=str(data.get("change_summary", "")),
            status="active",
            raw_prose=str(data.get("raw_prose", "")),
        )
    )

    if skill_class == "system":
        ss.insert_fragment(
            FragmentRow(
                fragment_id=f"{skill_id}-v1-f1",
                version_id=version_id,
                fragment_type="guardrail",
                sequence=1,
                content=str(data.get("raw_prose", "")),
            )
        )
    elif skill_class == "domain":
        for frag in data.get("fragments", []) or []:
            seq = int(frag["sequence"])
            ss.insert_fragment(
                FragmentRow(
                    fragment_id=f"{skill_id}-v1-f{seq}",
                    version_id=version_id,
                    fragment_type=str(frag.get("fragment_type", "execution")),
                    sequence=seq,
                    content=str(frag.get("content", "")),
                )
            )
    # workflow: no fragments (raw_prose injected by the SDD phase hook)

    return [str(t) for t in dict.fromkeys(data.get("requires", []) or [])]


def import_pack(ss: SkillStore, pack_dir: Path) -> dict[str, Any]:
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


def resolve_edges(ss: SkillStore, edges: Sequence[tuple[str, str]]) -> int:
    """Insert requires edges whose target skill exists. Returns edges written."""
    written = 0
    for source, target in edges:
        if ss.get_skill(target) is None:
            logger.warning("requires edge %s -> %s: target missing, skipped", source, target)
            continue
        ss.insert_dependency(
            SkillDependencyRow(
                source_skill_id=source,
                target_skill_id=target,
                rel_type="requires",
            )
        )
        written += 1
    return written


def import_packs(ss: SkillStore, pack_dirs: Sequence[Path]) -> dict[str, Any]:
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
    fs: FragmentStore,
    ss: SkillStore,
    *,
    embed: Callable[[list[str]], list[list[float]]],
    model: str,
    batch_size: int = 32,
) -> int:
    """Build fragment embeddings from the active fragments in the corpus store.

    ``fs`` and ``ss`` may be the same unified store. ``embed`` is a callable
    ``(texts: list[str]) -> list[list[float]]`` (e.g. an embed client bound to
    its model). Writes are atomic (one ``bulk_replace``), then the BM25 index
    is rebuilt. Returns the number of fragments embedded.
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
                    domain_tags=tuple(f.domain_tags) if f.domain_tags else None,
                ),
            )
    fs.bulk_replace(items)
    ss.set_meta("schema_version", "1")
    ss.set_meta("card_index", "off")
    logger.info("reembed_corpus: %d fragments embedded", len(items))
    return len(items)
