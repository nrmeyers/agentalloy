"""Persistent registry of indexed repositories.

The service indexes every registered repo into one shared multi-repo index
(live in the OverGraph store; rebuilt from this list on each service start).
`agentalloy add <repo>` registers a repo via POST /reindex. The registry
lives next to state.duck in the instance directory, so it survives restarts.
"""

import hashlib
import json
import re
from pathlib import Path


def project_key(repo: str | Path) -> str:
    """Canonical project key for a repo: ``<name>-<sha256(path)[:8]>``.

    Derived deterministically from the resolved absolute path so every
    component (wire, proxy URL prefix, MCP server, store scope) computes the
    same key independently. URL-safe: name is slugged, hash disambiguates
    same-named dirs.
    """
    canonical = str(Path(repo).expanduser().resolve())
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(canonical).name).strip("-").lower() or "repo"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return f"{name}-{digest}"


def registry_path(state_duck: str | Path) -> Path:
    return Path(state_duck).parent / "repos.json"


def load_repos(state_duck: str | Path) -> list[str]:
    """Registered repos (absolute paths). Missing or corrupt file → empty."""
    try:
        data = json.loads(registry_path(state_duck).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, str)]


def save_repos(state_duck: str | Path, repos: list[str]) -> None:
    registry_path(state_duck).write_text(json.dumps(repos, indent=2) + "\n")


def upsert_repo(state_duck: str | Path, repo: str) -> list[str]:
    """Register a repo (canonical absolute path), preserving order.

    Returns the updated list.
    """
    canonical = str(Path(repo).expanduser().resolve())
    repos = load_repos(state_duck)
    if canonical not in repos:
        repos.append(canonical)
    save_repos(state_duck, repos)
    return repos
