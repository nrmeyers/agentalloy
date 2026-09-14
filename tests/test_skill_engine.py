"""Skill engine tests — verify dynamic skill selection (T6)."""

from agentalloy.skill_engine import Skill, SkillEngine, SkillFragment


def _frag_skill(
    skill_id: str,
    frags: list[tuple[str, int]],
    pack: str = "test-pack",
    phases: list[str] | None = None,
    system: bool = False,
) -> Skill:
    """Helper: skill with typed fragments (type, sequence)."""
    return Skill(
        id=skill_id,
        name=skill_id,
        pack=pack,
        description=f"desc for {skill_id}",
        body="".join(f"b{seq}" for _t, seq in frags),
        phases=phases or ["build", "spec"],
        domain_tags=["test"],
        skill_class="system" if system else "domain",
        fragments=[
            SkillFragment(
                fragment_id=f"{skill_id}-f{seq}",
                fragment_type=f_type,
                sequence=seq,
                content=f"content {skill_id} {f_type} {seq}",
            )
            for f_type, seq in frags
        ],
    )


def test_skill_engine_get_skill_for() -> None:
    """Get skill candidates for a task."""
    engine = SkillEngine()

    # Get skills for spec phase
    skills = engine.get_skill_for("search for code", "spec", k=10)
    assert len(skills) > 0
    assert any(s.name == "code-search" for s in skills)


def test_skill_engine_compose_instructions() -> None:
    """Compose skill instructions."""
    engine = SkillEngine()
    skills = engine.get_skill_for("manage contracts", "design", k=10)

    instructions = engine.compose_instructions(skills)
    assert "# Active Skills" in instructions
    assert "contract-management" in instructions


def test_skill_engine_add_skill() -> None:
    """Add a custom skill."""
    engine = SkillEngine()
    initial_count = len(engine.list_skills())

    new_skill = Skill(
        id="custom-skill",
        name="custom-skill",
        description="A custom skill",
        body="Do something custom",
        phases=["build"],
        domain_tags=["custom"],
    )
    engine.add_skill(new_skill)

    assert len(engine.list_skills()) == initial_count + 1
    assert any(s.name == "custom-skill" for s in engine.list_skills())


def test_skill_engine_empty_compose() -> None:
    """Compose with no skills returns empty string."""
    engine = SkillEngine()
    instructions = engine.compose_instructions([])
    assert instructions == ""


def test_skill_phase_property() -> None:
    """Skill.phase returns first phase."""
    skill = Skill(id="test", name="test", phases=["design", "build"])
    assert skill.phase == "design"

    skill2 = Skill(id="test2", name="test2", phases=[])
    assert skill2.phase == "build"  # default


def test_skill_instructions_alias() -> None:
    """Skill.instructions is alias for body."""
    skill = Skill(id="test", name="test", body="hello world")
    assert skill.instructions == "hello world"


def test_skills_for_phase() -> None:
    """skills_for_phase returns matching skills."""
    engine = SkillEngine()
    build_skills = engine.skills_for_phase("build")
    assert len(build_skills) > 0
    assert all("build" in s.phases for s in build_skills)


def test_builtin_corpus_count() -> None:
    """Built-in corpus has 32 skills."""
    engine = SkillEngine()
    assert len(engine.list_skills()) == 32


# ---------------------------------------------------------------------------
# Fragment-native selection: pack catalog, skill rows, deterministic assembly
# ---------------------------------------------------------------------------


def test_pack_catalog_builtin_empty() -> None:
    """Built-in corpus has no pack metadata → empty catalog."""
    engine = SkillEngine()
    assert engine.pack_catalog() == []


def test_pack_catalog_from_packs() -> None:
    """pack_catalog: one row per pack with description and skill count."""
    from agentalloy.corpus_importer import PackMeta

    engine = SkillEngine()
    engine._packs = {
        "z-pack": PackMeta(name="z-pack", description="Z"),
        "a-pack": PackMeta(name="a-pack", description="A"),
    }
    engine.add_skill(_frag_skill("s1", [("execution", 1)], pack="z-pack"))
    engine.add_skill(_frag_skill("s2", [("execution", 1)], pack="z-pack"))
    engine.add_skill(_frag_skill("s3", [("execution", 1)], pack="a-pack"))
    catalog = engine.pack_catalog()
    assert [row["pack"] for row in catalog] == ["a-pack", "z-pack"]
    assert catalog[0]["description"] == "A"
    assert catalog[0]["skills"] == 1
    assert catalog[1]["skills"] == 2


def test_skills_catalog_fragment_breakdown() -> None:
    """skills_catalog rows carry the fragment-type breakdown."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("setup", 1), ("execution", 2), ("execution", 3)]))
    engine.add_skill(
        Skill(
            id="plain",
            name="plain",
            pack="test-pack",
            description="d",
            body="flat skill body",
        )
    )
    rows = {row["id"]: row for row in engine.skills_catalog(["test-pack"])}
    assert rows["s1"]["fragments"] == {"setup": 1, "execution": 2}
    assert rows["plain"]["fragments"] == {"body": 1}
    assert rows["s1"]["phases"] == ["build", "spec"]


def test_skills_catalog_filters_packs() -> None:
    """skills_catalog filters by the wanted packs only."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("in-a", [("execution", 1)], pack="a"))
    engine.add_skill(_frag_skill("in-b", [("execution", 1)], pack="b"))
    rows = engine.skills_catalog(["a"])
    assert [row["id"] for row in rows] == ["in-a"]


