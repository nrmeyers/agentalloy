"""E7 — windowed process-class slot demotion (build contract retrieval-process-demotion).

Pure-transform units, selector FAR-tier semantics, and the pack classification
audit that pins ``category_scope`` as the deterministic process/framework
distinguisher the mechanism keys on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentalloy.ingest import FragmentRecord, ReviewRecord
from agentalloy.reads.models import ActiveFragment
from agentalloy.retrieval.domain import (
    _about_exempt_skills,
    demote_process_skills,
    skill_granular_select,
)

_PACKS_DIR = Path(__file__).resolve().parents[1] / "src" / "agentalloy" / "_packs"


@pytest.fixture(autouse=True)
def _demotion_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the transform active regardless of the host env (default is `auto`,
    which reads LM_ASSIST). Tests of the posture itself override inside their
    bodies."""
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION", "on")


# The measured leak set (2026-07-07 campaign) plus its peers from the core packs.
_KNOWN_PROCESS_SKILLS = (
    "test-driven-development",
    "verification-before-completion",
    "brainstorming",
    "incremental-implementation",
    "code-review-practices",
    "debugging-systematic",
    "planning-and-task-breakdown",
    "git-workflow",
)
_KNOWN_FRAMEWORK_SKILLS = (
    "fastapi-routing-and-path-operations",
    "react-context-patterns",
)


def _frag(
    frag_id: str,
    skill_id: str,
    *,
    scope: tuple[str, ...] | None = ("framework",),
    ftype: str = "execution",
) -> ActiveFragment:
    return ActiveFragment(
        fragment_id=frag_id,
        fragment_type=ftype,
        sequence=1,
        content="",
        skill_id=skill_id,
        version_id=f"{skill_id}-v1",
        skill_class="domain",
        category="engineering",
        domain_tags=[],
        category_scope=scope,
    )


_PROCESS = ("process",)


# -------- demote_process_skills: the pure transform --------


def test_demotes_process_fragments_to_tail_when_domain_skill_in_window() -> None:
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("g1", "gold"),
        _frag("t2", "tdd", scope=_PROCESS),
        _frag("v1", "verification", scope=_PROCESS),
        _frag("g2", "gold"),
    ]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert [f.fragment_id for f in reordered] == ["g1", "g2", "t1", "t2", "v1"]
    assert demoted == {"tdd", "verification"}


def test_noop_when_window_is_all_process() -> None:
    # Generic-shaped pool: the only non-process skill sits outside the window.
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("v1", "verification", scope=_PROCESS),
        _frag("b1", "brainstorming", scope=_PROCESS),
        _frag("i1", "incremental", scope=_PROCESS),
        _frag("g1", "gold"),  # 5th distinct skill; window at k=2 is 4
    ]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert reordered == pool
    assert demoted == frozenset()


def test_noop_when_no_process_skills() -> None:
    pool = [_frag("g1", "gold"), _frag("h1", "helper")]
    reordered, demoted = demote_process_skills(pool, k=4)
    assert reordered == pool
    assert demoted == frozenset()


def test_noop_on_empty_pool() -> None:
    assert demote_process_skills([], k=4) == ([], frozenset())


def test_none_category_scope_counts_as_non_process() -> None:
    # Pre-column corpora hydrate category_scope=None — must not be demoted.
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "legacy", scope=None)]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert [f.fragment_id for f in reordered] == ["g1", "t1"]
    assert demoted == {"tdd"}


def test_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION", "off")
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert reordered == pool
    assert demoted == frozenset()


def test_default_auto_is_on_without_arbitration(monkeypatch: pytest.MonkeyPatch) -> None:
    # v2 posture: unset == auto == active iff Stage B arbitration is not running.
    monkeypatch.delenv("AGENTALLOY_PROCESS_DEMOTION", raising=False)
    monkeypatch.delenv("LM_ASSIST", raising=False)
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert [f.fragment_id for f in reordered] == ["g1", "t1"]
    assert demoted == {"tdd"}


def test_default_auto_is_off_under_arbitration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stage B makes the process/filler judgment semantically; auto stands down.
    monkeypatch.delenv("AGENTALLOY_PROCESS_DEMOTION", raising=False)
    monkeypatch.setenv("LM_ASSIST", "arbitrate")
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert reordered == pool
    assert demoted == frozenset()


