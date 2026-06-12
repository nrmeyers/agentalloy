"""Skill-graph edge declaration + ingest (requires / related).

Covers:
- ``requires`` → REQUIRES_COMPOSITIONAL, ``related`` → REFERENCES_CONCEPTUAL;
- absent fields → no edges (backward compatible);
- cross-pack forward refs: a target ingested later in a batch is wired on the
  retry pass; a still-missing target is a warning, not an error;
- re-ingest replaces a skill's outgoing edges (version-bump idempotency);
- validation rejects self-edges and non-kebab-case targets.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

from agentalloy.ingest import (
    EXIT_OK,
    EXIT_VALIDATION,
    ReviewRecord,
    _load_yaml,  # pyright: ignore[reportPrivateUsage]
    _validate,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.ingest import main as ingest_main
from agentalloy.storage.ladybug import LadybugStore


class _FakeSettings:
    def __init__(self, db_path: str) -> None:
        self.ladybug_db_path = db_path


def _skill_yaml(skill_id: str, *, requires: str = "", related: str = "") -> str:
    body = textwrap.dedent(f"""\
        skill_type: domain
        skill_id: {skill_id}
        canonical_name: Skill {skill_id}
        category: engineering
        skill_class: domain
        domain_tags: [testing]
        always_apply: false
        author: test
        change_summary: unit test
        raw_prose: |
          Run the build and confirm it compiles cleanly before moving on, then
          run the full test suite and read the first failing assertion closely.
        fragments:
          - sequence: 1
            fragment_type: execution
            content: |
              Run the build and confirm it compiles cleanly before moving on, then
              run the full test suite and read the first failing assertion closely.
    """)
    if requires:
        body += f"{requires}\n"
    if related:
        body += f"{related}\n"
    return body


def _edges(store: LadybugStore, rel: str, source_id: str) -> list[str]:
    rows = store.execute(
        f"MATCH (s:Skill {{skill_id: $id}})-[:{rel}]->(t:Skill) RETURN t.skill_id ORDER BY t.skill_id",
        {"id": source_id},
    )
    return [str(r[0]) for r in rows]


def _fresh_store(tmp_path: Path) -> str:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()
    return db_path


# --------------------------------------------------------------------------
# load_yaml parsing
# --------------------------------------------------------------------------


def test_load_yaml_parses_requires_and_related(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text(_skill_yaml("sk-a", requires="requires: [sk-b]", related="related: [sk-c, sk-d]"))
    record = _load_yaml(f)
    assert record.requires == ["sk-b"]
    assert record.related == ["sk-c", "sk-d"]


def test_load_yaml_defaults_edges_empty(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text(_skill_yaml("sk-a"))
    record = _load_yaml(f)
    assert record.requires == []
    assert record.related == []


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _rec(skill_id: str, requires: list[str], related: list[str]) -> ReviewRecord:
    return ReviewRecord(
        skill_type="domain",
        skill_id=skill_id,
        canonical_name="X",
        category="engineering",
        skill_class="domain",
        domain_tags=[],
        always_apply=False,
        phase_scope=[],
        category_scope=[],
        author="t",
        change_summary="c",
        raw_prose="prose",
        requires=requires,
        related=related,
    )


def test_validate_rejects_self_edge() -> None:
    errs = _validate(_rec("sk-a", ["sk-a"], []))
    assert any("self-edge" in e for e in errs)


def test_validate_rejects_non_kebab_target() -> None:
    errs = _validate(_rec("sk-a", [], ["Sk_B!"]))
    assert any("kebab-case" in e for e in errs)


def test_validate_accepts_valid_edges() -> None:
    errs = _validate(_rec("sk-a", ["sk-b"], ["sk-c"]))
    # No edge-specific complaints (unrelated fragment validation may fire on
    # this minimal record; we only assert the edge fields are accepted).
    assert not any("edge" in e or "kebab-case" in e for e in errs)


# --------------------------------------------------------------------------
# ingest → edges in the graph
# --------------------------------------------------------------------------


def test_ingest_writes_both_edge_types(tmp_path: Path) -> None:
    db_path = _fresh_store(tmp_path)
    # Ingest the targets first so refs resolve immediately.
    for sid in ("sk-b", "sk-c"):
        f = tmp_path / f"{sid}.yaml"
        f.write_text(_skill_yaml(sid))
        with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
            assert ingest_main([str(f), "--yes"]) == EXIT_OK

    f = tmp_path / "sk-a.yaml"
    f.write_text(_skill_yaml("sk-a", requires="requires: [sk-b]", related="related: [sk-c]"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(f), "--yes"]) == EXIT_OK

    store = LadybugStore(db_path)
    store.open()
    assert _edges(store, "REQUIRES_COMPOSITIONAL", "sk-a") == ["sk-b"]
    assert _edges(store, "REFERENCES_CONCEPTUAL", "sk-a") == ["sk-c"]
    store.close()


def test_ingest_no_edges_when_fields_absent(tmp_path: Path) -> None:
    db_path = _fresh_store(tmp_path)
    f = tmp_path / "sk-a.yaml"
    f.write_text(_skill_yaml("sk-a"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(f), "--yes"]) == EXIT_OK

    store = LadybugStore(db_path)
    store.open()
    assert _edges(store, "REQUIRES_COMPOSITIONAL", "sk-a") == []
    assert _edges(store, "REFERENCES_CONCEPTUAL", "sk-a") == []
    store.close()


def test_single_ingest_forward_ref_is_warning_not_error(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    db_path = _fresh_store(tmp_path)
    f = tmp_path / "sk-a.yaml"
    # sk-z does not exist — edge is skipped with a warning, ingest still succeeds.
    f.write_text(_skill_yaml("sk-a", requires="requires: [sk-z]"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(f), "--yes"]) == EXIT_OK
    err = capsys.readouterr().err
    assert "sk-z" in err and "warning" in err.lower()

    store = LadybugStore(db_path)
    store.open()
    assert _edges(store, "REQUIRES_COMPOSITIONAL", "sk-a") == []
    store.close()


def test_batch_resolves_cross_pack_forward_ref(tmp_path: Path) -> None:
    db_path = _fresh_store(tmp_path)
    batch = tmp_path / "batch"
    batch.mkdir()
    # sk-a (sorted first) requires sk-b, which appears later in the batch.
    (batch / "sk-a.yaml").write_text(_skill_yaml("sk-a", requires="requires: [sk-b]"))
    (batch / "sk-b.yaml").write_text(_skill_yaml("sk-b"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(batch), "--yes"]) == EXIT_OK

    store = LadybugStore(db_path)
    store.open()
    # The retry pass wired the forward ref.
    assert _edges(store, "REQUIRES_COMPOSITIONAL", "sk-a") == ["sk-b"]
    store.close()


def test_reingest_replaces_outgoing_edges(tmp_path: Path) -> None:
    db_path = _fresh_store(tmp_path)
    for sid in ("sk-b", "sk-c"):
        f = tmp_path / f"{sid}.yaml"
        f.write_text(_skill_yaml(sid))
        with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
            assert ingest_main([str(f), "--yes"]) == EXIT_OK

    fa = tmp_path / "sk-a.yaml"
    fa.write_text(_skill_yaml("sk-a", related="related: [sk-b]"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(fa), "--yes"]) == EXIT_OK

    # Re-author sk-a: related now points to sk-c instead. --force overwrites.
    fa.write_text(_skill_yaml("sk-a", related="related: [sk-c]"))
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(fa), "--yes", "--force"]) == EXIT_OK

    store = LadybugStore(db_path)
    store.open()
    # Old edge gone, new edge present — no duplication.
    assert _edges(store, "REFERENCES_CONCEPTUAL", "sk-a") == ["sk-c"]
    store.close()


def test_validation_error_blocks_ingest(tmp_path: Path) -> None:
    db_path = _fresh_store(tmp_path)
    f = tmp_path / "sk-a.yaml"
    f.write_text(_skill_yaml("sk-a", requires="requires: [sk-a]"))  # self-edge
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(f), "--yes"]) == EXIT_VALIDATION
