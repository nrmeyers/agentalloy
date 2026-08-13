"""Unit tests for the system skill Markdown parser."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentalloy.skill_md.parser import ParseError, parse_file, parse_text

_FIXTURES_DIR = Path(__file__).parents[2] / "fixtures"

_MINIMAL = textwrap.dedent("""\
    # My Governance Skill

    **skill_id:** sys-my-skill
    **category:** governance
    **always_apply:** true

    Do not do bad things.
""")

_FULL = textwrap.dedent("""\
    # Full Governance Skill

    **skill_id:** sys-full
    **category:** governance
    **always_apply:** false
    **phase_scope:** design,build
    **category_scope:** ops
    **author:** nate
    **change_summary:** initial load

    Line one.
    Line two.
""")

_PHASE_ONLY = textwrap.dedent("""\
    # Phase-scoped Skill

    **skill_id:** sys-phase-only
    **category:** governance
    **always_apply:** false
    **phase_scope:** review
    **category_scope:**

    Some content.
""")


def test_minimal_parse() -> None:
    s = parse_text(_MINIMAL)
    assert s.canonical_name == "My Governance Skill"
    assert s.skill_id == "sys-my-skill"
    assert s.category == "governance"
    assert s.always_apply is True
    assert s.phase_scope == []
    assert s.category_scope == []
    assert s.raw_prose == "Do not do bad things."


def test_full_parse() -> None:
    s = parse_text(_FULL)
    assert s.canonical_name == "Full Governance Skill"
    assert s.skill_id == "sys-full"
    assert s.always_apply is False
    assert s.phase_scope == ["design", "build"]
    assert s.category_scope == ["ops"]
    assert s.author == "nate"
    assert s.change_summary == "initial load"
    assert "Line one." in s.raw_prose
    assert "Line two." in s.raw_prose


def test_phase_only_scope() -> None:
    s = parse_text(_PHASE_ONLY)
    assert s.phase_scope == ["review"]
    assert s.category_scope == []


def test_missing_heading_raises() -> None:
    with pytest.raises(ParseError, match="no H1 heading"):
        parse_text("**skill_id:** sys-x\n\nContent.")


def test_missing_required_field_raises() -> None:
    bad = textwrap.dedent("""\
        # Name Only

        **always_apply:** true

        Content.
    """)
    with pytest.raises(ParseError, match="missing required field"):
        parse_text(bad)


def test_invalid_bool_raises() -> None:
    bad = textwrap.dedent("""\
        # Bad Bool

        **skill_id:** sys-bad
        **category:** governance
        **always_apply:** maybe

        Content.
    """)
    with pytest.raises(ParseError, match="must be a boolean"):
        parse_text(bad)


def test_extra_fields_are_preserved() -> None:
    extra = textwrap.dedent("""\
        # Extra Fields

        **skill_id:** sys-extra
        **category:** governance
        **always_apply:** false
        **future_field:** some_value

        Content.
    """)
    s = parse_text(extra)
    assert s.extra_fields.get("future_field") == "some_value"


def test_blank_lines_between_fields_allowed() -> None:
    with_blanks = textwrap.dedent("""\
        # Blank Lines

        **skill_id:** sys-blanks

        **category:** governance

        **always_apply:** false

        Content here.
    """)
    s = parse_text(with_blanks)
    assert s.skill_id == "sys-blanks"
    assert s.raw_prose == "Content here."


def test_prose_multiline() -> None:
    multi = textwrap.dedent("""\
        # Multi Prose

        **skill_id:** sys-multi
        **category:** governance
        **always_apply:** true

        First paragraph.

        Second paragraph.
    """)
    s = parse_text(multi)
    assert "First paragraph." in s.raw_prose
    assert "Second paragraph." in s.raw_prose


def test_skill_authoring_agent_fixture_parses() -> None:
    skill = parse_file(_FIXTURES_DIR / "skill-authoring-agent.md")
    assert skill.skill_id == "sys-skill-authoring-agent"
    assert skill.category == "tooling"
    assert skill.always_apply is False
    assert "domain" in skill.raw_prose.lower()
    assert "system" in skill.raw_prose.lower()


def test_defaults_for_optional_fields() -> None:
    minimal = textwrap.dedent("""\
        # Defaults

        **skill_id:** sys-defaults
        **category:** governance

        Content.
    """)
    s = parse_text(minimal)
    assert s.always_apply is False
    assert s.phase_scope == []
    assert s.category_scope == []
    assert s.author == "bootstrap"
    assert s.change_summary == "bootstrap load"