def test_explicit_on_overrides_arbitration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION", "on")
    monkeypatch.setenv("LM_ASSIST", "arbitrate")
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert demoted == {"tdd"}


def test_window_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # W=1: only the lead skill is inspected; a process lead means no evidence of
    # an on-domain alternative -> no-op even though gold ranks second.
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION_WINDOW", "1")
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=4)
    assert reordered == pool
    assert demoted == frozenset()


def test_malformed_window_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION_WINDOW", "banana")
    pool = [_frag("t1", "tdd", scope=_PROCESS), _frag("g1", "gold")]
    reordered, demoted = demote_process_skills(pool, k=2)
    assert [f.fragment_id for f in reordered] == ["g1", "t1"]
    assert demoted == {"tdd"}


def test_determinism() -> None:
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("g1", "gold"),
        _frag("v1", "verification", scope=_PROCESS),
    ]
    first = demote_process_skills(pool, k=2)
    second = demote_process_skills(list(pool), k=2)
    assert [f.fragment_id for f in first[0]] == [f.fragment_id for f in second[0]]
    assert first[1] == second[1]


# -------- v2: aboutness exemption (build contract retrieval-process-demotion-v2) --------


def test_exempt_skill_keeps_position_and_stays_out_of_far() -> None:
    # Name-probe shape: the query is about TDD, but a framework skill sneaks
    # into the window via dense noise — the v1 failure mode. Exempt TDD keeps
    # its fused lead; the un-exempt process peer still demotes.
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("f1", "framework-noise"),
        _frag("v1", "verification", scope=_PROCESS),
    ]
    reordered, demoted = demote_process_skills(pool, k=2, about_exempt=frozenset({"tdd"}))
    assert [f.fragment_id for f in reordered] == ["t1", "f1", "v1"]
    assert demoted == {"verification"}


def test_exempt_skill_counts_as_window_competition() -> None:
    # Window of [tdd(exempt), verification]: the exempt lead counts as an
    # on-topic alternative, so the remaining process filler demotes even with
    # no framework skill in the window.
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("v1", "verification", scope=_PROCESS),
    ]
    reordered, demoted = demote_process_skills(pool, k=1, about_exempt=frozenset({"tdd"}))
    assert [f.fragment_id for f in reordered] == ["t1", "v1"]
    assert demoted == {"verification"}


def test_empty_exempt_set_is_v1_equivalent() -> None:
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("g1", "gold"),
        _frag("v1", "verification", scope=_PROCESS),
    ]
    assert demote_process_skills(pool, k=2, about_exempt=frozenset()) == demote_process_skills(
        pool, k=2
    )


def test_about_exempt_is_dense_process_prefix() -> None:
    frags = {
        "o1": _frag("o1", "oidc"),
        "s1": _frag("s1", "secrets"),
        "t1": _frag("t1", "tdd", scope=_PROCESS),
        "v1": _frag("v1", "verification", scope=_PROCESS),
    }
    # Domain-shaped dense leg: process filler sits behind >=2 domain skills
    # (measured shallowest rank 3 across all 18 domain tasks) -> no exemption.
    assert _about_exempt_skills(["o1", "s1", "t1"], frags) == frozenset()
    # About-shaped dense leg: process skills lead -> the process prefix is exempt.
    assert _about_exempt_skills(["t1", "v1", "o1"], frags) == frozenset({"tdd", "verification"})
    # ONE interloper is tolerated (_ABOUT_PREFIX_INTERLOPERS=1): topic probes
    # occasionally rank a single adjacent framework skill just above the subject.
    assert _about_exempt_skills(["o1", "t1", "s1", "v1"], frags) == frozenset({"tdd"})
    assert _about_exempt_skills(["t1", "o1", "v1"], frags) == frozenset({"tdd", "verification"})


def test_about_exempt_skips_unhydrated_fragments() -> None:
    # Fragments filtered out of the hydrated pool must not end the prefix.
    frags = {"t1": _frag("t1", "tdd", scope=_PROCESS)}
    assert _about_exempt_skills(["ghost", "t1"], frags) == frozenset({"tdd"})
    assert _about_exempt_skills([], frags) == frozenset()


