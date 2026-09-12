"""Profile resolver for AgentAlloy v2.

Profiles are named bundles of configuration that auto-detect based on:
  1. Explicit project marker  (.agentalloy/profile)
  2. Git remote URL pattern   (match_remote in profiles.yaml)
  3. Path prefix              (match_path in profiles.yaml)
  4. Fallback to default

Simplified from v1 — v2 profiles control which skill packs are active
and which domain tags are preferred. The full corpus is always available;
profiles filter what gets auto-injected.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True)
class Profile:
    """A resolved profile."""

    name: str
    packs: list[str] = field(default_factory=list)
    domain_tags: list[str] = field(default_factory=list)
    is_default: bool = False


@dataclass
class ProfilesConfig:
    """Parsed profiles.yaml configuration."""

    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_profile: str = DEFAULT_PROFILE_NAME


def profiles_root() -> Path:
    """Return ~/.agentalloy/ (honoring XDG_DATA_HOME)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy"


def profiles_yaml_path() -> Path:
    """Return ~/.agentalloy/profiles.yaml."""
    return profiles_root() / "profiles.yaml"


def project_marker_path(root: Path) -> Path:
    """Return <project>/.agentalloy/profile."""
    return root / ".agentalloy" / "profile"


def load_profiles_config() -> ProfilesConfig:
    """Load ~/.agentalloy/profiles.yaml."""
    path = profiles_yaml_path()
    if not path.exists():
        return ProfilesConfig()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ProfilesConfig()

    profiles_raw = data.get("profiles", {}) or {}
    default_profile_raw = data.get("default_profile", DEFAULT_PROFILE_NAME)

    if not profiles_raw:
        profiles_raw = {DEFAULT_PROFILE_NAME: {}}
    if not default_profile_raw:
        default_profile_raw = DEFAULT_PROFILE_NAME

    return ProfilesConfig(
        profiles=profiles_raw if isinstance(profiles_raw, dict) else {},
        default_profile=str(default_profile_raw),
    )


def _git_remote_url(cwd: Path) -> str | None:
    """Return the origin remote URL, or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return None


def detect_profile(cwd: Path | None = None) -> Profile:
    """Resolve the active profile for cwd.

    Priority:
      1. Explicit project marker
      2. Git remote URL pattern
      3. Path prefix
      4. Default profile
    """
    if cwd is None:
        cwd = Path.cwd()

    config = load_profiles_config()

    # 1. Explicit project marker
    marker_path = project_marker_path(cwd)
    if marker_path.exists():
        try:
            marker_data = yaml.safe_load(marker_path.read_text(encoding="utf-8")) or {}
            marker_profile = str(marker_data.get("profile", "")).strip()
            if marker_profile and marker_profile in config.profiles:
                return _build_profile(marker_profile, config)
        except (yaml.YAMLError, OSError):
            pass

    # 2. Git remote URL match
    remote_url = _git_remote_url(cwd)
    if remote_url:
        for name, rules in config.profiles.items():
            match_remote = rules.get("match_remote", []) or []
            for pattern in match_remote:
                if fnmatch.fnmatch(remote_url, str(pattern)):
                    return _build_profile(name, config)

    # 3. Path prefix match
    cwd_abs = cwd.resolve()
    for name, rules in config.profiles.items():
        match_path = rules.get("match_path", []) or []
        for pattern in match_path:
            expanded = Path(str(pattern)).expanduser()
            parts = expanded.parts
            while len(parts) > 1 and parts[-1] in ("*", "**"):
                parts = parts[:-1]
            base = Path(*parts)
            try:
                cwd_abs.relative_to(base.resolve())
                return _build_profile(name, config)
            except ValueError:
                pass

    # 4. Default
    return _build_default_profile()


def _build_default_profile() -> Profile:
    """Build the default profile."""
    return Profile(name=DEFAULT_PROFILE_NAME, is_default=True)


def _build_profile(name: str, config: ProfilesConfig) -> Profile:
    """Build a Profile from config."""
    rules = config.profiles.get(name, {})
    packs = rules.get("packs", []) or []
    domain_tags = rules.get("domain_tags", []) or []
    return Profile(
        name=name,
        packs=[str(p) for p in packs],
        domain_tags=[str(t) for t in domain_tags],
        is_default=(name == DEFAULT_PROFILE_NAME),
    )


def list_profiles(cwd: Path | None = None) -> list[dict[str, Any]]:
    """Return all configured profiles."""
    config = load_profiles_config()
    active = detect_profile(cwd) if cwd else None

    result: list[dict[str, Any]] = [
        {
            "name": DEFAULT_PROFILE_NAME,
            "active": active.name == DEFAULT_PROFILE_NAME if active else False,
            "is_default": True,
        }
    ]

    for name, rules in config.profiles.items():
        if name == DEFAULT_PROFILE_NAME:
            continue
        result.append({
            "name": name,
            "active": active.name == name if active else False,
            "is_default": False,
            "packs": rules.get("packs", []) or [],
            "match_remote": rules.get("match_remote", []) or [],
            "match_path": rules.get("match_path", []) or [],
        })

    return result


# --- Repo technology detection (skill-selection relevance filter) ---

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
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", ".qwen", "target", ".mypy_cache", ".ruff_cache",
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
    stack = [repo_root]
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

    tags |= _manifest_framework_tags(repo_root)
    return tags


def _manifest_framework_tags(repo_root: Path) -> set[str]:
    """Framework tags from dependency manifests (best effort)."""
    import json as _json

    tags: set[str] = set()
    try:
        pkg = repo_root / "package.json"
        if pkg.exists():
            data = _json.loads(pkg.read_text(encoding="utf-8"))
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
        path = repo_root / manifest
        try:
            if path.exists():
                texts.append(path.read_text(encoding="utf-8").lower())
        except OSError:
            pass
    blob = "\n".join(texts)
    if blob:
        import re as _re

        for dep, tag in _DEP_FRAMEWORKS.items():
            if "/" in dep:
                continue  # npm-scoped names never appear in python manifests
            if _re.search(rf"\b{_re.escape(dep)}\b", blob):
                tags.add(tag)
    return tags
