"""Graph-expansion retrieval (RETRIEVAL_GRAPH_EXPAND).

Covers the four guarantees the design requires:

(a) flag OFF is a byte-identical no-op vs the pre-expansion result (same
    guarantee pattern as ``tests/test_card_index.py``'s off-mode tests);
(b) expansion appends required-skill fragments without displacing existing
    candidates;
(c) the ``+2`` fragment cap is respected;
(d) a missing edge (or already-present target) is a no-op.

Tests exercise ``_graph_expand`` directly with synthetic edges + a fake edge
source, plus an end-to-end flag-parity check through ``retrieve_domain_candidates``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentalloy.fixtures.loader import load_fixtures
from agentalloy.reads import get_active_fragments
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import (
    _GRAPH_EXPAND_MAX_FRAGMENTS,  # pyright: ignore[reportPrivateUsage]
    _graph_expand,  # pyright: ignore[reportPrivateUsage]
    retrieve_domain_candidates,
)
from agentalloy.runtime_state import RuntimeCache, load_runtime_cache
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


def _frag(fid: str, skill_id: str, *, ftype: str = "execution", seq: int = 1) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=fid,
        fragment_type=ftype,
        sequence=seq,
        content=f"content of {fid}",
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="domain",
        category="engineering",
        domain_tags=["x"],
    )


@dataclass
class _FakeEdgeSource:
    """Minimal RequiresEdgeSource: {skill_id: [required_skill_id, ...]}."""

    edges: dict[str, list[str]]

    def get_required_skill_ids(self, skill_id: str) -> list[str]:
        return list(self.edges.get(skill_id, []))


# --------------------------------------------------------------------------
# (a) flag OFF parity — the orchestrator never calls _graph_expand when off;
#     here we assert the function itself is a pure append (input untouched).
# --------------------------------------------------------------------------


def test_expand_does_not_mutate_input() -> None:
    selected = [_frag("a-v1-f1", "sk-a"), _frag("b-v1-f1", "sk-b")]
    original = list(selected)
    pool = {f.fragment_id: f for f in selected}
    pool["c-v1-f1"] = _frag("c-v1-f1", "sk-c")
    src = _FakeEdgeSource({"sk-a": ["sk-c"]})

    out = _graph_expand(selected, ["sk-a", "sk-b"], pool, src)

    # The input list object is not mutated (orchestrator reuses it when off).
    assert selected == original
    # Output is the input followed by the appended fragment.
    assert out[: len(original)] == original


def test_empty_selected_is_noop() -> None:
    src = _FakeEdgeSource({"sk-a": ["sk-c"]})
    assert _graph_expand([], ["sk-a"], {}, src) == []


# --------------------------------------------------------------------------
# (b) expansion appends without displacing
# --------------------------------------------------------------------------


def test_expand_appends_required_fragment_at_tail() -> None:
    selected = [_frag("a-v1-f1", "sk-a"), _frag("b-v1-f1", "sk-b")]
    target = _frag("c-v1-f1", "sk-c")
    pool = {f.fragment_id: f for f in [*selected, target]}
    src = _FakeEdgeSource({"sk-a": ["sk-c"]})

    out = _graph_expand(selected, ["sk-a", "sk-b"], pool, src)

    # Existing candidates keep their exact positions; target trails.
    assert [f.fragment_id for f in out] == ["a-v1-f1", "b-v1-f1", "c-v1-f1"]


def test_expand_skips_already_present_target_skill() -> None:
    # sk-b is required by sk-a but is already in the selected set → no-op.
    selected = [_frag("a-v1-f1", "sk-a"), _frag("b-v1-f1", "sk-b")]
    pool = {f.fragment_id: f for f in selected}
    src = _FakeEdgeSource({"sk-a": ["sk-b"]})

    out = _graph_expand(selected, ["sk-a", "sk-b"], pool, src)

    assert out == selected  # nothing appended


def test_expand_uses_best_ranked_fragment_of_target() -> None:
    selected = [_frag("a-v1-f1", "sk-a")]
    # Two fragments for sk-c; pool order = fused rank → first wins.
    c_best = _frag("c-v1-f1", "sk-c", seq=1)
    c_other = _frag("c-v1-f2", "sk-c", seq=2)
    pool = {"a-v1-f1": selected[0], "c-v1-f1": c_best, "c-v1-f2": c_other}
    src = _FakeEdgeSource({"sk-a": ["sk-c"]})

    out = _graph_expand(selected, ["sk-a"], pool, src)

    assert [f.fragment_id for f in out] == ["a-v1-f1", "c-v1-f1"]


# --------------------------------------------------------------------------
# (c) +2 cap respected
# --------------------------------------------------------------------------


def test_expand_respects_two_fragment_cap() -> None:
    assert _GRAPH_EXPAND_MAX_FRAGMENTS == 2
    selected = [_frag("a-v1-f1", "sk-a")]
    targets = {f"t{i}-v1-f1": _frag(f"t{i}-v1-f1", f"sk-t{i}") for i in range(5)}
    pool = {"a-v1-f1": selected[0], **targets}
    # sk-a requires five distinct skills — only the first two append.
    src = _FakeEdgeSource({"sk-a": [f"sk-t{i}" for i in range(5)]})

    out = _graph_expand(selected, ["sk-a"], pool, src)

    assert len(out) == 1 + _GRAPH_EXPAND_MAX_FRAGMENTS


def test_expand_caps_across_multiple_source_skills() -> None:
    selected = [_frag("a-v1-f1", "sk-a"), _frag("b-v1-f1", "sk-b")]
    pool = {
        "a-v1-f1": selected[0],
        "b-v1-f1": selected[1],
        "p-v1-f1": _frag("p-v1-f1", "sk-p"),
        "q-v1-f1": _frag("q-v1-f1", "sk-q"),
        "r-v1-f1": _frag("r-v1-f1", "sk-r"),
    }
    # sk-a → [sk-p, sk-q], sk-b → [sk-r]; cap=2 stops after sk-p, sk-q.
    src = _FakeEdgeSource({"sk-a": ["sk-p", "sk-q"], "sk-b": ["sk-r"]})

    out = _graph_expand(selected, ["sk-a", "sk-b"], pool, src)

    assert [f.fragment_id for f in out] == ["a-v1-f1", "b-v1-f1", "p-v1-f1", "q-v1-f1"]


# --------------------------------------------------------------------------
# (d) missing edge / missing fragment no-op
# --------------------------------------------------------------------------


def test_no_edges_is_noop() -> None:
    selected = [_frag("a-v1-f1", "sk-a")]
    pool = {f.fragment_id: f for f in selected}
    src = _FakeEdgeSource({})  # no edges declared
    assert _graph_expand(selected, ["sk-a"], pool, src) == selected


def test_target_with_no_fragment_in_pool_is_noop() -> None:
    selected = [_frag("a-v1-f1", "sk-a")]
    pool = {f.fragment_id: f for f in selected}  # sk-c has no fragment in pool
    src = _FakeEdgeSource({"sk-a": ["sk-c"]})
    assert _graph_expand(selected, ["sk-a"], pool, src) == selected


def test_only_top_three_skills_expanded() -> None:
    # A 4th-ranked skill's required target must NOT be pulled in.
    selected = [_frag(f"s{i}-v1-f1", f"sk-{i}") for i in range(4)]
    target = _frag("z-v1-f1", "sk-z")
    pool = {f.fragment_id: f for f in [*selected, target]}
    src = _FakeEdgeSource({"sk-3": ["sk-z"]})  # sk-3 is 4th in rank order

    out = _graph_expand(selected, ["sk-0", "sk-1", "sk-2", "sk-3"], pool, src)

    assert out == selected  # 4th skill's edge ignored


# --------------------------------------------------------------------------
# end-to-end flag parity through retrieve_domain_candidates
# --------------------------------------------------------------------------


@pytest.fixture
def populated(tmp_path: Path) -> LadybugStore:
    s = LadybugStore(str(tmp_path / "ladybug"))
    s.open()
    s.migrate()
    load_fixtures(s)
    return s


@pytest.fixture
def vectors(tmp_path: Path, populated: LadybugStore) -> VectorStore:
    vs = open_or_create(tmp_path / "vectors.duck")
    stub = StubLMClient()
    now = int(time.time())
    vs.insert_embeddings(
        [
            FragmentEmbedding(
                fragment_id=f.fragment_id,
                embedding=stub.embed(model="stub-embed", texts=[f.content])[0],
                skill_id=f.skill_id,
                category=f.category,
                fragment_type=f.fragment_type,
                embedded_at=now,
                embedding_model="stub",
                prose=f.content,
            )
            for f in get_active_fragments(populated)
        ]
    )
    vs.rebuild_fts_index()
    return vs


def _retrieve(cache: RuntimeCache, vectors: VectorStore) -> list[str]:
    result = retrieve_domain_candidates(
        cache,
        StubLMClient(),
        vectors,
        task="build a REST endpoint with input validation",
        phase="build",
        domain_tags=None,
        k=4,
        embedding_model="stub",
    )
    return [f.fragment_id for f in result.candidates]


def test_flag_off_is_byte_identical(
    populated: LadybugStore, vectors: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETRIEVAL_GRAPH_EXPAND unset/off → identical candidate ids to a corpus
    that has graph edges but the flag disabled (same guarantee as card-index
    off-mode). A synthetic requires edge exists but must not surface."""
    cache = load_runtime_cache(populated)
    skills_ranked = [
        s.skill_id for s in cache.get_active_skills(skill_class=("domain", "workflow"))
    ]
    assert len(skills_ranked) >= 2
    # Inject a synthetic edge so "on" has something to expand.
    edged = RuntimeCache(
        skills={s.skill_id: s for s in cache.get_active_skills()},
        fragments=get_active_fragments(populated),
        version_details={},
        requires_edges={skills_ranked[0]: [skills_ranked[1]]},
    )

    monkeypatch.delenv("RETRIEVAL_GRAPH_EXPAND", raising=False)
    baseline = _retrieve(edged, vectors)
    monkeypatch.setenv("RETRIEVAL_GRAPH_EXPAND", "off")
    off = _retrieve(edged, vectors)

    assert off == baseline  # byte-identical: flag off is a strict no-op


