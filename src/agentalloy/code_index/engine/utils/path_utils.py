from fnmatch import fnmatch
from pathlib import Path

from .. import constants as cs


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    if path.is_file() and path.suffix in cs.IGNORE_SUFFIXES:
        return True
    rel_path = path.relative_to(repo_path)
    rel_path_str = rel_path.as_posix()
    dir_parts = rel_path.parent.parts if path.is_file() else rel_path.parts

    # Check directory-based exclusions FIRST. A filename unignore (e.g.
    # `!.env.example`) must NOT resurrect files that live inside a directory
    # that's been excluded wholesale (e.g. `.claude/worktrees` or `node_modules`).
    # Without this ordering, a safe-template basename would pull worktree
    # copies of itself back into the index.
    dir_excluded = bool(exclude_paths) and (
        not exclude_paths.isdisjoint(dir_parts)
        or rel_path_str in exclude_paths
        or any(rel_path_str.startswith(f"{p}/") for p in exclude_paths)
    )
    if dir_excluded:
        return True

    # Basename / glob unignore: re-enable files that a basename exclude
    # would otherwise catch (e.g. `!.env.example` overriding `.env.*`).
    if unignore_paths and path.is_file() and any(fnmatch(path.name, p) for p in unignore_paths):
        return False

    # For files, let a bare filename / glob pattern (e.g. `.DS_Store`,
    # `.env`, `.env.*`, `*.pem`) in .cgrignore match the file anywhere in
    # the tree — gitignore-style basename + glob matching.
    if exclude_paths and path.is_file() and any(fnmatch(path.name, p) for p in exclude_paths):
        return True

    # Path-based unignore (e.g. `!vendor/mylib`) — whitelists a subtree that
    # the built-in IGNORE_PATTERNS would otherwise skip.
    if unignore_paths and any(
        rel_path_str == p or rel_path_str.startswith(f"{p}/") for p in unignore_paths
    ):
        return False

    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)


def should_prune_dir(
    dir_path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Decide whether a directory should be pruned at walk time.

    This is the directory-level companion to :func:`should_skip_path`. The
    eligible-file scan must NOT descend into heavy / ignored directories
    (``node_modules``, ``.git``, nested git worktrees under ``.claude``, etc.)
    — for a local working tree these can hold tens of thousands of files and
    tens of GB, and merely enumerating + stat-ing them stalls the
    "discovering" phase long enough for the job watchdog to reap the job.

    Pruning at traversal time means those subtrees are never enumerated at
    all, so the cost is proportional to the indexed source tree rather than
    the entire on-disk working tree.

    A directory is pruned when:

    * Its basename (or any ancestor segment, relative to the repo root) is in
      :data:`constants.IGNORE_PATTERNS` — the built-in heavy/noise dir set
      (``.git``, ``node_modules``, ``.venv``, ``__pycache__``, ``dist``,
      ``build``, ``.next``, ``.turbo``, ``.cache``, ``.claude``, ``.svn``,
      ``vendor``, ``site-packages``, ...).
    * It matches a caller-supplied directory exclude in ``exclude_paths``
      (basename match, exact relative-path match, or a relative-path prefix —
      mirroring the directory cases in :func:`should_skip_path`).

    It is NOT pruned when it (or an ancestor) is explicitly whitelisted via
    ``unignore_paths`` — a ``!vendor/mylib`` style re-enable must still allow
    the walk to descend so the whitelisted files are reachable.

    Args:
        dir_path: Absolute path of the candidate directory.
        repo_path: Absolute repo root the walk is anchored at.
        exclude_paths: Caller-supplied excludes (merged ``.cgrignore`` +
            request ``exclude_paths``). Directory-style entries prune here;
            bare-filename/glob entries are file-level only and ignored here.
        unignore_paths: Whitelist that prevents pruning of a subtree.

    Returns:
        True when the walk should not descend into ``dir_path``.
    """
    try:
        rel_path = dir_path.relative_to(repo_path)
    except ValueError:
        # Defensive: a path outside the repo root should not be descended.
        return True

    rel_path_str = rel_path.as_posix()
    dir_parts = rel_path.parts

    # Never prune a subtree the caller explicitly whitelisted (or any ancestor
    # of one) — the walk must reach the whitelisted files.
    if unignore_paths and any(
        rel_path_str == p or rel_path_str.startswith(f"{p}/") or p.startswith(f"{rel_path_str}/")
        for p in unignore_paths
    ):
        return False

    # Caller-supplied directory excludes: basename segment match, exact
    # relative-path match, or relative-path prefix. (Bare-filename/glob excludes
    # are file-level concerns handled in should_skip_path, not here.)
    if exclude_paths and (
        not exclude_paths.isdisjoint(dir_parts)
        or rel_path_str in exclude_paths
        or any(rel_path_str.startswith(f"{p}/") for p in exclude_paths)
    ):
        return True

    # Built-in heavy/noise directory set — any segment match prunes.
    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)
