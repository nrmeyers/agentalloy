"""_compose_decision_push — the slice-2 fire-gate + store-open wiring (build 03).

Asserts the push fires at design/build when available, and is a strict no-op
(no code-index open) when the phase is wrong, scope is empty, or the index is
unavailable — the additive/graceful-degrade guarantee for the proxy hot path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentalloy.api import code_index_gate
from agentalloy.api.proxy_apply import _compose_decision_push
from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.contracts import Contract, ContractScope
from agentalloy.storage.protocols import CodeEdge, CodeSymbol


def _contract(touches: list[str], phase: str = "design") -> Contract:
    return Contract(
        path=Path("c.md"),
        phase=phase,
        task_slug="t",
        domain_tags=[],
        scope=ContractScope(touches=touches, avoids=[]),
        success_criteria=[],
        related_contracts=[],
        created_at=None,
        body="",
        route="full",
    )


def _seeded_store(tmp_path: Path) -> DuckDBCodeGraphStore:
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
    monkeypatch: pytest.MonkeyPatch, store: DuckDBCodeGraphStore | None, available: bool
) -> dict[str, int]:
    calls = {"open": 0}

    def _open(settings: object, slug: str, *, role: str) -> object:
        calls["open"] += 1
        return SimpleNamespace(graph=store, close=lambda: None)

    monkeypatch.setattr(
        code_index_gate, "code_index_available", lambda repo, settings=None: available
    )
    monkeypatch.setattr("agentalloy.code_index.store.open_code_index", _open)
    monkeypatch.setattr("agentalloy.code_index.slug.repo_slug", lambda p: "slug")
    return calls


def test_push_fires_at_design_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seeded_store(tmp_path)
    _wire(monkeypatch, store, available=True)
    signal = SimpleNamespace(repo="/repo")
    out = _compose_decision_push(signal, "design", _contract(["pkg/a.py"]), "")
    assert "# Decisions governing this work" in out and "Why foo" in out
    store.close()


def test_no_push_wrong_phase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, _seeded_store(tmp_path), available=True)
    signal = SimpleNamespace(repo="/repo")
    assert _compose_decision_push(signal, "spec", _contract(["pkg/a.py"]), "") == ""
    assert calls["open"] == 0  # never opened the index off design/build


def test_no_push_empty_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _wire(monkeypatch, _seeded_store(tmp_path), available=True)
    signal = SimpleNamespace(repo="/repo")
    assert _compose_decision_push(signal, "design", _contract([]), "") == ""
    assert calls["open"] == 0


def test_no_push_when_unavailable_and_index_never_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _wire(monkeypatch, None, available=False)
    signal = SimpleNamespace(repo="/repo")
    # graceful degrade: unavailable -> "" AND no code-index open attempted
    assert _compose_decision_push(signal, "design", _contract(["pkg/a.py"]), "") == ""
    assert calls["open"] == 0
