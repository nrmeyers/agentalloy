"""Shared index opener for the codegraph store.

One OverGraph DB holds ALL registered repos (``repo`` property on nodes and
edges distinguishes them); the parse caches and the tantivy FTS side index
live in sibling directories under ``{index_dir}/codegraph/``:

    codegraph/
      codegraph.overgraph/   shared graph + vector store
      fts/                   tantivy BM25 side index (rebuilt on swap)
      parse-cache/{repo}/    tree-sitter stat/hash sidecars (per repo, so
                             relative-path keys can never collide across repos)
"""

from __future__ import annotations

import re
from pathlib import Path

from .protocols import EMBEDDING_DIM
from .store.overgraph_store import OverGraphCodeGraphStore

_SAFE_REPO_RE = re.compile(r"[^A-Za-z0-9._-]")


def index_root(index_dir: str | Path) -> Path:
    """``{index_dir}/codegraph`` — parent of every index artifact."""
    return Path(index_dir).expanduser().resolve() / "codegraph"


def graph_db_path(index_dir: str | Path) -> Path:
    return index_root(index_dir) / "codegraph.overgraph"


def parse_cache_dir(index_dir: str | Path, repo_name: str) -> Path:
    """Per-repo tree-sitter stat/hash cache dir (sanitized repo name)."""
    safe = _SAFE_REPO_RE.sub("_", repo_name) or "repo"
    return index_root(index_dir) / "parse-cache" / safe


def fts_dir(index_dir: str | Path) -> Path:
    """tantivy BM25 side index location."""
    return index_root(index_dir) / "fts"


def open_codegraph(index_dir: str | Path) -> OverGraphCodeGraphStore:
    """Open (or create) the shared codegraph store.

    The dimension is bound at DB creation (768); a model swap to a different
    vector dimension requires a fresh index directory, not a migration.
    """
    db_path = graph_db_path(index_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return OverGraphCodeGraphStore(db_path, vector_dimension=EMBEDDING_DIM)
