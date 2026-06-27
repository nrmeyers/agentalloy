"""Prose-drift + structural golden for the build-contract density work (#12 / #12b).

Guards the load-bearing pieces of the design→build hand-off so a future prose
rewrite cannot silently drop them:
  - §6 is a hard MUST for one build contract per task, naming the k budget.
  - the ≤2-domain_tags tag-focus rule + the named calendar anti-pattern/pattern.
  - the sdd-build contract_template steers authors to ONE dominant tech surface.
  - design's exit_gates.all_of carries the three enforcement nodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PACKS = Path(__file__).parent.parent / "src" / "agentalloy" / "_packs" / "sdd"


def _pack(name: str) -> dict[str, Any]:
    return yaml.safe_load((_PACKS / name).read_text(encoding="utf-8"))


def _design_prose() -> str:
    # Whitespace-flattened so phrase assertions are tolerant of YAML line wraps.
    return " ".join(_pack("sdd-design-and-planning.yaml")["raw_prose"].split())


def test_section6_is_hard_must() -> None:
    prose = _design_prose()
    assert "MUST" in prose
    assert "ONE build contract per task" in prose
    assert "never one whole-feature" in prose


def test_section6_names_k_cap() -> None:
    prose = _design_prose()
    assert "DEFAULT_K_BY_PHASE" in prose
    assert "starves" in prose  # the small-k starvation statement


def test_two_tag_rule_is_a_must() -> None:
    prose = _design_prose()
    assert "≤2" in prose
    assert "one dominant" in prose
    assert "at most one adjacent" in prose


def test_old_four_tag_example_removed() -> None:
    # The misleading 4-tag example must be gone; the one-dominant framing replaces it.
    assert "`pytest`, `fastapi`, `duckdb`, `async`" not in _design_prose()


def test_calendar_anti_pattern_and_split_present() -> None:
    prose = _design_prose()
    # The 7-tag calendar contract as the named anti-pattern.
    assert "[frontend, react, typescript, vite, calendar, css-grid, vitest]" in prose
    # The per-surface split as the pattern.
    for fragment in ("[calendar]", "[vite, react]", "[react, css-grid]", "[vitest]"):
        assert fragment in prose, fragment


def test_not_this_forbids_all_tech_contract() -> None:
    prose = _design_prose()
    assert "all-tech contract" in prose
    assert "[react, typescript, fastapi, duckdb" in prose


def test_build_template_single_surface() -> None:
    template = _pack("sdd-build.yaml")["contract_template"]
    assert "ONE dominant tech surface" in template
    assert "never every surface" in template  # domain_tags annotation


def test_design_exit_gate_has_three_enforcement_nodes() -> None:
    all_of = _pack("sdd-design-and-planning.yaml")["exit_gates"]["all_of"]
    keys: set[str] = set()
    for node in all_of:
        if isinstance(node, dict):
            keys.update(node.keys())
    assert "build_contracts_cover_tasks" in keys
    assert "build_contract_tag_focus" in keys
    assert "approval_recorded" in keys


def test_design_approval_node_keys_on_design_docs() -> None:
    # approval `since` must be the design docs, not the build contracts (so editing
    # a contract during build doesn't re-stale the approval).
    all_of = _pack("sdd-design-and-planning.yaml")["exit_gates"]["all_of"]
    appr = next(
        n["approval_recorded"] for n in all_of if isinstance(n, dict) and "approval_recorded" in n
    )
    assert appr["since"] == "docs/design/**/*.md"
