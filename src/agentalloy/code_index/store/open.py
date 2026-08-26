"""Code-index store factory: paths, role-based open, per-slug write locks.

Layout under ``settings.code_index_data_dir``::

    jobs.sqlite                    # shared jobs / events / indexed-repos registry
    repos/{slug}/{path_key}/graph.duck   # DuckDB symbol graph (source of truth)
    repos/{slug}/{path_key}/vectors.lance  # LanceDB vector + BM25 index (derived)
    repos/{slug}/{path_key}/cache/        # engine hash/stat sidecar caches

``{path_key}`` is a deterministic 8-hex SHA-256 prefix of the resolved
``repo_path``, so multiple checkouts of the same remote get separate indexes.
See :func:`agentalloy.code_index.store.jobs_store.repo_path_key`.

Locking doctrine (inverse of the skills arrangement): index jobs run INSIDE
the service process — the service IS the code-index writer. DuckDB is
single-writer cross-process, so:

- ``"service"`` / ``"writer"`` open ``graph.duck`` read-write (and migrate);
  concurrent writes within the process are serialized per (slug, path_key) via
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
from agentalloy.code_index.store.jobs_store import CodeIndexJobsStore, repo_path_key
from agentalloy.code_index.store.nebula_graph_store import NebulaGraphCodeGraphStore
from agentalloy.code_index.store.orient_graph_store import OrientDBCodeGraphStore
from agentalloy.code_index.store.vector_store import LanceCodeVectorStore
from agentalloy.config import Settings, get_settings
from agentalloy.storage.protocols import CodeIndexHandles

Role = Literal["service", "writer", "reader"]


@dataclass(frozen=True)
class CodeIndexPaths:
    """Resolved on-disk layout for one repo checkout."""

    root: Path
    repo_dir: Path
    graph_path: Path
    vectors_path: Path
    cache_dir: Path
    jobs_path: Path
    repo_key: str  # deterministic path hash for per-checkout isolation


def code_index_paths(
    settings: Settings | None,
    slug: str,
    *,
    repo_path: str | None = None,
) -> CodeIndexPaths:
    """Resolve on-disk paths for ``slug`` (+ optional ``repo_path``).

    When ``repo_path`` is provided, the data directory is scoped to that
    specific checkout (``repos/{slug}/{path_key}/``). Without it, falls back
    to the legacy ``repos/{slug}/`` layout for backward compatibility.
    """
    s = settings or get_settings()
    root = Path(s.code_index_data_dir)
    if repo_path is not None:
        key = repo_path_key(repo_path)
        repo_dir = root / "repos" / slug / key
    else:
        key = "default"
        repo_dir = root / "repos" / slug
    return CodeIndexPaths(
        root=root,
        repo_dir=repo_dir,
        graph_path=repo_dir / "graph.duck",
        vectors_path=repo_dir / "vectors.lance",
        cache_dir=repo_dir / "cache",
        jobs_path=root / "jobs.sqlite",
        repo_key=key,
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
    settings: Settings | None,
    slug: str,
    *,
    role: Role = "service",
    repo_path: str | None = None,
) -> CodeIndexHandles:
    """Open the per-repo graph + vector stores with access modes for ``role``.

    ``service`` / ``writer`` open the graph read-write and ensure the schema;
    ``reader`` opens it read-only (the graph file must already exist).

    When ``repo_path`` is provided, the data directory is scoped to that
    specific checkout, so multiple checkouts of the same remote get separate
    indexes.

    The graph backend is selected by ``settings.code_index_graph_backend``:
    ``"duckdb"`` (default) uses a local DuckDB file; ``"orientdb"`` connects
    to an OrientDB server via REST API; ``"nebulagraph"`` connects to a
    NebulaGraph server via binary protocol for native graph performance.
    """
    s = settings or get_settings()
    paths = code_index_paths(settings, slug, repo_path=repo_path)
    read_only = role == "reader"
    if not read_only:
        paths.repo_dir.mkdir(parents=True, exist_ok=True)
        paths.cache_dir.mkdir(parents=True, exist_ok=True)

    backend = s.code_index_graph_backend
    if backend == "nebulagraph":
        # NebulaGraph: per-slug space on the shared server. The space name
        # is derived from the slug (sanitised for NebulaGraph naming rules).
        space_name = slug.replace("-", "_").replace(".", "_")
        graph = NebulaGraphCodeGraphStore(
            space=space_name,
            host=s.nebulagraph_host,
            port=s.nebulagraph_port,
            username=s.nebulagraph_username,
            password=s.nebulagraph_password,
        )
        if not read_only:
            graph.migrate()
    elif backend == "orientdb":
        # OrientDB: per-slug database on the shared server. The database name
        # is derived from the slug (sanitised for OrientDB naming rules).
        db_name = slug.replace("-", "_").replace(".", "_")
        graph = OrientDBCodeGraphStore(
            database=db_name,
            base_url=s.orientdb_url,
            username=s.orientdb_username,
            password=s.orientdb_password,
        )
        if not read_only:
            graph.migrate()
    else:
        # DuckDB: local file per repo checkout.
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


def remove_repo(settings: Settings | None, slug: str, *, repo_path: str | None = None) -> bool:
    """Delete a slug's store directory (unwire). True iff it existed.

    When ``repo_path`` is provided, removes only that checkout's data
    directory. Otherwise removes all data under ``repos/{slug}/``.

    Callers must close any open handles for the slug first (and hold
    :func:`slug_write_lock` if the service is live).
    """
    paths = code_index_paths(settings, slug, repo_path=repo_path)
    if not paths.repo_dir.exists():
        return False
    shutil.rmtree(paths.repo_dir)
    return True
