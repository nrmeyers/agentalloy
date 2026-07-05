"""Per-repo storage for the code-index module.

Three engines per indexed repo (under ``{code_index_data_dir}/repos/{slug}/``):

- ``graph.duck``    — DuckDB symbol graph (source of truth): symbols, edges,
  centrality, repo_meta.  -> ``graph_store.DuckDBCodeGraphStore``
- ``vectors.lance`` — LanceDB vector ANN + native BM25 over symbols (derived).
  -> ``vector_store.LanceCodeVectorStore``
- ``jobs.sqlite``   — one shared WAL SQLite at the data root: index jobs,
  job events, indexed-repos registry.  -> ``jobs_store.CodeIndexJobsStore``

DTOs / Protocols live in ``agentalloy.storage.protocols`` (the canonical home
for storage contracts). Use ``open.open_code_index`` to construct handles.
"""

from __future__ import annotations

from agentalloy.code_index.store.graph_store import DuckDBCodeGraphStore
from agentalloy.code_index.store.jobs_store import (
    CodeIndexJob,
    CodeIndexJobsStore,
    IndexedRepo,
)
from agentalloy.code_index.store.open import (
    CodeIndexPaths,
    code_index_paths,
    open_code_index,
    open_jobs,
    remove_repo,
    slug_write_lock,
)
from agentalloy.code_index.store.pagerank import compute_pagerank, refresh_centrality
from agentalloy.code_index.store.vector_store import LanceCodeVectorStore

__all__ = [
    "DuckDBCodeGraphStore",
    "LanceCodeVectorStore",
    "CodeIndexJob",
    "CodeIndexJobsStore",
    "IndexedRepo",
    "CodeIndexPaths",
    "code_index_paths",
    "open_code_index",
    "open_jobs",
    "remove_repo",
    "slug_write_lock",
    "compute_pagerank",
    "refresh_centrality",
]
