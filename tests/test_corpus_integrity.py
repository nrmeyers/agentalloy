"""Corpus integrity — the in-package corpus v2 serves, checked without a model.

Runs in regular CI. Guards the pre-PR skill-selection eval
(``eval.skill_selection``): a gold skill that is missing from the corpus, or
out of scope for its task's phase, turns a selection miss into a corpus
problem — the eval would measure the corpus instead of the model.
"""

from __future__ import annotations

from collections import Counter

import pytest
from eval.domain_tasks import DOMAIN_TASKS
from eval.skill_selection import KNOWN_PHASE_MISMATCH

from agentalloy.skill_engine import SkillEngine


@pytest.fixture(scope="module")
def engine() -> SkillEngine:
    return SkillEngine(load_corpus=True)


def test_full_corpus_loads(engine: SkillEngine) -> None:
    assert len(engine.skills) >= 300
    assert len(engine.pack_catalog()) >= 30


def test_skill_ids_unique(engine: SkillEngine) -> None:
    dupes = [i for i, n in Counter(s.id for s in engine.skills.values()).items() if n > 1]
    assert dupes == []


def test_every_gold_skill_exists(engine: SkillEngine) -> None:
    missing = [
        (t.task_id, g) for t in DOMAIN_TASKS for g in t.gold_skills if g not in engine.skills
    ]
    assert missing == []


def test_gold_skill_phase_scope(engine: SkillEngine) -> None:
    out_of_phase = {
        t.task_id
        for t in DOMAIN_TASKS
        if not any(
            not engine.skills[g].phases or t.phase in engine.skills[g].phases for g in t.gold_skills
        )
    }
    assert out_of_phase == KNOWN_PHASE_MISMATCH


def test_assemble_skill_is_deterministic_over_gold(engine: SkillEngine) -> None:
    for t in DOMAIN_TASKS:
        a = engine.assemble_skill(list(t.gold_skills), phase=t.phase)
        b = engine.assemble_skill(list(t.gold_skills), phase=t.phase)
        assert a == b, t.task_id
