"""DEPRECATED re-export shim — the canonical home is ``agentalloy.code_index.slug``.

Kept so pre-existing imports (``contracts.py``, external callers) keep working
until the PR7 cleanup removes this module. Import from
``agentalloy.code_index.slug`` in new code.
"""

from __future__ import annotations

from agentalloy.code_index.slug import (
    canonical_slug_for_path,
    derive_slug,
    parse_github_remote,
    repo_slug,
    slugify_repo,
)

__all__ = [
    "canonical_slug_for_path",
    "derive_slug",
    "parse_github_remote",
    "repo_slug",
    "slugify_repo",
]
