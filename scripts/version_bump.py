#!/usr/bin/env python3
"""Derive the automatic SemVer bump for a PR from its Conventional-Commit title.

The version BUMP is no longer a human decision (RELEASE.md §4): CI runs this to
compute the next version and writes ``pyproject.toml`` + ``uv.lock`` on the PR
branch. Two gates, both deterministic:

- *Whether* to bump: the diff must touch the **shipped surface** (``src/``,
  ``frontend/``, container, dep pins) — docs/CI/test-only PRs don't version.
- *Which tier*: read from the PR title — ``feat!``/``BREAKING CHANGE`` → major,
  ``feat`` → minor, everything else touching shipped code → patch. The
  "else → patch" default honours §4's invariant (*a tag's version tells the
  truth about shipped content*) with zero human judgement.

The pure functions below carry no I/O and are unit-tested in
``tests/test_version_bump.py`` (mirrors the ``test_pack_version_bump_guard.py``
ethos). ``main`` does the git reads and prints the next version to stdout, or
nothing when no bump is warranted (docs-only, or the PR is already bumped —
idempotent, so the job's own push re-runs to a clean no-op).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

Tier = Literal["major", "minor", "patch"]

# Shipped surface — a change here means users run different bytes, so it must
# version (RELEASE.md §4). Single source of truth; keep in lockstep with the
# doc and with test_pack_version_bump_guard's _PACKS_PREFIX (a subset here).
SHIPPED_DIR_PREFIXES = ("src/", "frontend/", "container/")
SHIPPED_EXACT_FILES = ("pyproject.toml", "uv.lock")  # dependency pins
SHIPPED_FILE_PREFIXES = ("Containerfile",)  # Containerfile, Containerfile.dev, …

# type(scope)!: subject  —  scope and the breaking "!" are both optional.
_CONVENTIONAL = re.compile(r"^\s*(?P<type>[a-zA-Z]+)(?:\([^)]*\))?(?P<bang>!)?:")


def tier_from_title(title: str) -> Tier:
    """Map a Conventional-Commit PR title to a SemVer tier.

    ``feat!`` / a ``BREAKING CHANGE`` marker → major; ``feat`` → minor;
    anything else (``fix``, ``perf``, ``refactor``, ``chore``, or a title that
    doesn't parse) → patch. Never returns None: the no-bump decision belongs to
    :func:`touches_shipped_surface`, not to the tier.
    """
    if "BREAKING CHANGE" in title or "BREAKING-CHANGE" in title:
        return "major"
    m = _CONVENTIONAL.match(title)
    if m is None:
        return "patch"
    if m.group("bang"):
        return "major"
    if m.group("type").lower() == "feat":
        return "minor"
    return "patch"


def touches_shipped_surface(changed_files: list[str]) -> bool:
    """True if any changed path is part of the shipped wheel/image surface."""
    for f in changed_files:
        p = f.strip()
        if not p:
            continue
        if p in SHIPPED_EXACT_FILES:
            return True
        if p.startswith(SHIPPED_DIR_PREFIXES):
            return True
        if p.startswith(SHIPPED_FILE_PREFIXES):
            return True
    return False


def bump(version: str, tier: Tier) -> str:
    """Return ``version`` advanced by ``tier``, resetting lower components."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a plain X.Y.Z version: {version!r}")
    major, minor, patch = (int(part) for part in parts)
    if tier == "major":
        return f"{major + 1}.0.0"
    if tier == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def decide(title: str, changed_files: list[str], current_version: str) -> str | None:
    """Compute the next version for a PR, or None when no bump is warranted."""
    if not touches_shipped_surface(changed_files):
        return None
    return bump(current_version, tier_from_title(title))


# ---------------------------------------------------------------------------
# I/O boundary — git reads + pyproject parse; kept out of the pure logic above
# ---------------------------------------------------------------------------

_VERSION_LINE = re.compile(r'^version = "(?P<v>[^"]+)"', re.MULTILINE)


def _version_from_pyproject_text(text: str) -> str | None:
    m = _VERSION_LINE.search(text)
    return m.group("v") if m else None


def _read_current_version(pyproject: Path) -> str | None:
    return _version_from_pyproject_text(pyproject.read_text(encoding="utf-8"))


def _git(args: list[str], repo_root: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _version_at_ref(base_sha: str, repo_root: Path) -> str | None:
    try:
        text = _git(["show", f"{base_sha}:pyproject.toml"], repo_root)
    except subprocess.CalledProcessError:
        return None
    return _version_from_pyproject_text(text)


def _changed_files(base_sha: str, repo_root: Path) -> list[str]:
    # Three-dot: changes on HEAD since the merge-base with base_sha — the PR's
    # OWN changes only. Immune to main moving ahead of a non-rebased branch (a
    # two-dot diff would surface main's files as changed and false-positive a
    # bump). Requires full history (the workflow checks out with fetch-depth 0).
    out = _git(["diff", "--name-only", f"{base_sha}...HEAD"], repo_root)
    return [line for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True, help="PR title (conventional commit)")
    parser.add_argument("--base-sha", required=True, help="PR base commit SHA")
    parser.add_argument("--repo-root", default=".", help="repo root (default: cwd)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    pyproject = repo_root / "pyproject.toml"

    current = _read_current_version(pyproject)
    if current is None:
        print("error: no version in pyproject.toml", file=sys.stderr)
        return 2

    # Idempotency: if the branch already carries a bump (version differs from
    # base), emit nothing so the job's own re-triggered run is a clean no-op.
    base_version = _version_at_ref(args.base_sha, repo_root)
    if base_version is not None and base_version != current:
        return 0

    changed = _changed_files(args.base_sha, repo_root)
    nxt = decide(args.title, changed, current)
    if nxt is not None:
        print(nxt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