def test_assemble_skill_deterministic() -> None:
    """Same input → byte-identical render (spec point 3)."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("execution", 1), ("example", 2)]))
    engine.add_skill(_frag_skill("s2", [("setup", 1), ("guardrail", 2)]))
    first = engine.assemble_skill(["s1", "s2"], phase="build")
    second = engine.assemble_skill(["s1", "s2"], phase="build")
    assert first["skill"] == second["skill"]
    assert first["fragments"] == second["fragments"]
    assert first["source_skills"] == ["s1", "s2"]
    assert "# Dynamic Skill (build)" in first["skill"]


def test_assemble_skill_type_filter() -> None:
    """types filters non-head fragments; the head stays regardless."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("setup", 1), ("example", 2), ("execution", 3)]))
    result = engine.assemble_skill(["s1"], types=["execution"], phase="build")
    assert result["fragments"] == ["s1-f1", "s1-f3"]
    assert "content s1 setup 1" in result["skill"]  # head reserved
    assert "content s1 example 2" not in result["skill"]
    assert "content s1 execution 3" in result["skill"]


def test_assemble_skill_phase_drop() -> None:
    """Out-of-phase skills are dropped whole and reported."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("in", [("execution", 1)], phases=["build"]))
    engine.add_skill(_frag_skill("out", [("execution", 1)], phases=["deploy"]))
    result = engine.assemble_skill(["in", "out"], phase="build")
    assert result["source_skills"] == ["in"]
    assert "out (phase)" in result["dropped"]


def test_assemble_skill_head_reserve() -> None:
    """The minimum-sequence fragment is kept even if its type is filtered."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("example", 1), ("guardrail", 2)]))
    result = engine.assemble_skill(["s1"], types=["guardrail"], phase="build")
    # head (example @1) reserved, guardrail @2 included
    assert result["fragments"] == ["s1-f1", "s1-f2"]


def test_assemble_skill_cap_drops_tail_first() -> None:
    """16 fragments: cap drops the lowest-rank non-head before any rationale."""
    skill = _frag_skill(
        "big",
        [("example", i) for i in range(1, 11)] + [("rationale", i) for i in range(11, 17)],
    )
    for f in skill.fragments:
        f.content = " ".join(["word"] * 30)  # 30 words each
    engine = SkillEngine()
    engine.add_skill(skill)
    result = engine.assemble_skill(["big"], phase="build")
    assert result["word_count"] <= 600
    assert len(result["fragments"]) <= 15
    # rank order: examples (0) drop before rationales (1); head is never dropped
    assert "big-f2 (cap)" in result["dropped"]
    assert result["fragments"][0] == "big-f1"
    kept = set(result["fragments"])
    assert "big-f11" in kept  # first rationale survives the example drop


def test_assemble_skill_cap_enforces_word_budget() -> None:
    """The 600-word budget is enforced even when only high-value types remain."""
    skill = _frag_skill("big", [("execution", i) for i in range(1, 20)])
    for f in skill.fragments:
        f.content = " ".join(["word"] * 40)
    engine = SkillEngine()
    engine.add_skill(skill)
    result = engine.assemble_skill(["big"], phase="build")
    assert result["word_count"] <= 600
    assert len(result["fragments"]) <= 15
    assert result["fragments"][0] == "big-f1"  # head survives
    assert "(cap)" in " ".join(result["dropped"])


def test_assemble_skill_system_first() -> None:
    """System skills lead the render regardless of LFM order."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("plain-s", [("execution", 1)]))
    engine.add_skill(_frag_skill("sys-s", [("execution", 1)], system=True))
    result = engine.assemble_skill(["plain-s", "sys-s"], phase="build")
    assert result["source_skills"] == ["sys-s", "plain-s"]
    sys_pos = result["skill"].index("## skill: sys-s")
    plain_pos = result["skill"].index("## skill: plain-s")
    assert sys_pos < plain_pos


def test_assemble_skill_unknown_and_empty() -> None:
    """Unknown ids are dropped and reported; empty result renders nothing."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("execution", 1)]))
    result = engine.assemble_skill(["s1", "nope"], phase="build")
    assert "nope (unknown)" in result["dropped"]
    assert result["source_skills"] == ["s1"]

    empty = engine.assemble_skill(["nope"], phase="build")
    assert empty["skill"] == ""
    assert empty["source_skills"] == []
    assert "nope (unknown)" in empty["dropped"]


def test_assemble_skill_provenance_footer() -> None:
    """Footer counts fragments and source skills."""
    engine = SkillEngine()
    engine.add_skill(_frag_skill("s1", [("execution", 1), ("example", 2)]))
    engine.add_skill(_frag_skill("s2", [("execution", 1)]))
    result = engine.assemble_skill(["s1", "s2"], phase="build")
    assert "Provenance: 3 fragments from 2 skills" in result["skill"]
