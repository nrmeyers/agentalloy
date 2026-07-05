"""Shared fixtures for the code-index ingest + API + retrieval tests."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from agentalloy.code_index.store import open_code_index
from agentalloy.config import Settings
from agentalloy.storage.protocols import EMBEDDING_DIM, CodeEdge, CodeSymbol, CodeVectorRow

PY_UTIL = '''"""Utility module."""


def helper(x):
    """Add one to x."""
    return x + 1


def caller():
    """Calls helper."""
    return helper(41)
'''

PY_MAIN = '''"""Main module."""

from pkg.util import caller


def main():
    """Entry point."""
    return caller()
'''

README = """Intro paragraph before any heading.

# Overview

This repo demonstrates the agentalloy code-index ingest pipeline zanzibar.

## Usage

Run main() to add one to 41.
"""


def deterministic_vector(text: str) -> list[float]:
    """Stable, non-zero 768-dim vector derived from the text content."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(digest[i % len(digest)] + 1) / 257.0 for i in range(EMBEDDING_DIM)]


class FakeEmbedClient:
    """Deterministic EmbedClient double; records every embed() call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [deterministic_vector(t) for t in texts]

    def close(self) -> None:
        pass

    @property
    def embedded_texts(self) -> list[str]:
        return [t for batch in self.calls for t in batch]


class GatedEmbedClient(FakeEmbedClient):
    """FakeEmbedClient whose embed() blocks until release() — lets router
    tests observe a job mid-flight deterministically."""

    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()

    def release(self) -> None:
        self._gate.set()

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        assert self._gate.wait(timeout=30.0), "GatedEmbedClient never released"
        return super().embed(model=model, texts=texts)


def write_fixture_repo(root: Path) -> None:
    """A tiny two-module python repo (functions calling each other) + a README."""
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "util.py").write_text(PY_UTIL)
    (root / "pkg" / "main.py").write_text(PY_MAIN)
    (root / "README.md").write_text(README)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    write_fixture_repo(repo)
    return repo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(code_index_data_dir=str(tmp_path / "code-index-data"))


# ---------------------------------------------------------------------------
# Retrieval-test helpers: hand-built vectors + direct store seeding
# ---------------------------------------------------------------------------


class FixedEmbedClient:
    """EmbedClient double that returns one chosen vector for every text."""

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.texts: list[str] = []

    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [list(self.vector) for _ in texts]

    def close(self) -> None:
        pass


def axis_vec(i: int, j: int | None = None) -> list[float]:
    """Normalized 768-dim vector: one-hot at ``i``, or an equal mix of axes
    ``i`` and ``j`` (cosine ~0.707 against either one-hot)."""
    v = [0.0] * EMBEDDING_DIM
    if j is None:
        v[i] = 1.0
    else:
        v[i] = v[j] = 1.0 / math.sqrt(2.0)
    return v


def mix_vec(i: int, j: int, wi: float, wj: float) -> list[float]:
    """Normalized 768-dim vector with weights ``wi``/``wj`` on axes ``i``/``j``
    — cosine against one-hot(i) is exactly wi / sqrt(wi^2 + wj^2)."""
    norm = math.sqrt(wi * wi + wj * wj)
    v = [0.0] * EMBEDDING_DIM
    v[i] = wi / norm
    v[j] = wj / norm
    return v


def make_symbol(
    qn: str,
    *,
    kind: str = "Function",
    file_path: str | None = None,
    docstring: str | None = None,
    source_code: str | None = None,
    start_line: int | None = 1,
    end_line: int | None = 5,
) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=qn,
        kind=kind,
        name=qn.rsplit(".", 1)[-1],
        file_path=file_path if file_path is not None else f"{qn.replace('.', '/')}.py",
        start_line=start_line,
        end_line=end_line,
        docstring=docstring,
        decorators=[],
        is_exported=True,
        is_async=False,
        is_generator=False,
        source_code=source_code
        if source_code is not None
        else f"def {qn.rsplit('.', 1)[-1]}():\n    pass",
    )


def calls_edge(src: str, dst: str) -> CodeEdge:
    return CodeEdge(src=src, dst=dst, kind="CALLS", line_start=3)


def vector_row(
    qn: str, embedding: list[float], *, text: str = "", file_path: str | None = None
) -> CodeVectorRow:
    return CodeVectorRow(
        qualified_name=qn,
        embedding=embedding,
        symbol_type="Function",
        file_path=file_path if file_path is not None else f"{qn.replace('.', '/')}.py",
        start_line=1,
        end_line=5,
        text=text or f"def {qn.rsplit('.', 1)[-1]}(): pass",
        indexed_at=1_700_000_000,
    )


def seed_index(
    settings: Settings,
    slug: str,
    *,
    symbols: Sequence[CodeSymbol] = (),
    edges: Sequence[CodeEdge] = (),
    vectors: Sequence[CodeVectorRow] = (),
    centrality: dict[str, float] | None = None,
    fts: bool = False,
) -> None:
    """Write graph + vector fixtures directly into the per-slug stores."""
    handles = open_code_index(settings, slug, role="service")
    try:
        handles.graph.upsert_symbols(symbols)
        handles.graph.upsert_edges(edges)
        if centrality:
            handles.graph.write_centrality(centrality)
        handles.vectors.upsert(vectors)
        if fts:
            handles.vectors.rebuild_fts_index()
    finally:
        handles.close()
