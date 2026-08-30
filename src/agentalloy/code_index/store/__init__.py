"""Per-repo storage for the code-index module.

Two engines per indexed repo (under ``{code_index_data_dir}/repos/{slug}/``):

- ``graph.overgraph`` — OverGraph unified symbol graph + vector/BM25 index:
  symbols, edges, centrality, repo_meta, dense vectors, keyword index.
  -> ``overgraph_store.OverGraphCodeGraphStore``
- ``jobs.sqlite``     — one shared WAL SQLite at the data root: index jobs,
  job events, indexed-repos registry.  -> ``jobs_store.CodeIndexJobsStore``

DTOs / Protocols live in ``agentalloy.storage.protocols`` (the canonical home
for storage contracts). Use ``open.open_code_index`` to construct handles.
"""

from __future__ import annotations

from agentalloy.code_index.store.jobs_store import (
    CodeIndexJob,
    CodeIndexJobsStore,
    IndexedRepo,
    repo_path_key,
)
from agentalloy.code_index.store.open import (
    CodeIndexPaths,
    code_index_paths,
    open_code_index,
    open_jobs,
    remove_repo,
    slug_write_lock,
)
from agentalloy.code_index.store.overgraph_store import OverGraphCodeGraphStore
from agentalloy.code_index.store.pagerank import compute_pagerank, refresh_centrality

__all__ = [
    "OverGraphCodeGraphStore",
    "CodeIndexJob",
    "CodeIndexJobsStore",
    "IndexedRepo",
    "repo_path_key",
    "CodeIndexPaths",
    "code_index_paths",
    "open_code_index",
    "open_jobs",
    "remove_repo",
    "slug_write_lock",
    "compute_pagerank",
    "refresh_centrality",
]
