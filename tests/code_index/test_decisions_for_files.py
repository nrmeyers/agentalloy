"""decisions_for_files — file-scoped GOVERNS join (slice-2 build 01)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.storage.protocols import CodeEdge, CodeSymbol


def sym(
    qn: str, *, kind: str = "Function", file_path: str | None = None, **kw: object
) -> CodeSymbol:
    defaults: dict[str, object] = {
        "name": qn.rsplit(".", 1)[-1].rsplit("::", 1)[-1],
        "start_line": 1,
        "end_line": 5,
        "docstring": None,
        "decorators": [],
        "is_exported": None,
        "is_async": False,
        "is_generator": False,
        "source_code": None,
    }
    defaults.update(kw)
    return CodeSymbol(qualified_name=qn, kind=kind, file_path=file_path, **defaults)  # type: ignore[arg-type]


def governs(src: str, dst: str, *, doc: str) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="GOVERNS", file_path=doc)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBCodeGraphStore]:
    s = DuckDBCodeGraphStore(tmp_path / "graph.duck")
    s.migrate()
    yield s
    s.close()


def _seed(store: DuckDBCodeGraphStore) -> None:
    store.upsert_symbols(
        [
            sym("pkg.a.foo", file_path="pkg/a.py"),
            sym("pkg.a.bar", file_path="pkg/a.py"),
            sym("pkg.b.baz", file_path="pkg/b.py"),
            sym("pkg.c.orphan", file_path="pkg/c.py"),  # ungoverned
            sym(
                "docs/design/x/approach.md::d1",
                kind="MarkdownDoc",
                name="Decision 1",
                file_path="docs/design/x/approach.md",
                start_line=7,
                source_code="Chose `pkg.a.foo` and `pkg.b.baz`.",
            ),
        ]
    )
    store.upsert_edges(
        [
            governs("docs/design/x/approach.md::d1", "pkg.a.foo", doc="docs/design/x/approach.md"),
            governs("docs/design/x/approach.md::d1", "pkg.b.baz", doc="docs/design/x/approach.md"),
        ]
    )


def test_decisions_for_files_returns_governing_decisions(store: DuckDBCodeGraphStore) -> None:
    _seed(store)
    got = store.decisions_for_files(["pkg/a.py"])
    assert [d.qualified_name for d in got] == ["docs/design/x/approach.md::d1"]
    assert got[0].heading == "Decision 1" and got[0].start_line == 7


def test_decision_governing_multiple_touched_files_appears_once(
    store: DuckDBCodeGraphStore,
) -> None:
    _seed(store)
    # d1 governs symbols in BOTH pkg/a.py and pkg/b.py; querying both must not dup it
    got = store.decisions_for_files(["pkg/a.py", "pkg/b.py"])
    assert [d.qualified_name for d in got] == ["docs/design/x/approach.md::d1"]


def test_ungoverned_and_empty(store: DuckDBCodeGraphStore) -> None:
    _seed(store)
    assert store.decisions_for_files(["pkg/c.py"]) == []  # orphan file, no GOVERNS
    assert store.decisions_for_files(["pkg/missing.py"]) == []
    assert store.decisions_for_files([]) == []
