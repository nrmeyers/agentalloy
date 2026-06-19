"""AC-2, AC-3: fragments of active versions; filters work; inactive fragments excluded."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.ingest import (
    FragmentRecord,
    ReviewRecord,
    _insert,  # type: ignore[reportPrivateUsage]
)
from agentalloy.reads import active as reads_active
from agentalloy.reads import get_active_fragments, get_active_fragments_for_skill
from agentalloy.storage.ladybug import LadybugStore


@pytest.fixture
def store(corpus_dir: Path) -> LadybugStore:
    s = LadybugStore(str(corpus_dir / "ladybug"))
    s.open()
    return s


def _workflow_record() -> ReviewRecord:
    return ReviewRecord(
        skill_id="test-workflow-skill",
        canonical_name="Test Workflow Skill",
        category="engineering",
        skill_class="workflow",
        domain_tags=["testing", "workflow"],
        always_apply=False,
        phase_scope=[],
        category_scope=[],
        author="test-author",
        change_summary="initial",
        raw_prose="Follow the workflow steps carefully.",
        # Workflow skills are raw_prose-only and carry no fragments
        # ("has fragments" <=> skill_class == "domain"); _insert ignores any
        # fragments declared on a non-domain record.
        fragments=[],
        tier=None,
    )


def _domain_record() -> ReviewRecord:
    return ReviewRecord(
        skill_id="test-domain-skill",
        canonical_name="Test Domain Skill",
        category="engineering",
        skill_class="domain",
        domain_tags=["testing", "domain"],
        always_apply=False,
        phase_scope=[],
        category_scope=[],
        author="test-author",
        change_summary="initial",
        raw_prose="Apply domain knowledge.",
        fragments=[
            FragmentRecord(
                sequence=1,
                fragment_type="execution",
                content="Apply the domain pattern here.",
            )
        ],
        tier=None,
    )


def test_returns_fragments_with_full_context(store: LadybugStore) -> None:
    fragments = get_active_fragments(store)
    assert fragments, "expected at least one active fragment"
    for f in fragments:
        assert f.fragment_id
        assert f.fragment_type in {
            "guardrail",
            "setup",
            "execution",
            "verification",
            "example",
            "rationale",
        }
        assert f.sequence >= 1
        assert f.content
        assert f.version_id.endswith("-v2")  # only active versions
        assert f.skill_class in {"domain", "system"}


def test_skill_class_filter_domain_only(store: LadybugStore) -> None:
    fragments = get_active_fragments(store, skill_class="domain")
    for f in fragments:
        assert f.skill_class == "domain"


def test_categories_filter_list_based(store: LadybugStore) -> None:
    # Per phase_to_categories locked mapping: design maps to [design, governance, meta]
    fragments = get_active_fragments(store, categories=["design", "governance", "meta"])
    categories = {f.category for f in fragments}
    assert categories <= {"design", "governance", "meta"}
    assert "design" in categories  # fixtures include design-category skills


def test_categories_filter_narrows_correctly(store: LadybugStore) -> None:
    only_build = get_active_fragments(store, skill_class="domain", categories=["build"])
    assert {f.category for f in only_build} == {"build"}


def test_domain_tags_filter(store: LadybugStore) -> None:
    py_frags = get_active_fragments(store, domain_tags=["python"])
    assert py_frags, "expected python-tagged fragments"
    for f in py_frags:
        assert "python" in f.domain_tags


def test_fragments_for_single_skill(store: LadybugStore) -> None:
    frags = get_active_fragments_for_skill(store, "py-fastapi-endpoint-design")
    assert frags
    for f in frags:
        assert f.skill_id == "py-fastapi-endpoint-design"
        assert f.version_id == "py-fastapi-endpoint-design-v2"


def test_unknown_skill_returns_empty(store: LadybugStore) -> None:
    assert get_active_fragments_for_skill(store, "does-not-exist") == []


def test_fragments_ordered_by_sequence(store: LadybugStore) -> None:
    frags = get_active_fragments_for_skill(store, "py-fastapi-endpoint-design")
    assert frags == sorted(frags, key=lambda f: f.sequence)


def test_superseded_version_fragments_excluded(store: LadybugStore) -> None:
    # Manually insert a fragment on a superseded version; confirm it's not returned.
    store.execute(
        """
        CREATE (f:Fragment {
            fragment_id: 'should-not-appear',
            fragment_type: 'execution',
            sequence: 99,
            content: 'from superseded version'
        })
        """
    )
    store.execute(
        """
        MATCH (v:SkillVersion {version_id: 'py-fastapi-endpoint-design-v1'}),
              (f:Fragment {fragment_id: 'should-not-appear'})
        CREATE (v)-[:DECOMPOSES_TO]->(f)
        """
    )
    ids = {f.fragment_id for f in get_active_fragments(store)}
    assert "should-not-appear" not in ids


# ---------------------------------------------------------------------------
# Workflow skill_class tests (moved from test_retrieval_workflow_class.py)
#
# Under the skill_class taxonomy, "has fragments" <=> skill_class == "domain":
# workflow skills are raw_prose-only and contribute NO retrievable fragments
# (their prose is injected by the SDD phase hook, never retrieved). So the only
# surviving fragment-retrieval invariant for workflow is exclusion — domain
# queries must never leak a workflow skill. The earlier
# "workflow fragments are returned" tests encoded the deleted skill_type model
# and were removed; test_domain_filter_excludes_workflow_fragments was already
# dropped as a duplicate of test_skill_class_filter_domain_only.
# ---------------------------------------------------------------------------


def test_domain_string_query_excludes_workflow(store: LadybugStore) -> None:
    """get_active_fragments(skill_class="domain") only returns domain, not workflow."""
    _insert(store, _workflow_record(), force=False)
    _insert(store, _domain_record(), force=False)

    fragments = reads_active.get_active_fragments(store, skill_class="domain")
    fragment_skill_ids = {f.skill_id for f in fragments}
    assert "test-domain-skill" in fragment_skill_ids
    assert "test-workflow-skill" not in fragment_skill_ids
