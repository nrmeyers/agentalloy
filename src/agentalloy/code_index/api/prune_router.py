"""Repo-prune endpoint (``POST /code/prune``).

The explicit, user-driven orphan path: delete a registry row (and its store
dir, when no surviving row owns it) for a checkout that is already gone from
disk. Single-target mode uses HTTP status codes (404/400/409/200); batch mode
(``slug`` null) always returns 200 with per-row verdicts, reusing the
migrate-layout vocabulary. Shares its safety rules with migrate-layout via
``prune_rules`` (spec AC-8).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agentalloy.code_index.api.models import PruneEntry, PruneRequest, PruneView
from agentalloy.code_index.api.prune_rules import (
    PRUNE_GRACE_SECONDS,
    absence_is_corroborated,
    leftover_slug_parent,
    prunable_store_dir,
)
from agentalloy.code_index.api.state import CodeIndexState, get_code_index_state
from agentalloy.code_index.store import IndexedRepo

router = APIRouter()


def _ripe(repo: IndexedRepo, now: int) -> bool:
    """True once the absence stamp is older than the grace period."""
    return repo.missing_since is not None and now - repo.missing_since >= PRUNE_GRACE_SECONDS


def _survivors(all_rows: list[IndexedRepo], repo: IndexedRepo) -> list[IndexedRepo]:
    """Live sibling rows for the ownership check.

    A sibling is any OTHER (slug, repo_path) whose checkout is still on disk.
    Rows pruned earlier in this pass are dead-path rows too, so the is_dir()
    filter already excludes them.
    """
    return [
        r
        for r in all_rows
        if (r.slug, r.repo_path) != (repo.slug, repo.repo_path) and Path(r.repo_path).is_dir()
    ]


async def _delete_one(
    state: CodeIndexState,
    repo: IndexedRepo,
    all_rows: list[IndexedRepo],
) -> str | None:
    """Delete one dead row's store dir (if it owns it) + the registry row.

    Scoped to this exact (slug, repo_path): a sibling checkout may share the
    slug and still be live. Returns the removed directory, or None when the
    row did not own a deletable dir.
    """
    survivors = _survivors(all_rows, repo)
    if state.watch is not None and not any(r.slug == repo.slug for r in survivors):
        state.watch.stop(repo.slug)
    target = prunable_store_dir(state, repo, survivors)
    removed: str | None = None
    if target is not None:
        await asyncio.to_thread(shutil.rmtree, target, True)
        removed = str(target)
        parent = leftover_slug_parent(state, target)
        if parent is not None:
            # Best effort: the slug dir is usually left empty, but a sibling
            # checkout may still live under it — rmdir fails then, and so be it.
            with contextlib.suppress(OSError):
                await asyncio.to_thread(parent.rmdir)
    state.jobs.delete_repo(repo.slug, repo_path=repo.repo_path)
    return removed


def _would_remove_store_dir(
    state: CodeIndexState,
    repo: IndexedRepo,
    all_rows: list[IndexedRepo],
) -> str | None:
    """The store dir a real prune of this row would delete, or None.

    Dry-run's report: the same ownership check as ``_delete_one`` without
    touching anything. None means the dir is shared with a live sibling (or
    already gone) and would be preserved.
    """
    target = prunable_store_dir(state, repo, _survivors(all_rows, repo))
    return str(target) if target is not None else None


def _view(req: PruneRequest, entries: list[PruneEntry]) -> PruneView:
    return PruneView(
        dry_run=req.dry_run,
        forced=req.force,
        total=len(entries),
        pruned=sum(1 for e in entries if e.verdict == "pruned"),
        stamped=sum(1 for e in entries if e.verdict == "stamped"),
        skipped=sum(1 for e in entries if e.verdict not in ("pruned", "stamped")),
        entries=entries,
    )


@router.post(
    "/prune",
    response_model=PruneView,
    summary="Prune the index of a repo whose checkout is gone (or every such orphan)",
    responses={
        400: {"description": "The repo_path still exists — use `agentalloy code remove`"},
        404: {"description": "No such registry row"},
        409: {"description": "Active job, uncorroborated absence, or grace not elapsed"},
    },
)
async def prune(
    req: PruneRequest,
    state: CodeIndexState = Depends(get_code_index_state),
) -> PruneView:
    """Prune orphaned registry rows.

    - ``slug`` set: single-target. HTTP status codes — 404 unknown row, 409
      active job / uncorroborated absence / grace-not-elapsed, 400 checkout
      still present, 200 stamped-or-pruned.
    - ``slug`` null: batch. Always 200 with one verdict per registry row
      (``live | unreachable | stamped | waiting | pruned | busy``), reusing
      the migrate-layout classification.
    """
    rows = state.jobs.list_repos()
    now = int(time.time())
    if req.slug is None:
        return await _batch(state, rows, req, now)
    return await _single(state, rows, req, now)


async def _single(
    state: CodeIndexState,
    rows: list[IndexedRepo],
    req: PruneRequest,
    now: int,
) -> PruneView:
    assert req.slug is not None  # guarded by the dispatcher
    repo = (
        state.jobs.get_repo(req.slug, repo_path=req.repo_path)
        if req.repo_path is not None
        else state.jobs.get_repo(req.slug)  # unambiguous or None
    )
    if repo is None:
        raise HTTPException(
            status_code=404,
            detail=f"no such registry row: {req.slug}"
            + (f" at {req.repo_path}" if req.repo_path else ""),
        )
    if state.jobs.find_active(repo.slug) is not None:
        raise HTTPException(status_code=409, detail=f"an index job for {repo.slug!r} is active")
    repo_path = Path(repo.repo_path)
    if repo_path.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path still exists ({repo_path}); use `agentalloy code remove`",
        )
    if not absence_is_corroborated(repo_path):
        raise HTTPException(
            status_code=409,
            detail=(
                "absence uncorroborated (parent directory also gone) — "
                "probable down mount, not a deletion"
            ),
        )

    # Grace gate + first-sighting stamp: absence has to persist across runs.
    if repo.missing_since is None:
        if not req.dry_run:
            state.jobs.set_missing_since(repo.slug, repo.repo_path, now)
        return _view(
            req,
            [
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="stamped",
                    detail="first sighting; grace clock started",
                )
            ],
        )
    if not req.force and not _ripe(repo, now):
        remaining_days = (PRUNE_GRACE_SECONDS - (now - repo.missing_since)) / 86400
        raise HTTPException(
            status_code=409,
            detail=(
                f"grace not elapsed; ~{remaining_days:.1f} days remaining (use force to bypass)"
            ),
        )

    if req.dry_run:
        return _view(
            req,
            [
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="pruned",
                    store_dir=_would_remove_store_dir(state, repo, rows),
                    detail="dry run: would prune",
                )
            ],
        )
    removed_dir = await _delete_one(state, repo, rows)
    return _view(
        req,
        [
            PruneEntry(
                slug=repo.slug,
                repo_path=repo.repo_path,
                verdict="pruned",
                row_deleted=True,
                store_dir=removed_dir,
                store_dir_removed=removed_dir is not None,
            )
        ],
    )


async def _batch(
    state: CodeIndexState,
    rows: list[IndexedRepo],
    req: PruneRequest,
    now: int,
) -> PruneView:
    entries: list[PruneEntry] = []
    for repo in rows:
        if state.jobs.find_active(repo.slug) is not None:
            entries.append(
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="busy",
                    detail="active job",
                )
            )
            continue
        repo_path = Path(repo.repo_path)
        if repo_path.is_dir():
            entries.append(
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="live",
                    detail="checkout present",
                )
            )
            continue
        if not absence_is_corroborated(repo_path):
            entries.append(
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="unreachable",
                    detail="down mount",
                )
            )
            continue
        if repo.missing_since is None:
            if not req.dry_run:
                state.jobs.set_missing_since(repo.slug, repo.repo_path, now)
            entries.append(
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="stamped",
                    detail="first sighting",
                )
            )
            continue
        if req.force or _ripe(repo, now):
            if req.dry_run:
                entries.append(
                    PruneEntry(
                        slug=repo.slug,
                        repo_path=repo.repo_path,
                        verdict="pruned",
                        store_dir=_would_remove_store_dir(state, repo, rows),
                        detail="dry run: would prune",
                    )
                )
                continue
            removed_dir = await _delete_one(state, repo, rows)
            entries.append(
                PruneEntry(
                    slug=repo.slug,
                    repo_path=repo.repo_path,
                    verdict="pruned",
                    row_deleted=True,
                    store_dir=removed_dir,
                    store_dir_removed=removed_dir is not None,
                )
            )
            continue
        entries.append(
            PruneEntry(
                slug=repo.slug,
                repo_path=repo.repo_path,
                verdict="waiting",
                detail="grace not elapsed",
            )
        )
    return _view(req, entries)
