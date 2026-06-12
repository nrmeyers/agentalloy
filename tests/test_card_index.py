"""Stage 0 — skill-card indexing.

Covers the four guarantees the design requires:

(a) ``prefix`` mode changes the embedded/BM25-indexed text but never the stored
    fragment ``content`` (LadybugDB) returned by reads;
(b) cards rank (boost their skill) but never assemble — ``_apply_card_boost``;
(c) ``off`` mode is a byte-identical no-op vs the pre-Stage-0 index;
(d) the ``description`` column round-trips through ingest and reads.

Plus ``delete_cards`` scoping (the skill-scoped-rebuild fix) and
``build_card_text`` segment omission.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

from agentalloy.ingest import EXIT_OK, _load_yaml  # pyright: ignore[reportPrivateUsage]
from agentalloy.ingest import main as ingest_main
from agentalloy.reads import get_active_skill_by_id
from agentalloy.reads.active import _optional_str  # pyright: ignore[reportPrivateUsage]
from agentalloy.reembed.cli import (
    FragmentNeedingEmbedding,
    _indexed_text,  # pyright: ignore[reportPrivateUsage]
    reembed_fragments,
)
from agentalloy.retrieval.domain import _apply_card_boost  # pyright: ignore[reportPrivateUsage]
from agentalloy.storage.card_index import (
    CARD_FRAGMENT_TYPE,
    CardIndexMode,
    build_card_text,
    card_fragment_id,
    is_card_id,
    skill_id_from_card_id,
)
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import (
    FragmentEmbedding,
    VectorStore,
    open_or_create,
)
from tests.support import StubLMClient

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _frag(
    fid: str,
    content: str,
    *,
    skill_id: str = "sk-a",
    canonical_name: str = "Skill A",
    domain_tags: tuple[str, ...] = ("python",),
    description: str | None = "does a thing",
) -> FragmentNeedingEmbedding:
    return FragmentNeedingEmbedding(
        fragment_id=fid,
        content=content,
        fragment_type="execution",
        skill_id=skill_id,
        category="design",
        canonical_name=canonical_name,
        domain_tags=domain_tags,
        description=description,
    )


def _stub_embed_fn() -> tuple[list[str], object]:
    """Return (captured_texts, embed_fn) — embed_fn records each text embedded."""
    captured: list[str] = []
    stub = StubLMClient()

    def embed_fn(text: str) -> list[float]:
        captured.append(text)
        return stub.embed(model="stub-embed", texts=[text])[0]

    return captured, embed_fn


def _prose_of(vs: VectorStore, fragment_id: str) -> str:
    row = vs._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT prose FROM fragment_embeddings WHERE fragment_id = ?", [fragment_id]
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------
# build_card_text — segment omission
# --------------------------------------------------------------------------


def test_build_card_text_full() -> None:
    assert (
        build_card_text("React Hooks", ["react", "frontend"], "manage component state")
        == "skill: React Hooks — tags: react, frontend — manage component state"
    )


def test_build_card_text_omits_empty_tags() -> None:
    assert build_card_text("Plain Skill", [], "a description") == (
        "skill: Plain Skill — a description"
    )
    assert build_card_text("Plain Skill", None, "a description") == (
        "skill: Plain Skill — a description"
    )


def test_build_card_text_omits_empty_description() -> None:
    assert build_card_text("Tagged", ["x", "y"], None) == "skill: Tagged — tags: x, y"
    assert build_card_text("Tagged", ["x", "y"], "  ") == "skill: Tagged — tags: x, y"


def test_build_card_text_name_only() -> None:
    assert build_card_text("Bare", None, None) == "skill: Bare"


def test_card_id_round_trip() -> None:
    cid = card_fragment_id("my-skill")
    assert is_card_id(cid)
    assert skill_id_from_card_id(cid) == "my-skill"
    assert not is_card_id("my-skill-v1-f1")


# --------------------------------------------------------------------------
# (a) prefix mode — indexed text changes, stored content does not
# --------------------------------------------------------------------------


def test_indexed_text_prefix_prepends_header() -> None:
    frag = _frag("f1", "BODY TEXT")
    out = _indexed_text(frag, CardIndexMode.PREFIX)
    assert out != frag.content
    assert out.startswith("skill: Skill A — tags: python — does a thing")
    assert frag.content in out  # body preserved verbatim, just prefixed


def test_prefix_mode_indexes_header_but_stores_body_unchanged(tmp_path: Path) -> None:
    vs = open_or_create(tmp_path / "vectors.duck")
    captured, embed_fn = _stub_embed_fn()
    frag = _frag("f1", "BODY TEXT")

    stats = reembed_fragments(
        [frag],
        embed_fn=embed_fn,  # type: ignore[arg-type]
        vector_store=vs,
        embedding_model="stub",
        card_index=CardIndexMode.PREFIX,
    )

    assert stats.embedded == 1
    # The text handed to the embedder is the prefixed/indexed form...
    assert captured == [_indexed_text(frag, CardIndexMode.PREFIX)]
    assert captured[0] != frag.content
    # ...and the DuckDB prose column carries that same indexed text.
    assert _prose_of(vs, "f1") == captured[0]
    assert _prose_of(vs, "f1") != frag.content


def test_prefix_mode_leaves_ladybug_content_untouched(tmp_path: Path) -> None:
    """The LadybugDB Fragment.content (source of /compose prose) is never
    rewritten by card indexing — only the DuckDB indexed representation is."""
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(yaml_file), "--yes"]) == EXIT_OK

    store.open()
    content = store.scalar(
        "MATCH (:Skill {skill_id: 'test-domain-skill'})-[:HAS_VERSION]->(v)"
        "-[:DECOMPOSES_TO]->(f:Fragment {sequence: 1}) RETURN f.content"
    )
    store.close()
    # The stored content has no card header — prefix mode only touched DuckDB.
    assert content is not None
    assert not str(content).startswith("skill:")


# --------------------------------------------------------------------------
# (c) off mode — byte-identical no-op
# --------------------------------------------------------------------------


def test_indexed_text_off_is_identity() -> None:
    frag = _frag("f1", "BODY TEXT")
    assert _indexed_text(frag, CardIndexMode.OFF) is frag.content


def test_off_mode_prose_equals_content(tmp_path: Path) -> None:
    vs = open_or_create(tmp_path / "vectors.duck")
    captured, embed_fn = _stub_embed_fn()
    frag = _frag("f1", "BODY TEXT")

    reembed_fragments(
        [frag],
        embed_fn=embed_fn,  # type: ignore[arg-type]
        vector_store=vs,
        embedding_model="stub",
        card_index=CardIndexMode.OFF,
    )

    assert captured == [frag.content]
    assert _prose_of(vs, "f1") == frag.content


def test_off_mode_matches_default(tmp_path: Path) -> None:
    """``card_index=OFF`` is the default — explicit OFF == omitting the arg."""
    frag = _frag("f1", "BODY TEXT")

    vs_default = open_or_create(tmp_path / "a.duck")
    _, fn_a = _stub_embed_fn()
    reembed_fragments(
        [frag],
        embed_fn=fn_a,
        vector_store=vs_default,
        embedding_model="stub",  # type: ignore[arg-type]
    )

    vs_off = open_or_create(tmp_path / "b.duck")
    _, fn_b = _stub_embed_fn()
    reembed_fragments(
        [frag],
        embed_fn=fn_b,  # type: ignore[arg-type]
        vector_store=vs_off,
        embedding_model="stub",
        card_index=CardIndexMode.OFF,
    )

    assert _prose_of(vs_default, "f1") == _prose_of(vs_off, "f1") == frag.content


# --------------------------------------------------------------------------
# (b) _apply_card_boost — cards rank, never assemble
# --------------------------------------------------------------------------


def test_card_boost_no_op_on_card_free_list() -> None:
    """Default (off/prefix) corpus has no cards → list returned unchanged."""
    fused = ["a", "b", "c"]
    skill_of = {"a": "sk-1", "b": "sk-2", "c": "sk-1"}
    out = _apply_card_boost(fused, skill_of)
    assert out == fused  # identity order preserved


def test_card_boost_promotes_skill_and_strips_card() -> None:
    # sk-2's card ranks at the very top while sk-2's only fragment "c" trails.
    # The card lifts "c" to the card's rank, and the card id is stripped.
    card2 = card_fragment_id("sk-2")
    fused = [card2, "a", "b", "c"]  # card(sk-2), a=sk-1, b=sk-1, c=sk-2
    skill_of = {"a": "sk-1", "b": "sk-1", "c": "sk-2"}

    out = _apply_card_boost(fused, skill_of)

    # No card ids survive — cards never assemble.
    assert all(not is_card_id(fid) for fid in out)
    # sk-2's fragment "c" is promoted above "b" by its top-ranked card.
    assert out == ["a", "c", "b"]


def test_card_boost_keeps_real_order_when_card_ranks_low() -> None:
    # Card ranks below the skill's fragment → no reordering, card still stripped.
    card1 = card_fragment_id("sk-1")
    fused = ["a", "b", card1]
    skill_of = {"a": "sk-1", "b": "sk-2"}
    out = _apply_card_boost(fused, skill_of)
    assert out == ["a", "b"]


# --------------------------------------------------------------------------
# delete_cards scoping — the skill-scoped-rebuild fix
# --------------------------------------------------------------------------


def _insert_card(vs: VectorStore, skill_id: str) -> None:
    stub = StubLMClient()
    vs.insert_embeddings(
        [
            FragmentEmbedding(
                fragment_id=card_fragment_id(skill_id),
                embedding=stub.embed(model="stub", texts=[skill_id])[0],
                skill_id=skill_id,
                category="design",
                fragment_type=CARD_FRAGMENT_TYPE,
                embedded_at=0,
                embedding_model="stub",
                prose=f"skill: {skill_id}",
            )
        ]
    )


def test_delete_cards_unscoped_drops_all(tmp_path: Path) -> None:
    vs = open_or_create(tmp_path / "v.duck")
    _insert_card(vs, "sk-1")
    _insert_card(vs, "sk-2")
    assert vs.count_cards() == 2
    assert vs.delete_cards() == 2
    assert vs.count_cards() == 0


def test_delete_cards_scoped_drops_only_that_skill(tmp_path: Path) -> None:
    """The fix: a skill-scoped rebuild must not wipe other skills' cards."""
    vs = open_or_create(tmp_path / "v.duck")
    _insert_card(vs, "sk-1")
    _insert_card(vs, "sk-2")

    dropped = vs.delete_cards(skill_id="sk-1")

    assert dropped == 1
    assert vs.count_cards() == 1
    # sk-2's card survives the scoped delete.
    row = vs._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT skill_id FROM fragment_embeddings WHERE fragment_type = ?",
        [CARD_FRAGMENT_TYPE],
    ).fetchone()
    assert row is not None and row[0] == "sk-2"


