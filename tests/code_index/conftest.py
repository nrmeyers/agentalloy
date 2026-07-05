"""Shared fixtures for the code-index ingest + API tests."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from agentalloy.config import Settings
from agentalloy.storage.protocols import EMBEDDING_DIM

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
