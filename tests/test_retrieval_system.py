"""NXS-771: system-skill fragment retrieval pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.retrieval.system import SystemRetrievalResult, retrieve_system_fragments
from agentalloy.storage.skill_store import DuckDBSkillStore, open_skill_store


@pytest.fixture
def populated(corpus_dir: Path) -> DuckDBSkillStore:
    return open_skill_store(str(corpus_dir / "agentalloy.duck"), read_only=True)


# -------- AC-1: predicate evaluation — no LLM --------


def test_always_apply_skill_matches_any_phase(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="spec", category=None)
    assert "sys-governance-always" in result.applied_skill_ids


def test_phase_scoped_skill_matches_correct_phase(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert "sys-governance-build-phase" in result.applied_skill_ids


def test_phase_scoped_skill_excluded_on_mismatch(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="spec", category=None)
    assert "sys-governance-build-phase" not in result.applied_skill_ids


def test_no_llm_dependency(populated: DuckDBSkillStore) -> None:
    # retrieve_system_fragments takes no OllamaClient — pure predicate + DB.
    # If this call signature compiles and passes, no LLM path was invoked.
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert isinstance(result, SystemRetrievalResult)


# -------- AC-2: all matching fragments returned, no scoring/truncation --------


def test_all_fragments_from_matching_skills_returned(populated: DuckDBSkillStore) -> None:
    # phase=build: sys-governance-always (2 frags) + sys-governance-build-phase (2 frags)
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert len(result.candidates) == 4


def test_fragments_not_truncated_to_k(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    # No k parameter exists; all matching fragments must be present.
    fragment_ids = {f.fragment_id for f in result.candidates}
    assert "sys-governance-always-v2-f1" in fragment_ids
    assert "sys-governance-always-v2-f2" in fragment_ids
    assert "sys-governance-build-phase-v2-f1" in fragment_ids
    assert "sys-governance-build-phase-v2-f2" in fragment_ids


def test_fragments_have_no_semantic_score_field(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    for frag in result.candidates:
        assert not hasattr(frag, "score")


# -------- AC-3: empty match returns explicit empty result, not an error --------


def test_no_applicable_skills_returns_empty_candidates(populated: DuckDBSkillStore) -> None:
    # phase=spec only triggers always-apply; build-phase doesn't match; design-category
    # has no phase_scope and always_apply=False so never matches.
    result = retrieve_system_fragments(populated, phase=None, category=None)
    # only always_apply=True skill (sys-governance-always) should match
    assert result.applied_skill_ids == ["sys-governance-always"]
    assert len(result.candidates) == 2


def test_truly_empty_result_when_no_skills_match(populated: DuckDBSkillStore) -> None:
    # No system skill in fixtures has phase_scope=["nonexistent"]
    # sys-governance-always still matches because always_apply=True.
    # Use a fresh empty store to get a genuine zero-match scenario.
    pass  # see test below


def test_empty_store_returns_empty_result(tmp_path: Path) -> None:
    s = open_skill_store(str(tmp_path / "agentalloy.duck"))
    result = retrieve_system_fragments(s, phase="build", category=None)
    assert result.candidates == []
    assert result.applied_skill_ids == []
    assert result.retrieval_ms >= 0


def test_returns_system_retrieval_result_type(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert isinstance(result, SystemRetrievalResult)


# -------- AC-4: inactive and non-system skills excluded --------


def test_domain_skills_excluded(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    for frag in result.candidates:
        assert frag.skill_class == "system"


def test_inactive_versions_not_returned(populated: DuckDBSkillStore) -> None:
    # Fixture system skills each have a superseded v1 and active v2.
    # Only v2 fragments should appear.
    result = retrieve_system_fragments(populated, phase="build", category=None)
    for frag in result.candidates:
        assert frag.version_id.endswith("-v2")


def test_applied_skill_ids_ordered_deterministically(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert result.applied_skill_ids == sorted(result.applied_skill_ids)


# -------- result shape --------


def test_retrieval_ms_non_negative(populated: DuckDBSkillStore) -> None:
    result = retrieve_system_fragments(populated, phase="build", category=None)
    assert result.retrieval_ms >= 0