# --------------------------------------------------------------------------
# (d) description round-trip + _optional_str
# --------------------------------------------------------------------------


def test_optional_str_normalizes() -> None:
    assert _optional_str(None) is None
    assert _optional_str("") is None
    assert _optional_str("  ") is None
    assert _optional_str("x") == "x"
    assert _optional_str("  trimmed  ") == "trimmed"


def test_load_yaml_carries_description(tmp_path: Path) -> None:
    yaml_file = tmp_path / "with_desc.yaml"
    yaml_file.write_text(_DOMAIN_YAML + "description: a one-line self description\n")
    record = _load_yaml(yaml_file)
    assert record.description == "a one-line self description"


def test_load_yaml_defaults_description_blank(tmp_path: Path) -> None:
    yaml_file = tmp_path / "no_desc.yaml"
    yaml_file.write_text(_DOMAIN_YAML)
    record = _load_yaml(yaml_file)
    assert record.description == ""


def test_description_round_trips_through_ingest(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML + "description: my skill description\n")
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(yaml_file), "--yes"]) == EXIT_OK

    store.open()
    skill = get_active_skill_by_id(store, "test-domain-skill")
    store.close()
    assert skill is not None
    assert skill.description == "my skill description"


def test_blank_description_round_trips_to_none(tmp_path: Path) -> None:
    """No ``description:`` → "" on the record → NULL on insert → None on read."""
    db_path = str(tmp_path / "ladybug")
    store = LadybugStore(db_path)
    store.open()
    store.migrate()
    store.close()

    yaml_file = tmp_path / "domain.yaml"
    yaml_file.write_text(_DOMAIN_YAML)
    with patch("agentalloy.ingest.get_settings", return_value=_FakeSettings(db_path)):
        assert ingest_main([str(yaml_file), "--yes"]) == EXIT_OK

    store.open()
    skill = get_active_skill_by_id(store, "test-domain-skill")
    store.close()
    assert skill is not None
    assert skill.description is None


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, db_path: str) -> None:
        self.ladybug_db_path = db_path


_DOMAIN_YAML = textwrap.dedent("""\
    skill_type: domain
    skill_id: test-domain-skill
    canonical_name: Test Domain Skill
    category: engineering
    skill_class: domain
    domain_tags: [testing, pytest]
    always_apply: false
    phase_scope: null
    category_scope: null
    author: test
    change_summary: unit test
    raw_prose: |
      Run pytest with the -x flag to stop on first failure. This is the
      fastest way to get useful feedback during a debug loop because the
      stack trace from the very first failing assertion is rarely buried
      under cascading downstream failures.

      All tests pass with exit code 0; non-zero indicates at least one
      failure or collection error. Wire this exit code into your CI step
      so a regression blocks the merge rather than emitting a green check.
    fragments:
      - sequence: 1
        fragment_type: execution
        content: |
          Run pytest with the -x flag to stop on first failure. This is the
          fastest way to get useful feedback during a debug loop because the
          stack trace from the very first failing assertion is rarely buried
          under cascading downstream failures.
      - sequence: 2
        fragment_type: verification
        content: |
          All tests pass with exit code 0; non-zero indicates at least one
          failure or collection error. Wire this exit code into your CI step
          so a regression blocks the merge rather than emitting a green check.
""")