# -------- skill_granular_select: demoted == FAR last-resort --------


def test_selector_demoted_skill_backfills_only_after_top_skill_drained() -> None:
    # gold has 2 frags, fw2 has 1: NEAR budget covers 3 of k=4. The last slot
    # must deepen nothing (gold drained) and only then fall to demoted tdd.
    pool = [
        _frag("g1", "gold", ftype="setup"),
        _frag("g2", "gold", ftype="execution"),
        _frag("f1", "fw2", ftype="verification"),
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("t2", "tdd", scope=_PROCESS),
    ]
    selected, _ = skill_granular_select(pool, 4, demoted_skill_ids=frozenset({"tdd"}))
    ids = [f.fragment_id for f in selected]
    assert ids[:3] == ["g1", "g2", "f1"] or set(ids[:3]) == {"g1", "g2", "f1"}
    assert len([i for i in ids if i.startswith("t")]) == 1
    assert ids[3].startswith("t")


def test_selector_demoted_skill_gets_zero_slots_when_domain_fills_k() -> None:
    pool = [
        _frag("g1", "gold", ftype="setup"),
        _frag("g2", "gold", ftype="execution"),
        _frag("g3", "gold", ftype="verification"),
        _frag("f1", "fw2", ftype="overview"),
        _frag("f2", "fw2", ftype="execution"),
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("t2", "tdd", scope=_PROCESS),
    ]
    selected, _ = skill_granular_select(pool, 4, demoted_skill_ids=frozenset({"tdd"}))
    assert all(f.skill_id != "tdd" for f in selected)
    assert len(selected) == 4


def test_selector_no_demotion_is_byte_identical_legacy() -> None:
    pool = [
        _frag("g1", "gold"),
        _frag("f1", "fw2"),
        _frag("t1", "tdd", scope=_PROCESS),
    ]
    legacy, legacy_rank = skill_granular_select(pool, 3)
    empty_set, empty_rank = skill_granular_select(pool, 3, demoted_skill_ids=frozenset())
    none_set, none_rank = skill_granular_select(pool, 3, demoted_skill_ids=None)
    assert [f.fragment_id for f in legacy] == [f.fragment_id for f in empty_set]
    assert [f.fragment_id for f in legacy] == [f.fragment_id for f in none_set]
    assert legacy_rank == empty_rank == none_rank


# -------- end-to-end shape: transform feeding the selector --------


def test_transform_plus_selector_reproduces_strip_sim_with_backfill() -> None:
    # The 2026-07-07 leak shape: TDD leads the fused order on a domain task.
    # After demotion the gold skill owns the depth slots and TDD only backfills.
    pool = [
        _frag("t1", "tdd", scope=_PROCESS),
        _frag("g1", "gold", ftype="setup"),
        _frag("g2", "gold", ftype="execution"),
        _frag("v1", "verification", scope=_PROCESS),
        _frag("g3", "gold", ftype="verification"),
        _frag("f1", "fw2", ftype="overview"),
    ]
    reordered, demoted = demote_process_skills(pool, k=4)
    selected, skills_ranked = skill_granular_select(reordered, 4, demoted_skill_ids=demoted)
    assert skills_ranked[0] == "gold"
    assert all(f.skill_id in {"gold", "fw2"} for f in selected)
    assert len(selected) == 4


# -------- retrieval-path wiring: demotion fires inside retrieve_domain_candidates --------


def _record(skill_id: str, *, scope: list[str], contents: list[str]) -> ReviewRecord:
    return ReviewRecord(
        skill_id=skill_id,
        canonical_name=skill_id,
        category="engineering",
        skill_class="domain",
        domain_tags=["webhooks"],
        always_apply=False,
        phase_scope=[],
        category_scope=scope,
        author="test",
        change_summary="initial",
        raw_prose=" ".join(contents),
        fragments=[
            FragmentRecord(sequence=i + 1, fragment_type="execution", content=c)
            for i, c in enumerate(contents)
        ],
        tier=None,
    )


