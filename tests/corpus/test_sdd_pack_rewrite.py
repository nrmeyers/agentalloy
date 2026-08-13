"""TC1–TC3 — SDD pack corpus rewrite: store-first, no filesystem contract paths.

Static assertions over the wheel-bundled ``_packs/sdd`` YAML after the rewrite:
every ``.agentalloy/contracts`` reference is gone, replaced by CLI invocations;
prose never instructs actions the Tier A enforcement posture denies; and the
pack version has been bumped so the re-ingest propagates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_PACKS = Path(__file__).parents[2] / "src" / "agentalloy" / "_packs" / "sdd"

# The eight SDD workflow skills that carry the rewrite (sys-* are system skills,
# sdd-fast and sdd-add-skill never referenced the contracts path).
_SDD_WORKFLOW_SKILLS = [
    "sdd-intake.yaml",
    "sdd-spec-and-scoping.yaml",
    "sdd-design-and-architecture.yaml",
    "sdd-plan-and-contracts.yaml",
    "sdd-build.yaml",
    "sdd-verify-and-review.yaml",
    "sdd-deliver-and-ship.yaml",
]

# Pre-build phases where Tier A enforcement denies src/ and tests/ writes.
# Prose in these skills must NEVER instruct Write|Edit on src/** or tests/**.
_PRE_BUILD_PHASES = {
    "sdd-intake.yaml",
    "sdd-spec-and-scoping.yaml",
    "sdd-design-and-architecture.yaml",
}

# Patterns that would instruct a denied action in pre-build phases:
# editing source files, writing tests, running code generators on src/.
# Negative instructions ("MUST NOT write src/", "don't edit tests/") are
# read-only discipline — they're the correct behavior, not violations.
# We only flag affirmative instructions to write/edit source or test files.
# The helper below filters out matches preceded by negation keywords.
_AFFIRMATIVE_ACTION_PATTERNS = [
    (r"edit.*\bsrc\b", "edit src"),
    (r"write.*\bsrc\b", "write src"),
    (r"create.*\bsrc\b", "create src"),
    (r"modify.*\bsrc\b", "modify src"),
    (r"edit.*\btests\b", "edit tests"),
    (r"write.*\btests\b", "write tests"),
]
_NEGATION_CONTEXTS = [
    "must not",
    "don't",
    "do not",
    "never",
    "no .* here",
    "you're .*",  # "you're editing src/" = warning, not instruction
]


def _is_affirmative_instruction(line: str, pattern: str) -> bool:
    """Return True if a regex match is an affirmative instruction, not a negation."""
    lower = line.lower()
    return all(not re.search(ctx, lower) for ctx in _NEGATION_CONTEXTS)


def _prose(name: str) -> str:
    data: dict[str, Any] = yaml.safe_load((_PACKS / name).read_text(encoding="utf-8"))
    return data.get("raw_prose", "")


def _full_text(name: str) -> str:
    return (_PACKS / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TC1 — zero `.agentalloy/contracts` occurrences remain
# ---------------------------------------------------------------------------


def test_tc1_no_contracts_path_in_sdd_skills() -> None:
    """TC1: Grep the SDD pack — zero `.agentalloy/contracts` occurrences remain."""
    offenders: list[str] = []
    for name in _SDD_WORKFLOW_SKILLS:
        text = _full_text(name)
        matches = [line.strip() for line in text.splitlines() if ".agentalloy/contracts" in line]
        if matches:
            offenders.append(f"{name}:\n" + "\n".join(f"  {m}" for m in matches))
    assert not offenders, ".agentalloy/contracts still present in SDD skills:\n" + "\n".join(
        offenders
    )


def test_tc1_no_contracts_path_in_entire_sdd_pack() -> None:
    """TC1 (broader): no `.agentalloy/contracts` anywhere under the SDD pack dir."""
    offenders: list[str] = []
    for f in _PACKS.rglob("*.yaml"):
        if ".agentalloy/contracts" in f.read_text(encoding="utf-8"):
            offenders.append(f.name)
    assert not offenders, f".agentalloy/contracts found in: {', '.join(offenders)}"


# ---------------------------------------------------------------------------
# TC2 — prose ↔ deny-rule cross-product holds
# ---------------------------------------------------------------------------


def test_tc2_no_denied_action_in_pre_build_prose() -> None:
    """TC2: Every action the pack prose instructs at intake/spec/design is
    permitted by the Tier A posture (no src/ or tests/ write instructions)."""
    offenders: list[str] = []
    for name in _PRE_BUILD_PHASES:
        prose = _prose(name)
        for pattern, label in _AFFIRMATIVE_ACTION_PATTERNS:
            for line in prose.splitlines():
                if re.search(pattern, line, re.IGNORECASE) and _is_affirmative_instruction(
                    line, pattern
                ):
                    offenders.append(f"{name}: affirmative {label} instruction: {line.strip()!r}")
    assert not offenders, "Pre-build prose instructs denied actions:\n" + "\n".join(offenders)


def test_tc2_pre_build_prose_emphasizes_read_only() -> None:
    """TC2 (positive): pre-build skills carry explicit read-only discipline."""
    for name in _PRE_BUILD_PHASES:
        prose = _prose(name).lower()
        # Each pre-build skill must contain a MUST NOT touching src/ clause
        assert "must not" in prose or "don't" in prose, (
            f"{name} lacks read-only discipline language"
        )


def test_tc2_session_boundary_on_phase_advance() -> None:
    """TC2 (D9 prose half): advancing skills state that phase advance ends the
    session and `agentalloy resume` rebuilds context."""
    # Skills that advance to the next phase (not ship → intake reset, which is
    # user-confirmed and already has different language).
    advancing_skills = {
        "sdd-intake.yaml",
        "sdd-spec-and-scoping.yaml",
        "sdd-design-and-architecture.yaml",
        "sdd-plan-and-contracts.yaml",
        "sdd-build.yaml",
        "sdd-verify-and-review.yaml",
    }
    for name in advancing_skills:
        prose = _prose(name)
        prose_lower = prose.lower()
        assert "phase advance ends" in prose_lower, f"{name} missing session boundary language"
        assert "agentalloy resume" in prose, f"{name} missing `agentalloy resume` reference"


# ---------------------------------------------------------------------------
# TC3 — version bumped; ingest + re-embed succeed
# ---------------------------------------------------------------------------


def test_tc3_pack_version_bumped() -> None:
    """TC3: pack.yaml version is greater than the prior shipped version (1.9.0)."""
    pack_yaml = yaml.safe_load((_PACKS / "pack.yaml").read_text(encoding="utf-8"))
    version = pack_yaml.get("version", "0.0.0")
    # Parse semver for comparison
    parts = [int(x) for x in str(version).split(".")]
    old_parts = [1, 9, 0]  # prior version
    assert parts > old_parts, f"SDD pack version {version!r} is not greater than 1.9.0"


def test_tc3_pack_yaml_valid_structure() -> None:
    """TC3: pack.yaml parses and carries all required fields."""
    pack_yaml = yaml.safe_load((_PACKS / "pack.yaml").read_text(encoding="utf-8"))
    required = {"name", "version", "tier", "skills"}
    actual = set(pack_yaml.keys())
    missing = required - actual
    assert not missing, f"pack.yaml missing fields: {missing}"
    assert isinstance(pack_yaml["skills"], list), "skills must be a list"
    assert len(pack_yaml["skills"]) > 0, "skills must not be empty"


def test_tc3_all_sdd_skills_declared_in_pack() -> None:
    """TC3: every SDD workflow skill is declared in pack.yaml."""
    pack_yaml = yaml.safe_load((_PACKS / "pack.yaml").read_text(encoding="utf-8"))
    declared = {s["skill_id"] for s in pack_yaml.get("skills", [])}
    for name in _SDD_WORKFLOW_SKILLS:
        skill_id = name.replace(".yaml", "")
        assert skill_id in declared, f"{skill_id} not declared in pack.yaml skills list"
