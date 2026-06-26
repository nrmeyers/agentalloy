"""Unit tests for the load-bearing invariants module.

Covers path-glob normalization, derivation from exit_gates + authored
prose_invariants, the prose substring check, and the overlay fall-back guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import agentalloy
from agentalloy.signals.invariants import (
    _normalize_gate_path,
    check_prose,
    derive_invariants,
    overlay_prose,
)


class TestNormalizeGatePath:
    @pytest.mark.parametrize(
        ("glob", "expected"),
        [
            ("docs/design/**/approach.md", "approach.md"),  # filename behind wildcard dir
            (".agentalloy/contracts/build/*.md", ".agentalloy/contracts/build/"),  # dir prefix
            ("src/**", "src/"),
            ("tests/**/*.py", "tests/"),
            ("**/*.md", None),  # no literal anchor
            ("*", None),
            ("tasks.md", "tasks.md"),  # fully literal file
            ("docs/api.md", "docs/api.md"),  # fully literal nested file
            ("Makefile", "Makefile"),  # literal, no extension
            ("", None),
            ("   ", None),
        ],
    )
    def test_normalization(self, glob: str, expected: str | None) -> None:
        assert _normalize_gate_path(glob) == expected


class TestDeriveInvariants:
    def test_real_sdd_skill_derives_path_tokens(self) -> None:
        path = Path(agentalloy.__file__).parent / "_packs" / "sdd" / "sdd-design-and-planning.yaml"
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        inv = derive_invariants(data)
        # The deterministic gate paths must surface as invariants.
        assert "approach.md" in inv
        assert "tasks.md" in inv
        assert "test-plan.md" in inv
        assert ".agentalloy/contracts/build/" in inv

    def test_shipped_prose_satisfies_its_own_invariants(self) -> None:
        # A shipped skill must never violate its own linter, or every
        # customization would be impossible.
        path = Path(agentalloy.__file__).parent / "_packs" / "sdd" / "sdd-design-and-planning.yaml"
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert check_prose(data["raw_prose"], derive_invariants(data)) == []

    def test_all_bundled_workflow_skills_satisfy_own_invariants(self) -> None:
        # CI guard: every bundled workflow skill's prose must contain all of its
        # own invariants (derived paths + authored prose_invariants). A future
        # prose edit that drops a load-bearing token fails here, not in the field.
        packs = Path(agentalloy.__file__).parent / "_packs"
        offenders: dict[str, list[str]] = {}
        for f in packs.rglob("*.yaml"):
            if f.name == "pack.yaml":
                continue
            data: dict[str, Any] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if data.get("skill_class") != "workflow":
                continue
            missing = check_prose(data.get("raw_prose", "") or "", derive_invariants(data))
            if missing:
                offenders[f.stem] = missing
        assert offenders == {}, f"skills violating their own invariants: {offenders}"

    def test_authored_invariants_are_appended(self) -> None:
        skill = {
            "exit_gates": {"all_of": [{"artifact_exists": {"path": "docs/**/approach.md"}}]},
            "prose_invariants": ["agentalloy phase set build", "agentalloy task next"],
        }
        inv = derive_invariants(skill)
        assert inv == ["approach.md", "agentalloy phase set build", "agentalloy task next"]

    def test_empty_when_no_gates_no_authored(self) -> None:
        # The critical safety case: a gate-less system skill yields no
        # invariants, so the linter/guard is a no-op.
        assert derive_invariants({"skill_class": "system", "raw_prose": "x"}) == []

    def test_dedupe_order_preserving(self) -> None:
        skill = {
            "exit_gates": {
                "all_of": [
                    {"artifact_exists": {"path": "docs/**/tasks.md"}},
                    {"artifact_contains": {"path": "spec/**/tasks.md", "sections": ["Tasks"]}},
                ]
            },
            "prose_invariants": ["tasks.md", "go run"],
        }
        # Both globbed gate paths normalize to the filename `tasks.md`, and the
        # authored list repeats it — collapses to one, first-seen position kept.
        assert derive_invariants(skill) == ["tasks.md", "go run"]

    def test_fully_literal_gate_path_kept_whole(self) -> None:
        # A gate path with no glob is the exact path the agent writes — keep it
        # verbatim, do not reduce to the bare filename.
        skill = {"exit_gates": {"all_of": [{"artifact_exists": {"path": "docs/a/tasks.md"}}]}}
        assert derive_invariants(skill) == ["docs/a/tasks.md"]

    def test_blank_authored_entries_dropped(self) -> None:
        assert derive_invariants({"prose_invariants": ["keep", "  ", ""]}) == ["keep"]


class TestCheckProse:
    def test_all_present(self) -> None:
        assert check_prose("run agentalloy task next then write tasks.md", ["tasks.md"]) == []

    def test_reports_missing_in_order(self) -> None:
        prose = "edit approach.md"
        missing = check_prose(prose, ["approach.md", "tasks.md", "test-plan.md"])
        assert missing == ["tasks.md", "test-plan.md"]

    def test_empty_invariants_never_missing(self) -> None:
        assert check_prose("", []) == []


class TestOverlayProse:
    SHIPPED: dict[str, Any] = {
        "skill_id": "demo",
        "raw_prose": "SHIPPED prose mentions tasks.md",
        "exit_gates": {"all_of": [{"artifact_exists": {"path": "docs/**/tasks.md"}}]},
        "domain_tags": ["a"],
    }

    def test_none_override_returns_shipped(self) -> None:
        eff, missing = overlay_prose(self.SHIPPED, None)
        assert eff is self.SHIPPED
        assert missing == []

    def test_valid_override_overlays_prose_keeps_structured(self) -> None:
        eff, missing = overlay_prose(self.SHIPPED, "REWORDED but keeps tasks.md", ["b"])
        assert missing == []
        assert eff["raw_prose"] == "REWORDED but keeps tasks.md"
        assert eff["exit_gates"] == self.SHIPPED["exit_gates"]  # structured field preserved
        assert eff["domain_tags"] == ["b"]
        assert self.SHIPPED["raw_prose"] == "SHIPPED prose mentions tasks.md"  # not mutated

    def test_violating_override_falls_back_to_shipped(self) -> None:
        eff, missing = overlay_prose(self.SHIPPED, "REWORDED and dropped the path token")
        assert missing == ["tasks.md"]
        assert eff["raw_prose"] == self.SHIPPED["raw_prose"]  # shipped prose served
