"""TC21 — SDD workflow prose encodes the self-drive phase-set handoffs.

Static assertion over the wheel-bundled ``_packs/sdd`` YAML: every workflow
skill's prose names the explicit forward ``agentalloy phase set <next>`` an agent
runs to advance itself, preserves its backward/bail routes, and ``sdd-ship``
encodes the user-confirmed reset back to intake. Extends the config-consistency
guard family; pairs with the guarded ``phase set`` (the command the prose tells
the agent to run only refuses when the exit artifact is missing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACKS = Path(__file__).parent.parent / "src" / "agentalloy" / "_packs" / "sdd"

# Forward (self-advance) routes each skill's prose must name. Mirrors the linear
# SDD graph and the two intake routes (full → spec, fast → sdd-fast).
_FORWARD: dict[str, list[str]] = {
    "sdd-intake.yaml": ["phase set spec", "phase set sdd-fast"],
    "sdd-spec-and-scoping.yaml": ["phase set design"],
    "sdd-design-and-planning.yaml": ["phase set build"],
    "sdd-build.yaml": ["phase set qa"],
    "sdd-verify-and-review.yaml": ["phase set ship"],
    "sdd-fast.yaml": ["phase set ship"],
    "sdd-deliver-and-ship.yaml": ["phase set intake"],  # the user-confirmed reset
}

# Backward / bail routes that must survive the prose rewrite (route-by-cause).
_BACKWARD: dict[str, list[str]] = {
    "sdd-spec-and-scoping.yaml": ["phase set sdd-fast"],
    "sdd-build.yaml": ["phase set design"],
    "sdd-verify-and-review.yaml": [
        "phase set build",
        "phase set design",
        "phase set spec",
    ],
    "sdd-fast.yaml": ["phase set spec"],
    "sdd-deliver-and-ship.yaml": ["phase set build"],
}


def _prose(name: str) -> str:
    data: dict[str, Any] = yaml.safe_load((_PACKS / name).read_text(encoding="utf-8"))
    return data["raw_prose"]


def test_every_sdd_skill_has_a_yaml() -> None:
    found = {p.name for p in _PACKS.glob("sdd-*.yaml")}
    assert found == set(_FORWARD), found


def test_forward_handoffs_present() -> None:
    for name, cmds in _FORWARD.items():
        prose = _prose(name)
        for cmd in cmds:
            assert cmd in prose, f"{name} is missing forward route `{cmd}`"


def test_backward_routes_preserved() -> None:
    for name, cmds in _BACKWARD.items():
        prose = _prose(name)
        for cmd in cmds:
            assert cmd in prose, f"{name} dropped backward route `{cmd}`"


def test_ship_is_terminal_and_user_confirmed() -> None:
    # crit 14: ship does not self-advance; the one way out is the user-confirmed
    # reset to intake. The prose must say so, not just carry the command.
    prose = _prose("sdd-deliver-and-ship.yaml")
    assert "phase set intake" in prose
    assert "terminal" in prose.lower()
    assert "stay in `ship`" in prose


def test_self_drive_language_present() -> None:
    # The behavioural contract: advance yourself, but stop on ambiguity. Spot-check
    # the phases that self-advance carry the "stop and surface it" escape hatch.
    for name in (
        "sdd-spec-and-scoping.yaml",
        "sdd-design-and-planning.yaml",
        "sdd-build.yaml",
    ):
        prose = _prose(name).lower()
        assert "advance yourself" in prose, name
        assert "surface it" in prose, name
