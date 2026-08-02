"""Tests for the gate path-store validator (Issue #502).

Verifies that ``artifact_exists`` and ``artifact_contains`` leaves using
``path:`` with a glob targeting ``docs/{spec,design,qa,ship,fast}/**`` are
rejected at pack-load time, while legitimate ``path:`` globs and correct
``phase:``+``name:`` leaves are allowed.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from agentalloy.ingest import _validate_gate_spec
from agentalloy.pack_validation import (
    _STORE_BACKED_PHASES,
    _validate_gate_path_stores,
    _walk_gate_paths,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal mock that exposes an ``exit_gates`` attribute matching what
# ``_validate_gate_path_stores`` expects.


@dataclass
class MockRecord:
    """Minimal mock carrying an ``exit_gates`` dict."""

    exit_gates: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# _STORE_BACKED_PHASES sanity
# ---------------------------------------------------------------------------


def test_store_backed_phases_are_complete() -> None:
    """The canonical set matches all SDD store-backed phases."""
    assert {"spec", "design", "qa", "ship", "fast"} == _STORE_BACKED_PHASES


# ---------------------------------------------------------------------------
# _validate_gate_path_stores — negative (should reject)
# ---------------------------------------------------------------------------


def test_rejects_artifact_exists_path_qa() -> None:
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": "docs/qa/*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert len(errs) == 1
    assert "docs/qa/*.md" in errs[0]
    assert "store-backed" in errs[0]


def test_rejects_artifact_contains_path_ship() -> None:
    record = MockRecord(
        exit_gates={
            "all_of": [
                {
                    "artifact_contains": {
                        "path": "docs/ship/*.md",
                        "sections": ["Summary", "Rollback"],
                    },
                },
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert len(errs) == 1
    assert "docs/ship/*.md" in errs[0]


def test_rejects_artifact_exists_path_fast() -> None:
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": "docs/fast/*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert len(errs) == 1
    assert "docs/fast/*.md" in errs[0]


def test_rejects_all_store_backed_phases() -> None:
    """Each store-backed phase is individually detected."""
    for phase in _STORE_BACKED_PHASES:
        record = MockRecord(
            exit_gates={
                "all_of": [
                    {"artifact_exists": {"path": f"docs/{phase}/*.md"}},
                ]
            }
        )
        errs = _validate_gate_path_stores(record)
        assert len(errs) == 1, f"Expected rejection for phase {phase}"
        assert f"docs/{phase}/*.md" in errs[0]


def test_rejects_nested_any_of() -> None:
    """Validator walks any_of composites."""
    record = MockRecord(
        exit_gates={
            "any_of": [
                {"artifact_exists": {"path": "docs/qa/*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert len(errs) == 1
    assert "docs/qa/*.md" in errs[0]


def test_rejects_nested_not() -> None:
    """Validator walks not composites."""
    record = MockRecord(
        exit_gates={
            "not": {
                "artifact_exists": {"path": "docs/ship/*.md"},
            }
        }
    )
    errs = _validate_gate_path_stores(record)
    assert len(errs) == 1
    assert "docs/ship/*.md" in errs[0]


# ---------------------------------------------------------------------------
# _validate_gate_path_stores — positive (should allow)
# ---------------------------------------------------------------------------


def test_allows_artifact_exists_phase_form() -> None:
    """Store form with ``phase:`` + ``name:`` is allowed."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"phase": "qa", "name": "*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_artifact_contains_phase_form() -> None:
    """Store form with sections preserved is allowed."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {
                    "artifact_contains": {
                        "phase": "ship",
                        "name": "*.md",
                        "sections": ["Summary", "Rollback"],
                    },
                },
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_path_src_star() -> None:
    """``path: src/**`` is genuine on-disk content — allowed."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": "src/**"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_path_docs_solutions() -> None:
    """``path: docs/solutions/*.md`` is on-disk — allowed."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": "docs/solutions/*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_path_custom_skills() -> None:
    """``path: .agentalloy/custom-skills/**/*.yaml`` is on-disk — allowed."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": ".agentalloy/custom-skills/**/*.yaml"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_no_exit_gates() -> None:
    """Skills without exit_gates are silently allowed."""
    record = MockRecord(exit_gates=None)
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_non_artifact_predicates() -> None:
    """Non-artifact predicates are not checked."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"approval_recorded": {"since": "docs/fast/*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_path_with_phase_key() -> None:
    """If both ``path`` and ``phase`` are present, phase takes precedence
    at evaluation time, so we allow it (the error would be at gate
    evaluation, not authoring)."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {"path": "docs/qa/*.md", "phase": "qa", "name": "*.md"}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


# ---------------------------------------------------------------------------
# _validate_gate_path_stores — edge cases
# ---------------------------------------------------------------------------


def test_allows_empty_exit_gates() -> None:
    record = MockRecord(exit_gates={})
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_non_dict_exit_gates() -> None:
    record = MockRecord(exit_gates="not-a-dict")
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_artifact_exists_with_no_args() -> None:
    """Artifact predicate with no args dict is tolerated."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": {}},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


def test_allows_artifact_exists_with_non_dict_args() -> None:
    """Artifact predicate with non-dict args is tolerated."""
    record = MockRecord(
        exit_gates={
            "all_of": [
                {"artifact_exists": "bad-args"},
            ]
        }
    )
    errs = _validate_gate_path_stores(record)
    assert errs == []


# ---------------------------------------------------------------------------
# _walk_gate_paths — deep nesting
# ---------------------------------------------------------------------------


def test_walk_deeply_nested_all_of() -> None:
    """Validator walks through multiple levels of all_of."""
    errors: list[str] = []
    _walk_gate_paths(
        {
            "all_of": [
                {
                    "all_of": [
                        {"artifact_exists": {"path": "docs/qa/*.md"}},
                    ]
                },
            ]
        },
        errors,
    )
    assert len(errors) == 1
    assert "docs/qa/*.md" in errors[0]


# ---------------------------------------------------------------------------
# _validate_gate_spec — ingest secondary safety net
# ---------------------------------------------------------------------------


def test_ingest_validator_rejects_path_qa() -> None:
    """_validate_gate_spec rejects ``path:`` targeting docs/qa/."""
    leaf = {"artifact_exists": {"path": "docs/qa/*.md"}}
    errs = _validate_gate_spec(leaf)
    assert len(errs) == 1
    assert "store-backed" in errs[0]


def test_ingest_validator_allows_phase_form() -> None:
    """_validate_gate_spec allows ``phase:`` form."""
    leaf = {"artifact_exists": {"phase": "qa", "name": "*.md"}}
    errs = _validate_gate_spec(leaf)
    assert errs == []


def test_ingest_validator_allows_path_src() -> None:
    """_validate_gate_spec allows ``path: src/**``."""
    leaf = {"artifact_exists": {"path": "src/**"}}
    errs = _validate_gate_spec(leaf)
    assert errs == []


# ---------------------------------------------------------------------------
# fnmatch correctness — the glob matching logic
# ---------------------------------------------------------------------------


def test_fnmatch_docs_qa_star_md() -> None:
    """docs/qa/*.md should match docs/qa/**."""
    assert fnmatch.fnmatch("docs/qa/*.md", "docs/qa/**")


def test_fnmatch_docs_ship_star_md() -> None:
    """docs/ship/*.md should match docs/ship/**."""
    assert fnmatch.fnmatch("docs/ship/*.md", "docs/ship/**")


def test_fnmatch_docs_fast_star_md() -> None:
    """docs/fast/*.md should match docs/fast/**."""
    assert fnmatch.fnmatch("docs/fast/*.md", "docs/fast/**")


def test_fnmatch_src_star_no_match() -> None:
    """src/** should NOT match docs/qa/**."""
    assert not fnmatch.fnmatch("src/**", "docs/qa/**")


def test_fnmatch_docs_solutions_no_match() -> None:
    """docs/solutions/*.md should NOT match docs/qa/**."""
    assert not fnmatch.fnmatch("docs/solutions/*.md", "docs/qa/**")
