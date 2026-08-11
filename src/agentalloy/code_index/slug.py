"""Canonical repo-slug derivation for the code-index module.

This IS the canonical implementation now: the code-index module stores each
repo's per-slug data directory (``repos/{slug}/``) under the slug derived
here, and every consumer (ingest pipeline, ``/code`` routers, unwire cleanup)
must produce the *identical* string. Originally adopted from codebase-indexer's
``app/services/slug.py`` (``parse_github_remote`` / ``canonical_slug_for_path``
/ ``derive_slug``) plus ``app/config.py:slugify_repo``; agentalloy no longer
mirrors an external system of record.

The canonical rule is:

  1. Exactly one remote, named ``origin`` (refuse to guess when 0 or >1).
  2. ``origin`` is a recognized git host URL (github.com, or any other host —
     GitLab, Bitbucket, self-hosted) → a canonical, path-independent slug.
     github.com keeps its original ``{org}__{repo}`` form (no host prefix) so
     existing indexes aren't stranded; every other host gets ``{host}__{org}__{repo}``
     so two different hosts with an identically-named org/repo don't collide.
  3. Otherwise (no git dir, zero/multiple remotes, unparseable URL) fall back
     to the directory basename — this is what left every worktree of a
     non-GitHub repo re-indexing from scratch, since a worktree's basename
     differs from the main checkout's.

Then ``slugify_repo`` enforces a filesystem-safe charset on the result.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches any of the three common git remote URL shapes, capturing the host
# and the org/repo path so non-GitHub hosts (GitLab, Bitbucket, self-hosted)
# can be slugged canonically instead of falling back to the basename:
#   scheme://[user@]host[:port]/<path>(.git)?      e.g. https://gitlab.com/org/repo.git
#   git@host:<path>(.git)?                          e.g. git@gitlab.com:org/repo.git
#   ssh://git@host[:port]/<path>(.git)?             e.g. ssh://git@gitlab.com/org/repo.git
_GIT_URL_RE = re.compile(
    r"""^
    (?:
        (?:[A-Za-z][A-Za-z0-9+.-]*://)(?:[^@/]+@)?(?P<host_a>[A-Za-z0-9.-]+)(?::\d+)?/(?P<path_a>.+)
        |
        (?:[^@/]+@)?(?P<host_b>[A-Za-z0-9.-]+):(?P<path_b>.+)
    )
    $""",
    re.VERBOSE,
)

# Hard cap on subprocess wall-clock so a hung filesystem can't stall a query.
_GIT_TIMEOUT_S = 5.0


def slugify_repo(name: str) -> str:
    """Mirror of codebase-indexer ``config.slugify_repo``.

    Replaces anything that's not alphanumeric/dash/underscore/dot with ``_``,
    collapses runs, strips leading/trailing separators. Never empty.
    """
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return s or "repo"


_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def parse_git_remote(url: str | None) -> tuple[str, str, str] | None:
    """Parse any git remote URL into ``(host, org, repo)``; None if unparseable.

    Generalizes the old GitHub-only parsing to arbitrary hosts (GitLab,
    Bitbucket, self-hosted) so ``canonical_slug_for_path`` can key non-GitHub
    repos by host identity instead of falling back to the directory basename.
    The basename fallback is what stranded every worktree of a non-GitHub repo
    with its own from-scratch code index, since a worktree's basename differs
    from the main checkout's.

    ``org`` may itself contain ``/`` for nested groups (GitLab subgroups) —
    everything but the final path segment (``repo``).

    >>> parse_git_remote("git@github.com:navistone/TheForge.git")
    ('github.com', 'navistone', 'TheForge')
    >>> parse_git_remote("https://gitlab.com/team/backend/repo.git")
    ('gitlab.com', 'team/backend', 'repo')
    """
    if url is None:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    match = _GIT_URL_RE.match(candidate)
    if not match:
        return None
    host = match.group("host_a") or match.group("host_b")
    path = match.group("path_a") or match.group("path_b")
    if not host or not path:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    org, repo = "/".join(parts[:-1]), parts[-1]
    return (host.lower(), org, repo)


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Parse a GitHub remote URL into ``(org, repo)``; None for non-GitHub.

    >>> parse_github_remote("git@github.com:navistone/TheForge.git")
    ('navistone', 'TheForge')
    >>> parse_github_remote("https://github.com/navistone/TheForge")
    ('navistone', 'TheForge')
    >>> parse_github_remote("https://gitlab.com/foo/bar.git") is None
    True
    """
    parsed = parse_git_remote(url)
    if parsed is None:
        return None
    host, org, repo = parsed
    if host not in _GITHUB_HOSTS or "/" in org:
        return None
    return (org, repo)


def canonical_slug_for_path(local_path: Path) -> str | None:
    """Return a canonical, worktree-path-independent slug for *local_path*'s origin.

    Refuses to guess when there are zero or multiple remotes (an ``origin``
    fork plus an ``upstream`` would otherwise route the slug to the wrong
    project) or when the origin URL doesn't parse as a git remote — those
    cases fall back to the directory basename.

    github.com origins keep the original ``{org}__{repo}`` form (no host
    prefix) so existing indexes aren't stranded. Every other parseable host
    (GitLab, Bitbucket, self-hosted) gets ``{host}__{org}__{repo}`` — still
    canonical and independent of the checkout's directory name, just
    host-qualified so two hosts with an identically-named org/repo don't
    collide.
    """
    path = Path(local_path)
    if not path.is_dir():
        return None
    try:
        remotes_proc = subprocess.run(
            ["git", "-C", str(path), "remote"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if remotes_proc.returncode != 0:
            return None
        remotes = [r.strip() for r in remotes_proc.stdout.splitlines() if r.strip()]
        if len(remotes) != 1 or remotes[0] != "origin":
            # Zero, multiple, or non-origin remote — ambiguous; use basename.
            return None

        url_proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if url_proc.returncode != 0:
            return None
        url = (url_proc.stdout or "").strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("code_index.slug: git probe failed for %s — %s", path, exc)
        return None

    parsed = parse_git_remote(url)
    if parsed is None:
        return None
    host, org, repo = parsed
    if host in _GITHUB_HOSTS:
        return slugify_repo(f"{org}__{repo}")
    return slugify_repo(f"{host}__{org}__{repo}")


def derive_slug(local_path: Path, fallback_basename: str) -> str:
    """Canonical slug for ``local_path``, else the slugified basename.

    Mirror of codebase-indexer ``slug.derive_slug``.
    """
    canonical = canonical_slug_for_path(Path(local_path))
    if canonical:
        return canonical
    return slugify_repo(fallback_basename or "repo")


def repo_slug(project_root: Path) -> str:
    """The codebase-indexer slug for the repo at ``project_root``.

    Convenience wrapper: ``derive_slug(project_root, project_root.name)``.
    """
    project_root = Path(project_root)
    return derive_slug(project_root, project_root.name)
