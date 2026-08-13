"""Meta-skill corpus delivery — proves the real sdd pack, imported through the real
production path, actually surfaces all 8 converted meta/conventions skills via the
real system-skill retrieval predicate.

This is the "delivery is proven, not asserted" test the spec requires
(docs/spec-contracts/meta-skill-corpus-delivery.spec.md AC 1). No fixture corpus,
no mocks: the real `_packs/sdd` directory, the real `install.importer.import_packs`,
the real `retrieval.system.retrieve_system_fragments`.
"""

from __future__ import annotations

from pathlib import Path

import agentalloy
from agentalloy.install.importer import import_packs
from agentalloy.retrieval.system import retrieve_system_fragments
from agentalloy.skill_md.parser import parse_file
from agentalloy.storage.skill_store import open_skill_store

_PACKS = Path(agentalloy.__file__).parent / "_packs"
_SDD_DIR = _PACKS / "sdd"

# skill_id -> the _packs subdir its source .md lives in (differs by skill).
_SOURCE_DIRS = {
    "sys-skill-authoring-rules": "meta",
    "sys-skill-review-verdict": "meta",
    "sys-r1-tiered-sourcing": "meta",
    "sys-skill-tagging-rules": "meta",
    "sys-fragment-types-and-sizing": "meta",
    "sys-skill-naming": "conventions",
    "sys-skill-output-formatting": "conventions",
    "sys-skill-writing-voice": "conventions",
}
_TARGET_IDS = set(_SOURCE_DIRS)


def _fresh_store(tmp_path: Path):
    ss = open_skill_store(str(tmp_path / "corpus.duck"))
    ss.migrate()
    import_packs(ss, [_SDD_DIR])
    return ss


def test_all_skills_delivered_on_add_skill_phase(tmp_path: Path) -> None:
    ss = _fresh_store(tmp_path)
    try:
        result = retrieve_system_fragments(ss, phase="add-skill", category=None)
        assert set(result.applied_skill_ids) >= _TARGET_IDS
        candidate_ids = {f.skill_id for f in result.candidates}
        assert candidate_ids >= _TARGET_IDS
    finally:
        ss.close()


def test_none_delivered_on_a_different_phase(tmp_path: Path) -> None:
    ss = _fresh_store(tmp_path)
    try:
        result = retrieve_system_fragments(ss, phase="build", category=None)
        assert not (_TARGET_IDS & set(result.applied_skill_ids))
    finally:
        ss.close()


def test_raw_prose_carried_over_verbatim(tmp_path: Path) -> None:
    ss = _fresh_store(tmp_path)
    try:
        result = retrieve_system_fragments(ss, phase="add-skill", category=None)
        delivered = {f.skill_id: f.content for f in result.candidates if f.skill_id in _TARGET_IDS}
        for skill_id, subdir in _SOURCE_DIRS.items():
            source = parse_file(_PACKS / subdir / f"{skill_id}.md")
            assert delivered[skill_id].strip() == source.raw_prose.strip(), (
                f"{skill_id}: delivered fragment content diverges from the source .md"
            )
    finally:
        ss.close()


def test_requires_edges_resolved(tmp_path: Path) -> None:
    ss = _fresh_store(tmp_path)
    try:
        rows = ss.execute(
            "SELECT target_skill_id FROM skill_dependencies "
            "WHERE source_skill_id = 'sdd-add-skill' AND rel_type = 'requires'"
        )
        targets = {str(r[0]) for r in rows}
        assert targets >= _TARGET_IDS
    finally:
        ss.close()


def test_pack_manifest_entries_are_consistent() -> None:
    import yaml

    manifest = yaml.safe_load((_SDD_DIR / "pack.yaml").read_text(encoding="utf-8"))
    entries = {e["skill_id"]: e for e in manifest["skills"]}
    for skill_id in _TARGET_IDS:
        assert skill_id in entries, f"{skill_id} missing from sdd/pack.yaml"
        entry = entries[skill_id]
        assert entry["fragment_count"] == 0
        assert (_SDD_DIR / entry["file"]).is_file()
