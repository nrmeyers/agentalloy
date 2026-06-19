"""Schema validation tests for the compose contract (NXS-765).

AC-1: request schema accepts task, phase, domain_tags?, k?, trace_id?
AC-2..4: response shapes exist for composed, empty, and 503 stages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from agentalloy.api.compose_models import (
    ComposedResult,
    ComposeRequest,
    EmptyResult,
    ErrorAvailable,
    ErrorResponse,
    LatencyBreakdown,
)

# ---------------------------------------------------------------------------
# ComposeRequest.resolved_contract_tags
# ---------------------------------------------------------------------------


def _write_contract(path: Path, phase: str = "build", domain_tags: list[str] | None = None) -> Path:
    fm: dict[str, Any] = {
        "phase": phase,
        "task_slug": "test-task",
        "domain_tags": domain_tags or ["NestJS", "JWT"],
        "scope": {"touches": [], "avoids": []},
        "success_criteria": [],
        "related_contracts": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.dump(fm)}---\n\nTest task.\n")
    return path


def test_resolved_contract_tags_from_explicit(tmp_path: Path):
    from agentalloy.api.compose_models import ComposeRequest

    req = ComposeRequest(task="do thing", phase="build", contract_tags=["A", "B"])
    assert req.resolved_contract_tags == ["A", "B"]


def test_resolved_contract_tags_from_path(tmp_path: Path):
    from agentalloy.api.compose_models import ComposeRequest

    # Must live under a project's .agentalloy/contracts/<phase>/ directory
    # to pass the path-containment guard.
    contract_dir = tmp_path / ".agentalloy" / "contracts" / "build"
    f = _write_contract(contract_dir / "c.md", domain_tags=["NestJS", "JWT"])
    req = ComposeRequest(task="do thing", phase="build", contract_path=str(f))
    assert req.resolved_contract_tags == ["NestJS", "JWT"]


def test_resolved_contract_tags_rejects_unsafe_path(tmp_path: Path):
    """Paths outside any .agentalloy/contracts/ tree are silently rejected (returns None)."""
    from agentalloy.api.compose_models import ComposeRequest

    f = _write_contract(tmp_path / "loose-contract.md", domain_tags=["X"])
    req = ComposeRequest(task="do thing", phase="build", contract_path=str(f))
    assert req.resolved_contract_tags is None


def test_resolved_contract_tags_none_when_not_set():
    from agentalloy.api.compose_models import ComposeRequest

    req = ComposeRequest(task="do thing", phase="build")
    assert req.resolved_contract_tags is None


# -------- AC-1: request --------


def test_request_minimal_valid() -> None:
    req = ComposeRequest(task="build auth", phase="design")
    assert req.task == "build auth"
    assert req.phase == "design"
    assert req.domain_tags is None
    # k now optional — None means "use phase default" (resolved server-side).
    assert req.k is None
    assert req.resolved_k() == 4  # design phase default
    assert req.trace_id is None


def test_request_resolves_phase_defaults() -> None:
    """Verify the phase-driven k defaults from POC §15.7."""
    assert ComposeRequest(task="t", phase="build").resolved_k() == 2
    assert ComposeRequest(task="t", phase="ship").resolved_k() == 2
    assert ComposeRequest(task="t", phase="qa").resolved_k() == 4
    assert ComposeRequest(task="t", phase="design").resolved_k() == 4
    assert ComposeRequest(task="t", phase="intake").resolved_k() == 4
    # explicit k overrides phase default
    assert ComposeRequest(task="t", phase="build", k=8).resolved_k() == 8


def test_request_all_fields() -> None:
    req = ComposeRequest(
        task="t",
        phase="build",
        domain_tags=["python", "fastapi"],
        k=25,
        trace_id="corr-1",
    )
    assert req.domain_tags == ["python", "fastapi"]
    assert req.k == 25
    assert req.trace_id == "corr-1"


@pytest.mark.parametrize("phase", ["intake", "spec", "design", "qa", "build", "ship"])
def test_request_accepts_every_phase(phase: str) -> None:
    ComposeRequest(task="t", phase=phase)  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", ["ops", "meta", "governance"])
def test_request_rejects_retired_phases(phase: str) -> None:
    """ops/meta/governance were retired from the Phase Literal in Stage 1b."""
    with pytest.raises(ValidationError):
        ComposeRequest(task="t", phase=phase)  # type: ignore[arg-type]


def test_request_rejects_unknown_phase() -> None:
    with pytest.raises(ValidationError):
        ComposeRequest(task="t", phase="invalid")  # type: ignore[arg-type]


def test_request_rejects_empty_task() -> None:
    with pytest.raises(ValidationError):
        ComposeRequest(task="", phase="design")


def test_request_rejects_k_out_of_range() -> None:
    with pytest.raises(ValidationError):
        ComposeRequest(task="t", phase="design", k=0)
    with pytest.raises(ValidationError):
        ComposeRequest(task="t", phase="design", k=51)


# -------- AC-2: composed response --------


def test_composed_result_round_trip() -> None:
    c = ComposedResult(
        task="t",
        phase="design",
        output="assembled",
        domain_fragments=["f-1", "f-2"],
        source_skills=["s-a"],
        system_fragments=["sys-1"],
        system_skills_applied=True,
        assembly_tier=2,
        latency_ms=LatencyBreakdown(retrieval_ms=10, assembly_ms=500, total_ms=510),
    )
    data = c.model_dump()
    assert data["status"] == "ok"
    assert data["result_type"] == "composed"
    assert data["latency_ms"]["total_ms"] == 510


# -------- AC-3: empty response --------


def test_empty_result_has_fixed_discriminator_and_output() -> None:
    e = EmptyResult(
        task="t",
        phase="design",
        system_fragments=[],
        system_skills_applied=False,
    )
    data = e.model_dump()
    assert data["result_type"] == "empty"
    assert data["output"] == ""
    assert data["reason"] == "no_domain_fragments_matched"
    assert data["domain_fragments"] == []
    assert data["source_skills"] == []


# -------- AC-4: 503 error response --------


@pytest.mark.parametrize("stage", ["retrieval", "assembly"])
def test_error_response_accepts_both_stages(stage: str) -> None:
    e = ErrorResponse(
        stage=stage,  # type: ignore[arg-type]
        code="dependency_unavailable",
        message="boom",
    )
    assert e.stage == stage


@pytest.mark.parametrize(
    "code",
    [
        "dependency_unavailable",
        "store_unavailable",
        "embedding_failed",
        "embedding_model_unavailable",
    ],
)
def test_error_response_accepts_every_code(code: str) -> None:
    ErrorResponse(
        stage="retrieval",
        code=code,  # type: ignore[arg-type]
        message="m",
    )


def test_error_response_rejects_unknown_code() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            stage="retrieval",
            code="invented",  # type: ignore[arg-type]
            message="m",
        )


def test_error_response_available_can_hold_partial_state() -> None:
    e = ErrorResponse(
        stage="retrieval",
        code="embedding_model_unavailable",
        message="embed model not loaded",
        available=ErrorAvailable(domain_fragments=["f-1"], system_fragments=["sys-1"]),
    )
    assert e.available is not None
    assert e.available.domain_fragments == ["f-1"]
