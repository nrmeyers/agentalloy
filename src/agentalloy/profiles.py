# pyright: reportMissingTypeStubs=false
# pyright: reportArgumentType=false
"""Profile management for multi-repo support.

Profiles let users configure per-environment settings (skills, datastore
paths, SDD packs, domain tags) and activate them by project marker, git
remote, path pattern, or an explicit env var.

Config file location (user-level):
    $XDG_CONFIG_HOME/agentalloy/profiles.yaml
    (or ~/.config/agentalloy/profiles.yaml on Linux)

Example profiles.yaml:
    default_profile: default

    profiles:
      default: {}

      monorepo:
        match_remote:
          - "git@github.com:company/*.git"
        match_path:
          - "~/work/company/*"
        packs: [sdd-core, python]
        domain_tags: [python, fastapi]

      personal:
        match_remote:
          - "git@github.com:me/*.git"

Activation rules (in order):
    1. Explicit override: ``AGENTALLOY_PROFILE=<name>`` env var
    2. Project marker:   ``.agentalloy/profile`` file in the repo root
    3. Git remote match: ``git remote get-url origin`` matches a glob
    4. Path match:        cwd matches a glob pattern
    5. Default profile (``default_profile`` key, or "default")

The v2 server uses ``packs`` / ``domain_tags`` to filter which skill packs
and domain tags are auto-injected; the v10 install subcommands use the
per-profile ``skills_dir`` / ``datastore_path``. Both field sets coexist on
the same ``Profile``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default profile name when no matchers are hit.
DEFAULT_PROFILE_NAME = "default"

#: Valid keys under an ``overrides`` block in profiles.yaml.
VALID_OVERRIDE_CLASSES: frozenset[str] = frozenset(
    {"datastore", "skills", "paths", "code_index", "search"}
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """A resolved profile.

    ``skills_dir`` / ``datastore_path`` are the per-profile storage paths used
    by the v10 install subcommands; ``packs`` / ``domain_tags`` are the v2
    domain-selection fields (which SDD packs to load and which entity domain
    tags to index).
    """

    name: str
    skills_dir: Path
    datastore_path: Path
    is_default: bool = False
    packs: list[str] = field(default_factory=list)
    domain_tags: list[str] = field(default_factory=list)


@dataclass
class ProfilesConfig:
    """Parsed profiles.yaml."""

    default_profile: str
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def profiles_root() -> Path:
    """Base directory for profile config + per-profile data."""
    cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not cfg:
        cfg = str(Path.home() / ".config")
    root = Path(cfg) / "agentalloy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def profile_dir(name: str) -> Path:
    return profiles_root() / "profiles" / name


def profile_skills_dir(name: str) -> Path:
    d = profile_dir(name) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_datastore_path(name: str) -> Path:
    d = profile_dir(name) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "datastore.duckdb"


def domain_datastore_path(domain: str = "default") -> Path:
    """Datastore for a domain profile (one per distinct domain set)."""
    d = profile_dir(domain) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "datastore.duckdb"


def profiles_yaml_path() -> Path:
    return profiles_root() / "profiles.yaml"


def project_marker_path(root: Path) -> Path:
    """Project-local marker file: ``<root>/.agentalloy/profile``."""
    return root / ".agentalloy" / "profile"


# ---------------------------------------------------------------------------
# YAML load / write
# ---------------------------------------------------------------------------


def load_profiles_config() -> ProfilesConfig:
    """Load profiles.yaml; returns an empty default if absent."""
    path = profiles_yaml_path()
    if not path.is_file():
        return ProfilesConfig(default_profile=DEFAULT_PROFILE_NAME, profiles={})
    raw: dict[str, Any] = {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ProfilesConfig(default_profile=DEFAULT_PROFILE_NAME, profiles={})

    profiles: dict[str, dict[str, Any]] = {}
    for name, cfg in (raw.get("profiles") or {}).items():
        if not isinstance(name, str):
            continue
        if not isinstance(cfg, dict):
            cfg = {}
        name = name.strip()
        if not name:
            continue
        profiles[name] = cfg

    default = raw.get("default_profile")
    if not isinstance(default, str) or not default.strip():
        default = DEFAULT_PROFILE_NAME
    return ProfilesConfig(default_profile=default.strip(), profiles=profiles)


def _atomic_yaml_write(path: Path, data: dict[str, Any]) -> None:
    """Write YAML atomically via a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _save_config(cfg: ProfilesConfig) -> None:
    data: dict[str, Any] = {
        "default_profile": cfg.default_profile,
        "profiles": cfg.profiles,
    }
    _atomic_yaml_write(profiles_yaml_path(), data)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_remote_url(cwd: Path) -> str | None:
    """Return the ``origin`` remote URL for ``cwd`` if it's a git repo."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return None


def _match_pattern(value: str, pattern: str) -> bool:
    """Glob match that also handles git remote URL forms."""
    pat = str(pattern)
    if fnmatch.fnmatch(value, pat):
        return True
    # Normalize git URLs: strip scheme, handle .git suffix
    normalized = value.replace("git@", "").replace("https://", "")
    if fnmatch.fnmatch(normalized, pat):
        return True
    if value.endswith(".git") and fnmatch.fnmatch(value[:-4], pat):
        return True
    return False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_profile(cwd: Path | None = None) -> Profile:
    """Return the active profile for the current working directory.

    Resolution order:
        1. ``AGENTALLOY_PROFILE`` env var
        2. Project marker (``.agentalloy/profile``)
        3. Git remote URL glob match
        4. Path glob match
        5. ``default_profile`` setting
    """
    if cwd is None:
        cwd = Path.cwd()

    cfg = load_profiles_config()

    # 1. Explicit env override
    env_name = os.environ.get("AGENTALLOY_PROFILE", "").strip()
    if env_name and env_name in cfg.profiles:
        return _build_profile(env_name, cfg, is_default=False)

    # 2. Project marker
    marker_path = project_marker_path(cwd)
    if marker_path.is_file():
        try:
            marker_data = yaml.safe_load(marker_path.read_text(encoding="utf-8")) or {}
            marker_profile = str(marker_data.get("profile", "")).strip()
            if marker_profile and marker_profile in cfg.profiles:
                return _build_profile(marker_profile, cfg, is_default=False)
        except (yaml.YAMLError, OSError):
            pass

    # 3. Git remote match
    remote = _git_remote_url(cwd)
    if remote:
        for name, rules in cfg.profiles.items():
            for pattern in rules.get("match_remote", []) or []:
                if _match_pattern(remote, pattern):
                    return _build_profile(name, cfg, is_default=False)

    # 4. Path match
    cwd_str = str(cwd)
    for name, rules in cfg.profiles.items():
        for pattern in rules.get("match_path", []) or []:
            expanded = os.path.expanduser(str(pattern))
            if fnmatch.fnmatch(cwd_str, expanded):
                return _build_profile(name, cfg, is_default=False)

    # 5. Default
    return _build_profile(cfg.default_profile, cfg, is_default=True)


def _build_profile(name: str, cfg: ProfilesConfig, is_default: bool) -> Profile:
    rules = cfg.profiles.get(name, {}) or {}
    return Profile(
        name=name,
        skills_dir=profile_skills_dir(name),
        datastore_path=profile_datastore_path(name),
        is_default=is_default,
        packs=[str(p) for p in (rules.get("packs", []) or [])],
        domain_tags=[str(t) for t in (rules.get("domain_tags", []) or [])],
    )


# ---------------------------------------------------------------------------
# Query / mutation (init / set-default / delete)
# ---------------------------------------------------------------------------


def get_profile(name: str) -> Profile:
    """Return a profile by name, or raise ``KeyError`` if unknown.

    The implicit default is always resolvable even if absent from the yaml.
    """
    cfg = load_profiles_config()
    if name not in cfg.profiles and name != cfg.default_profile and name != DEFAULT_PROFILE_NAME:
        raise KeyError(f"unknown profile: {name!r}")
    return _build_profile(name, cfg, is_default=(name == cfg.default_profile))


def init_profile(
    name: str,
    match_remote: list[str] | None = None,
    match_path: list[str] | None = None,
) -> Profile:
    """Create a new profile entry in profiles.yaml.

    Raises ``ValueError`` if the profile already exists.
    """
    cfg = load_profiles_config()
    if name in cfg.profiles:
        raise ValueError(f"profile {name!r} already exists")

    rules: dict[str, Any] = {}
    if match_remote:
        rules["match_remote"] = list(match_remote)
    if match_path:
        rules["match_path"] = list(match_path)

    cfg.profiles[name] = rules
    _save_config(cfg)
    return _build_profile(name, cfg, is_default=False)


def set_default_profile(name: str) -> None:
    """Change the ``default_profile`` key."""
    cfg = load_profiles_config()
    if name not in cfg.profiles and name != DEFAULT_PROFILE_NAME:
        raise KeyError(f"unknown profile: {name!r}")
    cfg.default_profile = name
    _save_config(cfg)


def delete_profile(name: str) -> None:
    """Remove a profile from profiles.yaml.

    Raises ``ValueError`` for the default / active default, ``KeyError`` for
    an unknown profile. Does NOT delete the on-disk data directories.
    """
    if name == DEFAULT_PROFILE_NAME:
        raise ValueError(f"cannot delete the default profile {name!r}")
    cfg = load_profiles_config()
    if name not in cfg.profiles:
        raise KeyError(f"unknown profile: {name!r}")
    if cfg.default_profile == name:
        raise ValueError("cannot delete the active default profile; change default first")
    del cfg.profiles[name]
    _save_config(cfg)


def list_profiles(cwd: Path | None = None) -> list[dict[str, Any]]:
    """Return all configured profiles with activation metadata.

    Each entry is a dict with: ``name``, ``active_for_cwd``, ``is_default``,
    ``match_remote``, ``match_path``, ``packs``, ``domain_tags``,
    ``has_overrides``.
    """
    cfg = load_profiles_config()
    active_name = detect_profile(cwd).name if cwd is not None else cfg.default_profile
    out: list[dict[str, Any]] = []
    for name, rules in cfg.profiles.items():
        out.append(
            {
                "name": name,
                "active_for_cwd": name == active_name,
                "is_default": name == cfg.default_profile,
                "match_remote": rules.get("match_remote", []) or [],
                "match_path": rules.get("match_path", []) or [],
                "packs": rules.get("packs", []) or [],
                "domain_tags": rules.get("domain_tags", []) or [],
                "has_overrides": "overrides" in rules,
            }
        )
    # Ensure the default profile appears even if not explicitly in the yaml.
    if cfg.default_profile not in cfg.profiles:
        out.append(
            {
                "name": cfg.default_profile,
                "active_for_cwd": cfg.default_profile == active_name,
                "is_default": True,
                "match_remote": [],
                "match_path": [],
                "packs": [],
                "domain_tags": [],
                "has_overrides": False,
            }
        )
    return out


def _ensure_profile_dir(name: str) -> Path:
    """Create the profile directory structure if missing."""
    d = profile_dir(name)
    (d / "skills").mkdir(parents=True, exist_ok=True)
    (d / "data").mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Repo technology detection (v2 — skill-selection relevance filter)
# ---------------------------------------------------------------------------

# Extensions → language tag. Only languages that also appear in skill/pack
# names matter for filtering, but detecting broadly costs nothing.
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".kt": "kotlin",
    ".swift": "swift",
}

# Dependency names → framework tag (matched against pyproject/requirements/
# package.json dependency names, lowercase).
_DEP_FRAMEWORKS: dict[str, str] = {
    "fastapi": "fastapi",
    "django": "django",
    "flask": "flask",
    "pytest": "pytest",
    "sqlalchemy": "sqlalchemy",
    "fastify": "fastify",
    "express": "express",
    "react": "react",
    "vue": "vue",
    "next": "nextjs",
    "svelte": "svelte",
    "@nestjs/core": "nestjs",
    "rails": "rails",
    "spring-boot": "spring",
}

_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".qwen",
    "target",
    ".mypy_cache",
    ".ruff_cache",
}

_SCAN_FILE_CAP = 4000


def detect_repo_tags(repo_root: Path) -> set[str]:
    """Language + framework tags actually present in a repo.

    Extension histogram (languages with >=3 files, or any when the repo is
    small) plus dependency-manifest names. Used to filter the skill catalog
    the orchestrator sees, so a pure-Python repo never gets typescript or
    fastify skills recommended.
    """
    counts: dict[str, int] = {}
    scanned = 0
    stack = [Path(repo_root)]
    while stack and scanned < _SCAN_FILE_CAP:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if scanned >= _SCAN_FILE_CAP:
                break
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
                continue
            scanned += 1
            lang = _EXT_LANG.get(entry.suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1

    total = sum(counts.values())
    threshold = 1 if total < 30 else 3
    tags = {lang for lang, n in counts.items() if n >= threshold}

    tags |= _manifest_framework_tags(Path(repo_root))
    return tags


def _manifest_framework_tags(repo_root: Path) -> set[str]:
    """Framework tags from dependency manifests (best effort)."""
    root = Path(repo_root)
    tags: set[str] = set()
    try:
        pkg = root / "package.json"
        if pkg.exists():
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps: dict[str, Any] = {}
            for key in ("dependencies", "devDependencies"):
                block = data.get(key)
                if isinstance(block, dict):
                    deps.update(block)
            for name in deps:
                tag = _DEP_FRAMEWORKS.get(str(name).lower())
                if tag:
                    tags.add(tag)
    except (OSError, ValueError):
        pass

    texts: list[str] = []
    for manifest in ("pyproject.toml", "requirements.txt"):
        path = root / manifest
        try:
            if path.exists():
                texts.append(path.read_text(encoding="utf-8").lower())
        except OSError:
            pass
    blob = "\n".join(texts)
    if blob:
        for dep, tag in _DEP_FRAMEWORKS.items():
            if "/" in dep:
                continue  # npm-scoped names never appear in python manifests
            if re.search(rf"\b{re.escape(dep)}\b", blob):
                tags.add(tag)
    return tags
