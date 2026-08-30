"""Shared prune safety rules for the ``/code`` surface.

Single source of truth for the grace constant, the absence-corroboration
rule, and the store-dir ownership check. Both ``repos_router``
(``/code/migrate-layout``) and ``prune_router`` (``/code/prune``) import these,
so the two prune paths cannot drift.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.store import IndexedRepo, code_index_paths

#: A checkout must be observed absent at least this long before its index is
#: deleted. Long enough that an unattended upgrade run during a down mount or a
#: mid-move worktree only ever stamps the clock; short enough that dead rows do
#: not accumulate across a normal upgrade cadence.
PRUNE_GRACE_SECONDS = 7 * 24 * 3600


def absence_is_corroborated(repo_path: Path) -> bool:
    """True when the checkout looks deleted rather than merely unreachable.

    A removed worktree leaves its parent directory in place. A down NFS mount,
    an unplugged drive, or an unmounted volume takes whole ancestors with it —
    so an absent path whose parent is also absent is not evidence of deletion,
    and must never start the prune clock.
    """
    parent = repo_path.parent
    return parent != repo_path and parent.is_dir()


def prunable_store_dir(
    state: CodeIndexState,
    repo: IndexedRepo,
    survivors: list[IndexedRepo],
) -> Path | None:
    """The directory a dead registry row owns outright, or None.

    The registry is keyed ``(slug, repo_path)``, so several checkouts share a
    slug — which is the whole reason the per-checkout layout exists. A dead row
    still in the legacy layout owns ``repos/{slug}/``, the *parent* of every
    live sibling's store, so deleting it by slug alone would wipe indexes that
    are still in use. Only delete a directory no surviving row lives in or
    under.
    """
    own = Path(repo.data_dir)
    if not own.exists():
        return None
    for other in survivors:
        for cand in (
            Path(other.data_dir),
            code_index_paths(state.settings, other.slug, repo_path=other.repo_path).repo_dir,
        ):
            if cand == own or own in cand.parents:
                return None
    return own


def leftover_slug_parent(state: CodeIndexState, own: Path) -> Path | None:
    """The empty slug parent to rmdir after a per-checkout dir is removed.

    ``repos/{slug}/{path_key}`` is removed by prune, leaving ``repos/{slug}/``
    as dead weight when no per-checkout child remains. Only rmdir (never
    rmtree) that parent, and only when it structurally IS a slug dir (a child
    of ``repos/``) — a legacy row's ``data_dir`` is the slug dir itself, whose
    contents are not prune's to judge, and for it ``own``'s parent is
    ``repos/``.
    """
    parent = own.parent
    root = Path(state.settings.code_index_data_dir)
    return parent if parent.parent == root / "repos" else None
