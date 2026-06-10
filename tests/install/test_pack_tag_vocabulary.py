"""Guard: every non-exception pack's skills must carry the pack base tag.

Rationale: tag-based filtering (domain_tags=["webhooks"]) silently drops
skills that lack the pack's canonical base tag, even when those skills are
clearly part of that domain.  This test makes drift a CI failure rather than
a silent retrieval bug.

The rule is ADDITIVE-ONLY — existing tags are never removed; only the
pack directory name must appear somewhere in domain_tags.

EXCEPTIONS (packs where the dir name is NOT a meaningful consumer filter tag):
  core            — internal scaffolding, not a domain
  engineering     — cross-cutting umbrella, too broad
  conventions     — style/convention skills, not domain-specific
  meta            — meta-skills about the agent itself
  intake          — SDD intake phase, not a domain
  sdd             — lifecycle phase label, not a domain
  design-review   — review phase, not a domain
  code-review     — review phase, not a domain
  documentation   — process category, not a domain
  refactoring     — process category, not a domain
  testing         — process category, not a domain
  linting         — process category, not a domain
  performance     — cross-cutting concern, not a domain
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PACKS_ROOT = Path(__file__).resolve().parents[2] / "src" / "agentalloy" / "_packs"

# Packs where the directory name is not a meaningful domain filter tag.
# When adding a new pack here, document *why* in the module docstring above.
TAG_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "core",
        "engineering",
        "conventions",
        "meta",
        "intake",
        "sdd",
        "design-review",
        "code-review",
        "documentation",
        "refactoring",
        "testing",
        "linting",
        "performance",
    }
)

PACK_DIRS = sorted(
    d for d in PACKS_ROOT.iterdir() if (d / "pack.yaml").is_file() and d.name not in TAG_EXCEPTIONS
)


@pytest.mark.parametrize("pack_dir", PACK_DIRS, ids=lambda d: d.name)
def test_all_skills_carry_pack_base_tag(pack_dir: Path) -> None:
    """Every skill YAML in *pack_dir* must include the pack directory name in domain_tags."""
    pack_name = pack_dir.name
    missing: list[str] = []

    for skill_file in sorted(pack_dir.glob("*.yaml")):
        if skill_file.name == "pack.yaml":
            continue
        with open(skill_file) as fh:
            data = yaml.safe_load(fh)
        tags: list[str] = data.get("domain_tags") or []
        if pack_name not in tags:
            missing.append(skill_file.name)

    assert not missing, (
        f"Pack '{pack_name}': the following skills are missing the base tag '{pack_name}':\n"
        + "\n".join(f"  - {s}" for s in missing)
    )


def test_non_exception_packs_discovered() -> None:
    # Ensure the parametrize list isn't silently empty after an exceptions-list mishap.
    assert len(PACK_DIRS) >= 20, (
        f"only found {len(PACK_DIRS)} non-exception packs; exceptions list may be too broad"
    )
