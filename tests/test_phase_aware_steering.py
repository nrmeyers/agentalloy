"""Phase-aware steering tests — verify activation vs turn-based injection."""

import tempfile
from pathlib import Path

from agentalloy.phase_aware_steering import PHASE_PERSONAS, PhaseAwareSteering
from agentalloy.skill_engine import SkillEngine
from agentalloy.state_store import StateStore


def _make_steering() -> PhaseAwareSteering:
    """Create a PhaseAwareSteering with a temp DuckDB store."""
    db_path = str(Path(tempfile.mkdtemp()) / "state.duck")
    store = StateStore(db_path)
    engine = SkillEngine()
    return PhaseAwareSteering(store, engine)


def test_first_turn_is_activation() -> None:
    """First turn (no prior phase) is an activation turn."""
    steering = _make_steering()
    context, ctx_type = steering.build_context(prompt="hello")
    assert ctx_type == 2  # activation
    assert "Phase: Intake" in context
    assert "Your Responsibilities" in context


def test_subsequent_turn_same_phase_is_skills() -> None:
    """Second turn in same phase injects only skills (or nothing)."""
    steering = _make_steering()

    # First turn: activation
    _, ctx_type_1 = steering.build_context(prompt="hello")
    assert ctx_type_1 == 2

    # Second turn: same phase, should be skills-only or no injection
    context_2, ctx_type_2 = steering.build_context(prompt="search for something")
    assert ctx_type_2 in (0, 1)  # 0 = no injection, 1 = skills only
    # Should NOT contain full persona
    assert "Your Responsibilities" not in context_2


def test_phase_change_triggers_activation() -> None:
    """Phase transition triggers a new activation turn."""
    steering = _make_steering()

    # First turn in spec
    _, ctx_type_1 = steering.build_context(prompt="spec task")
    assert ctx_type_1 == 2

    # Advance phase
    steering.state_store.record_artifact("spec", "spec-exit", "spec done")
    steering.state_store.advance_phase("design")

    # Next turn should be activation for design phase
    context_2, ctx_type_2 = steering.build_context(prompt="design task")
    assert ctx_type_2 == 2
    assert "Phase: Design" in context_2
    assert "HOW the spec will be implemented" in context_2


def test_all_phases_have_personas() -> None:
    """Every phase in the phase order has a persona defined."""
    from agentalloy.state_store import PHASE_ORDER

    expected_phases = list(PHASE_ORDER)
    for phase in expected_phases:
        assert phase in PHASE_PERSONAS, f"Missing persona for phase: {phase}"
        assert len(PHASE_PERSONAS[phase]) > 100, f"Persona too short for {phase}"


def test_skill_diffing_skips_unchanged() -> None:
    """If skills haven't changed between turns, no injection."""
    steering = _make_steering()

    # First turn: activation (persona + contract, no skills)
    steering.build_context(prompt="hello")

    # Second turn: first time skills are injected (if any found)
    steering.build_context(prompt="hello")

    # Third turn with same prompt → same skills → no injection
    context_3, ctx_type_3 = steering.build_context(prompt="hello")
    assert ctx_type_3 == 0  # no injection (skills unchanged from turn 2)


def test_reset_clears_tracking() -> None:
    """Reset clears phase and skill tracking."""
    steering = _make_steering()

    # First turn
    steering.build_context(prompt="hello")

    # Reset
    steering.reset()

    # Next turn should be activation again
    _, ctx_type = steering.build_context(prompt="hello")
    assert ctx_type == 2


def test_extract_user_prompt_from_proxy() -> None:
    """Verify prompt extraction from message list."""
    from agentalloy.proxy import _extract_user_prompt

    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]
    assert _extract_user_prompt(messages) == "second question"


def test_extract_user_prompt_empty() -> None:
    """Empty message list returns empty string."""
    from agentalloy.proxy import _extract_user_prompt

    assert _extract_user_prompt([]) == ""


def test_extract_user_prompt_multimodal() -> None:
    """Handles multimodal content (list of parts)."""
    from agentalloy.proxy import _extract_user_prompt

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }
    ]
    prompt = _extract_user_prompt(messages)
    assert "describe this" in prompt