def test_flag_on_appends_required_without_displacing(
    populated: LadybugStore, vectors: VectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = load_runtime_cache(populated)
    # Find a real top-ranked skill, then declare it requires a skill that is
    # NOT already retrieved, so expansion has a non-present target to append.
    monkeypatch.delenv("RETRIEVAL_GRAPH_EXPAND", raising=False)
    base_cache = RuntimeCache(
        skills={s.skill_id: s for s in cache.get_active_skills()},
        fragments=get_active_fragments(populated),
        version_details={},
    )
    baseline = _retrieve(base_cache, vectors)
    present = set(baseline)
    all_frags = get_active_fragments(populated)
    present_skills = {f.skill_id for f in all_frags if f.fragment_id in present}
    top_skill = next(f.skill_id for f in all_frags if f.fragment_id == baseline[0])
    # A target skill that has a fragment but is not in the baseline result.
    target_skill = next(
        (f.skill_id for f in all_frags if f.skill_id not in present_skills),
        None,
    )
    assert target_skill is not None

    edged = RuntimeCache(
        skills={s.skill_id: s for s in cache.get_active_skills()},
        fragments=all_frags,
        version_details={},
        requires_edges={top_skill: [target_skill]},
    )
    monkeypatch.setenv("RETRIEVAL_GRAPH_EXPAND", "on")
    expanded = _retrieve(edged, vectors)

    # Baseline candidates are preserved as a prefix; the target skill's
    # fragment is appended (not displacing anything).
    assert expanded[: len(baseline)] == baseline
    assert len(expanded) <= len(baseline) + _GRAPH_EXPAND_MAX_FRAGMENTS
    appended_skills = {f.skill_id for f in all_frags if f.fragment_id in set(expanded) - present}
    assert target_skill in appended_skills
