"""Code-index storage — unified OverGraph store + registry/jobs + PageRank.

The v10 facade surface (open_code_index / CodeIndexJob(s)Store / paths /
locks) is the contract the rest of the shell imports; the underlying store
is the v2 unified OverGraph handle (graph + vectors in one DB).
"""

from agentalloy.code_index.store.jobs_store import (
    CodeIndexJob,
    CodeIndexJobsStore,
    IndexedRepo,
)
from agentalloy.code_index.store.open import (
    CodeIndexHandles,
    CodeIndexPaths,
    code_index_paths,
    open_code_index,
    open_jobs,
    remove_repo,
    slug_write_lock,
)
from agentalloy.code_index.store.overgraph_store import (
    CodeSearchHit,
    OverGraphCodeGraphStore,
)
from agentalloy.code_index.store.pagerank import (
    compute_pagerank,
    refresh_centrality,
)

__all__ = [
    "CodeIndexHandles",
    "CodeIndexJob",
    "CodeIndexJobsStore",
    "CodeIndexPaths",
    "CodeSearchHit",
    "IndexedRepo",
    "OverGraphCodeGraphStore",
    "code_index_paths",
    "compute_pagerank",
    "open_code_index",
    "open_jobs",
    "refresh_centrality",
    "remove_repo",
    "slug_write_lock",
]
