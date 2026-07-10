"""Slice-2 build 04 — the decision push at the `_compose_block` seam.

Build 03's unit tests pin `_compose_decision_push` in isolation; these drive the
real `_compose_block` to prove the *integration* guarantees the full-feature merge
rests on:

- **Cadence** (TC1/TC1b) — the push rides the Tier-2 work-item channel, so it
  fires only on a cursor-entry turn (``announce_cursor=True``) and is absent on a
  mid-work-item turn (``announce_cursor=False``), same as the domain leg.
- **Strict additivity** (AC 6/9) — when the code index is unavailable the composed
  ``.text`` is byte-identical to the pre-Knowledge composition; the decision path
  can only *add* a block, never perturb the existing tiers.
- **Read-only boundary** (AC 9, TC8) — the store is opened ``role="reader"``; the
  push never writes the corpus.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from agentalloy.api import code_index_gate, proxy_apply
from agentalloy.api.compose_models import ComposedResult, EmptyResult, LatencyBreakdown
from agentalloy.api.proxy_apply import _compose_block
from agentalloy.api.proxy_signal import SignalResult
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.orchestration.compose import ComposeOrchestrator
from agentalloy.storage.protocols import CodeEdge, CodeSymbol


class _FakeOrch(ComposeOrchestrator):
    """Returns a fixed domain-leg result; never touches retrieval/storage."""

    def __init__(self, output: str) -> None:  # noqa: D107 — deliberately no super().__init__
        self._output = output

    async def compose(self, req: Any, **_kw: object) -> Any:
        if getattr(req, "legs", None) == "system":  # Tier 1 — unused here
            return EmptyResult(task="t", phase="build", system_fragments=[])
        return ComposedResult(
            task=getattr(req, "task", "t"),
            phase=getattr(req, "phase", "design"),
            output=self._output,
            domain_fragments=["frag"],
            source_skills=["skill"],
            system_fragments=[],
            system_skills_applied=False,
            assembly_tier=1,
            latency_ms=LatencyBreakdown(retrieval_ms=1, assembly_ms=1, total_ms=2),
        )


def _write_contract(path: Path, touches: list[str]) -> Path:
    fm = {
        "phase": "design",
        "task_slug": "t",
        "domain_tags": [],
        "scope": {"touches": touches, "avoids": []},
        "success_criteria": [],
        "related_contracts": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.dump(fm)}---\n\nbody\n", encoding="utf-8")
    return path


def _seed(tmp_path: Path) -> DuckDBCodeGraphStore:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    s.upsert_symbols(
        [
            CodeSymbol(
                qualified_name="pkg.a.foo",
                kind="Function",
                name="foo",
                file_path="pkg/a.py",
                start_line=1,
                end_line=5,
                docstring=None,
                decorators=[],
                is_exported=None,
                is_async=False,
                is_generator=False,
                source_code=None,
            ),
            CodeSymbol(
                qualified_name="docs/design/x/approach.md::why",
                kind="MarkdownDoc",
                name="Why foo",
                file_path="docs/design/x/approach.md",
                start_line=3,
                end_line=9,
                docstring=None,
                decorators=[],
                is_exported=None,
                is_async=False,
                is_generator=False,
                source_code="Chose `pkg.a.foo`.",
            ),
        ]
    )
    s.upsert_edges(
        [
            CodeEdge(
                src="docs/design/x/approach.md::why",
                dst="pkg.a.foo",
                kind="GOVERNS",
                file_path="docs/design/x/approach.md",
            )
        ]
    )
    return s


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    store: DuckDBCodeGraphStore | None,
    *,
    available: bool,
) -> dict[str, Any]:
    seen: dict[str, Any] = {"open": 0, "roles": []}

    def _open(settings: object, slug: str, *, role: str) -> object:
        seen["open"] += 1
        seen["roles"].append(role)
        return SimpleNamespace(graph=store, close=lambda: None)

    monkeypatch.setattr(
        code_index_gate, "code_index_available", lambda repo, settings=None: available
    )
    monkeypatch.setattr("agentalloy.code_index.store.open_code_index", _open)
    monkeypatch.setattr("agentalloy.code_index.slug.repo_slug", lambda p: "slug")
    return seen


def _signal(tmp_path: Path, *, announce_cursor: bool) -> SignalResult:
    contract = _write_contract(tmp_path / "c.md", ["pkg/a.py"])
    return SignalResult(
        should_compose=True,
        announce=False,  # skip Tier 1; isolate the work-item channel
        announce_cursor=announce_cursor,
        current_contract=str(contract),
        phase="design",
        task="do it",
        repo=str(tmp_path),
    )


async def test_push_present_on_cursor_entry_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seed(tmp_path)
    seen = _wire(monkeypatch, store, available=True)
    block = await _compose_block(_signal(tmp_path, announce_cursor=True), _FakeOrch("DOMAIN"))
    assert "DOMAIN" in block.text  # domain leg still composes
    assert "# Decisions governing this work" in block.text  # + the push, additively
    assert "Why foo" in block.text
    assert seen["roles"] == ["reader"]  # AC9: opened read-only, no corpus write
    store.close()


async def test_no_push_on_non_entry_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TC1b — a mid-work-item turn (cursor unchanged) skips the whole Tier-2
    channel, so neither the domain leg nor the decision push fire."""
    store = _seed(tmp_path)
    seen = _wire(monkeypatch, store, available=True)
    block = await _compose_block(_signal(tmp_path, announce_cursor=False), _FakeOrch("DOMAIN"))
    assert "# Decisions governing this work" not in block.text
    assert seen["open"] == 0  # index never opened off a cursor-entry turn
    store.close()


async def test_byte_identical_when_index_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC 6/9 additivity: an unavailable index composes the exact same text as a
    run with the decision push stubbed out entirely — the path only ever adds."""
    seen = _wire(monkeypatch, None, available=False)
    with_gate = await _compose_block(_signal(tmp_path, announce_cursor=True), _FakeOrch("DOMAIN"))

    # Baseline: decision path forced to a no-op, everything else identical.
    monkeypatch.setattr(proxy_apply, "_compose_decision_push", lambda *a, **k: "")
    baseline = await _compose_block(_signal(tmp_path, announce_cursor=True), _FakeOrch("DOMAIN"))

    assert with_gate.text == baseline.text
    assert "# Decisions governing this work" not in with_gate.text
    assert seen["open"] == 0  # unavailable -> never opened