@pytest.fixture
def demotion_corpus(corpus_dir: Path) -> Path:
    """Corpus copy seeded with one process-scope and one framework skill whose
    fragments lexically match the probe task."""
    from agentalloy.ingest import _insert  # pyright: ignore[reportPrivateUsage]
    from agentalloy.install.importer import reembed_corpus
    from agentalloy.storage.fragment_store import LanceFragmentStore
    from agentalloy.storage.skill_store import open_skill_store
    from tests.support import StubLMClient

    ss = open_skill_store(str(corpus_dir / "agentalloy.duck"))
    _insert(
        ss,
        _record(
            "webhook-signature-gold",
            scope=["framework"],
            contents=[
                "Verify the webhook signature header before processing the payload.",
                "Reject webhook requests whose signature timestamp is stale.",
            ],
        ),
        force=False,
    )
    _insert(
        ss,
        _record(
            "tdd-process",
            scope=["process"],
            contents=[
                "Write the webhook signature verification test first.",
                "Red-green-refactor the webhook signature handler.",
            ],
        ),
        force=False,
    )
    stub = StubLMClient()
    fs = LanceFragmentStore(corpus_dir / "fragments.lance")
    reembed_corpus(fs, ss, embed=lambda t: stub.embed(model="stub", texts=t), model="stub")
    fs.rebuild_fts_index()
    fs.close()
    ss.close()
    return corpus_dir


def _retrieve(corpus: Path, k: int) -> list[ActiveFragment]:
    from agentalloy.retrieval.domain import retrieve_domain_candidates
    from agentalloy.storage.fragment_store import LanceFragmentStore
    from agentalloy.storage.skill_store import open_skill_store
    from tests.support import StubLMClient

    ss = open_skill_store(str(corpus / "agentalloy.duck"), read_only=True)
    fs = LanceFragmentStore(corpus / "fragments.lance")
    try:
        result = retrieve_domain_candidates(
            ss,
            StubLMClient(),
            fs,
            task="verify the webhook signature before processing",
            phase="build",
            domain_tags=None,
            k=k,
            embedding_model="stub",
        )
        return list(result.candidates)
    finally:
        fs.close()
        ss.close()


def test_retrieval_path_demotes_process_skill(demotion_corpus: Path) -> None:
    selected = _retrieve(demotion_corpus, k=2)
    assert selected, "expected candidates from the seeded corpus"
    assert all(f.skill_id != "tdd-process" for f in selected), (
        f"process skill won a slot over the on-domain skill: {[f.skill_id for f in selected]}"
    )
    # NB: which non-process skill wins the slots is stub-embedding-dependent;
    # the paired kill-switch test below proves tdd-process is in the fused pool
    # and only the demotion keeps it out of the selection.


def test_retrieval_path_kill_switch_restores_legacy(
    demotion_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTALLOY_PROCESS_DEMOTION", "off")
    selected = _retrieve(demotion_corpus, k=2)
    # Legacy round-robin gives the process sibling a slot whenever it is fused
    # into the pool — the leak this contract exists to fix.
    assert any(f.skill_id == "tdd-process" for f in selected)


# -------- classification audit: category_scope is the load-bearing metadata --------


def _pack_category_scope(skill_id: str) -> list[str]:
    matches = list(_PACKS_DIR.glob(f"*/{skill_id}.yaml"))
    assert matches, f"pack YAML for {skill_id} not found under _packs/"
    data = yaml.safe_load(matches[0].read_text())
    raw = data.get("category_scope") or []
    return [raw] if isinstance(raw, str) else list(raw)


@pytest.mark.parametrize("skill_id", _KNOWN_PROCESS_SKILLS)
def test_known_generic_skills_are_process_scope(skill_id: str) -> None:
    assert "process" in _pack_category_scope(skill_id), (
        f"{skill_id} lost category_scope=[process] — E7 demotion silently stops "
        "covering it; if the recat is deliberate, update this audit AND re-measure "
        "the domain benchmark (spec AC1)."
    )


@pytest.mark.parametrize("skill_id", _KNOWN_FRAMEWORK_SKILLS)
def test_known_framework_skills_are_not_process_scope(skill_id: str) -> None:
    assert "process" not in _pack_category_scope(skill_id), (
        f"{skill_id} became category_scope process — E7 would demote a domain "
        "skill; that is almost certainly an authoring mistake."
    )
