"""TC21 — SDD workflow prose encodes phase transitions via the API-first interface.

Static assertion over the wheel-bundled ``_packs/sdd`` YAML: every workflow
skill's prose references the state panel and the state-service endpoints for
phase transitions and artifact recording. The CLI commands have been replaced
with explicit endpoint calls (POST /state/advance, PUT /state/artifact) and
the agentalloy_query tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACKS = Path(__file__).parents[2] / "src" / "agentalloy" / "_packs" / "sdd"

# Forward transitions: each skill's prose must reference explicit phase
# advancement via the state service (not CLI commands, not auto-advance).
_FORWARD_MARKERS: dict[str, list[str]] = {
    "sdd-intake.yaml": ["state panel", "advance"],
    "sdd-spec-and-scoping.yaml": ["approval", "advance"],
    "sdd-design-and-architecture.yaml": ["approval", "advance"],
    "sdd-plan-and-contracts.yaml": ["approval", "advance"],
    "sdd-build.yaml": ["advance"],
    "sdd-verify-and-review.yaml": ["advance"],
    "sdd-fast.yaml": ["advance"],
    "sdd-deliver-and-ship.yaml": ["advance"],
    "sdd-flow.yaml": ["intake"],
    "sdd-add-skill.yaml": ["approval"],
}

# Backward / bail routes: the prose should mention going back to earlier phases
# when things go wrong. These are now expressed as natural language, not CLI.
_BACKWARD_MARKERS: dict[str, list[str]] = {
    "sdd-spec-and-scoping.yaml": ["fast"],  # bail to fast lane
    "sdd-build.yaml": ["design"],  # bail to design
    "sdd-verify-and-review.yaml": ["build", "design", "spec"],  # route-by-cause
    "sdd-fast.yaml": ["spec"],  # bail back to spec
    "sdd-deliver-and-ship.yaml": [],  # terminal
    "sdd-add-skill.yaml": ["intake"],  # bail to intake
    "sdd-flow.yaml": [],  # no backward routes
}

# Artifact recording: prose must reference the state-service artifact endpoint,
# not CLI commands and not the disabled marker-extraction path.
_ARTIFACT_ENDPOINT = "PUT /state/artifact"
_ARTIFACT_MARKER = "<!-- agentalloy:artifact"


def _prose(name: str) -> str:
    data: dict[str, Any] = yaml.safe_load((_PACKS / name).read_text(encoding="utf-8"))
    return data["raw_prose"]


def test_every_sdd_skill_has_a_yaml() -> None:
    expected = set(_FORWARD_MARKERS.keys())
    found = {p.name for p in _PACKS.glob("sdd-*.yaml")}
    assert found == expected, found


def test_forward_handoffs_present() -> None:
    """Each skill's prose references explicit phase advancement."""
    for name, markers in _FORWARD_MARKERS.items():
        prose = _prose(name)
        for marker in markers:
            assert marker in prose, f"{name} is missing forward reference `{marker}`"


def test_backward_routes_preserved() -> None:
    """Bail routes are still mentioned in the prose (as natural language)."""
    for name, markers in _BACKWARD_MARKERS.items():
        prose = _prose(name)
        for marker in markers:
            assert marker in prose, f"{name} dropped backward route reference `{marker}`"


def test_add_skill_forward_is_approval() -> None:
    """add-skill's forward route is human approval, not a phase set command."""
    prose = _prose("sdd-add-skill.yaml")
    assert "approval" in prose.lower()
    assert "agentalloy validate-pack" in prose  # scaffolding tool stays


def test_ship_is_terminal_and_user_confirmed() -> None:
    """Ship does not self-advance; the one way out is user-confirmed reset."""
    prose = _prose("sdd-deliver-and-ship.yaml")
    assert "terminal" in prose.lower() or "intake" in prose.lower()


def test_self_drive_language_present() -> None:
    """Phases that self-advance carry the 'stop and surface it' escape hatch."""
    for name in (
        "sdd-spec-and-scoping.yaml",
        "sdd-design-and-architecture.yaml",
        "sdd-build.yaml",
    ):
        prose = _prose(name).lower()
        assert "advance" in prose, name
        assert "surface" in prose or "stop" in prose, name


def test_qa_verdict_uses_store_endpoint() -> None:
    """QA records verdict via the artifact endpoint, not disk paths or markers."""
    prose = _prose("sdd-verify-and-review.yaml")
    assert _ARTIFACT_ENDPOINT in prose
    assert _ARTIFACT_MARKER not in prose
    # Must NOT instruct CLI artifact-set
    assert "agentalloy contract artifact-set" not in prose


def test_ship_delivery_uses_store_endpoint() -> None:
    """Ship records delivery and narrative via the artifact endpoint, not CLI."""
    prose = _prose("sdd-deliver-and-ship.yaml")
    assert _ARTIFACT_ENDPOINT in prose
    assert _ARTIFACT_MARKER not in prose
    # Must NOT instruct CLI artifact-set
    assert "agentalloy contract artifact-set" not in prose


def test_no_artifact_markers_in_any_prose() -> None:
    """Marker extraction is off by default — prose must not teach markers."""
    for path in _PACKS.glob("sdd-*.yaml"):
        prose = _prose(path.name)
        assert _ARTIFACT_MARKER not in prose, f"{path.name} still teaches disabled artifact markers"


def test_no_temp_file_transport_in_any_prose() -> None:
    """Deliverables go straight to the store — prose must not teach writing
    them to disk (temp files included)."""
    for path in _PACKS.glob("sdd-*.yaml"):
        prose = _prose(path.name)
        assert "temp file" not in prose, path.name
        assert "write the json to" not in prose.lower(), path.name


def test_artifact_recording_teaches_the_cli() -> None:
    """Every phase that records deliverables teaches `agentalloy artifact put`
    as the recording mechanism (PUT /state/artifact remains the fallback)."""
    for name in (
        "sdd-spec-and-scoping.yaml",
        "sdd-design-and-architecture.yaml",
        "sdd-plan-and-contracts.yaml",
        "sdd-build.yaml",
        "sdd-verify-and-review.yaml",
        "sdd-deliver-and-ship.yaml",
        "sdd-fast.yaml",
    ):
        prose = _prose(name)
        assert "agentalloy artifact put" in prose, name
        assert _ARTIFACT_ENDPOINT in prose, name


def test_no_cli_phase_set_in_any_prose() -> None:
    """No workflow skill prose contains `agentalloy phase set` commands."""
    for path in _PACKS.glob("sdd-*.yaml"):
        prose = _prose(path.name)
        assert "agentalloy phase set" not in prose, (
            f"{path.name} still contains CLI `agentalloy phase set`"
        )


def test_no_cli_contract_init_in_any_prose() -> None:
    """No workflow skill prose contains `agentalloy contract init` commands."""
    for path in _PACKS.glob("sdd-*.yaml"):
        prose = _prose(path.name)
        assert "agentalloy contract init" not in prose, (
            f"{path.name} still contains CLI `agentalloy contract init`"
        )
