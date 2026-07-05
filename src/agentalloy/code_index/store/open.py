"""Code-index store factory: paths, role-based open, per-slug write locks.

Layout under ``settings.code_index_data_dir``::

    jobs.sqlite                    # shared jobs / events / indexed-repos registry
    repos/{slug}/graph.duck        # DuckDB symbol graph (source of truth)
    repos/{slug}/vectors.lance     # LanceDB vector + BM25 index (derived)
    repos/{slug}/cache/            # engine hash/stat sidecar caches

Locking doctrine (inverse of the skills arrangement): index jobs run INSIDE
the service process — the service IS the code-index writer. DuckDB is
single-writer cross-process, so:

- ``"service"`` / ``"writer"`` open ``graph.duck`` read-write (and migrate);
  concurrent writes within the process are serialized per slug via
  :func:`slug_write_lock` (writers take it around write phases).
- ``"reader"`` opens everything read-only — for one-shot CLI inspection while
  the service is down. Out-of-process readers must prefer the HTTP API when
  the service is up (its RW handle excludes other processes entirely).
- Lance has no exclusive lock (MVCC); the same open works for every role.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore
from agentalloy.code_index.store.vector_store import LanceCodeVectorStore
from agentalloy.config import Settings, get_settings
from agentalloy.storage.protocols import CodeIndexHandles

Role = Literal["service", "writer", "reader"]


@dataclass(frozen=True)
class CodeIndexPaths:
    """Resolved on-disk layout for one repo slug."""

    root: Path
    repo_dir: Path
    graph_path: Path
    vectors_path: Path
    cache_dir: Path
    jobs_path: Path


def code_index_paths(settings: Settings | None, slug: str) -> CodeIndexPaths:
    s = settings or get_settings()
    root = Path(s.code_index_data_dir)
    repo_dir = root / "repos" / slug
    return CodeIndexPaths(
        root=root,
        repo_dir=repo_dir,
        graph_path=repo_dir / "graph.duck",
        vectors_path=repo_dir / "vectors.lance",
        cache_dir=repo_dir / "cache",
        jobs_path=root / "jobs.sqlite",
    )


# -- per-slug in-process write locks -------------------------------------------

_slug_locks: dict[str, threading.Lock] = {}
_slug_locks_guard = threading.Lock()


def slug_write_lock(slug: str) -> threading.Lock:
    """The process-wide write lock for ``slug`` (same object per slug).

    Writers take it around write phases so two in-process index jobs for the
    same repo never interleave graph writes. Deliberately simple: no pooling,
    no cross-process locking (DuckDB's own file lock covers that boundary).
    """
    with _slug_locks_guard:
        return _slug_locks.setdefault(slug, threading.Lock())


# -- open / remove ---------------------------------------------------------------


def open_code_index(
    settings: Settings | None, slug: str, *, role: Role = "service"
) -> CodeIndexHandles:
    """Open the per-repo graph + vector stores with access modes for ``role``.

    ``service`` / ``writer`` open the graph read-write and ensure the schema;
    ``reader`` opens it read-only (the graph file must already exist).
    """
    paths = code_index_paths(settings, slug)
    read_only = role == "reader"
    if not read_only:
        paths.repo_dir.mkdir(parents=True, exist_ok=True)
        paths.cache_dir.mkdir(parents=True, exist_ok=True)
    graph = DuckDBCodeGraphStore(paths.graph_path, read_only=read_only)
    if not read_only:
        graph.migrate()
    vectors = LanceCodeVectorStore(paths.vectors_path)
    return CodeIndexHandles(slug=slug, graph=graph, vectors=vectors)


def open_jobs(settings: Settings | None = None) -> CodeIndexJobsStore:
    """Open (creating if needed) the shared jobs store at the data root."""
    s = settings or get_settings()
    root = Path(s.code_index_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return CodeIndexJobsStore(root / "jobs.sqlite")


def remove_repo(settings: Settings | None, slug: str) -> bool:
    """Delete a slug's entire store directory (unwire). True iff it existed.

    Callers must close any open handles for the slug first (and hold
    :func:`slug_write_lock` if the service is live).
    """
    paths = code_index_paths(settings, slug)
    if not paths.repo_dir.exists():
        return False
    shutil.rmtree(paths.repo_dir)
    return True
