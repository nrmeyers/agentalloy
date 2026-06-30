"""Profile resolver and per-profile datastore manager.

Profiles are named bundles of system + workflow skill overrides. Each profile
has its own datastore (``skills.duck``) and override directory. The shared
domain datastore (``domain.duck``) is independent of profiles — all profiles
see the same domain corpus.

Resolution order for detection:
  1. Explicit project marker  (.agentalloy/profile)
  2. Git remote URL pattern   (match_remote in profiles.yaml)
  3. Path prefix              (match_path in profiles.yaml)
  4. Fallback to default_profile

All public functions are synchronous and cheap (<10ms) — detect_profile is
called on every retrieval and every hook fire.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import contextlib
import fnmatch
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PROFILE_NAME = "default"

# Valid skill classes for override files
VALID_OVERRIDE_CLASSES = frozenset({"system", "workflow"})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """A resolved profile with its associated paths.

    Attributes:
        name: Profile name (e.g. "default", "work").
        skills_dir: Base directory for override files
            ``~/.agentalloy/profiles/<name>/skills/``.
        datastore_path: DuckDB path for this profile's skills.
            ``~/.agentalloy/profiles/<name>/skills.duck``.
        is_default: True if this is the built-in ``default`` profile.
    """

    name: str
    skills_dir: Path
    datastore_path: Path
    is_default: bool = False


@dataclass
class ProfilesConfig:
    """Parsed profiles.yaml configuration.

    Attributes:
        profiles: Raw config dict keyed by profile name.
        default_profile: Name of the fallback profile.
    """

    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    default_profile: str = DEFAULT_PROFILE_NAME


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def profiles_root() -> Path:
    """Return ``~/.agentalloy/`` (honoring XDG_DATA_HOME).

    Resolved per-call so a process that adjusts XDG_DATA_HOME after import
    (e.g. tests) sees the correct location.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy"


def profile_dir(name: str) -> Path:
    """Return ``~/.agentalloy/profiles/<name>/``."""
    return profiles_root() / "profiles" / name


def profile_skills_dir(name: str) -> Path:
    """Return ``~/.agentalloy/profiles/<name>/skills/``."""
    return profile_dir(name) / "skills"


def profile_datastore_path(name: str) -> Path:
    """Return ``~/.agentalloy/profiles/<name>/skills.duck``."""
    return profile_dir(name) / "skills.duck"


def domain_datastore_path() -> Path:
    """Return the shared domain datastore path.

    This is independent of any profile — all profiles see the same domain
    corpus.
    """
    return profiles_root() / "domain.duck"


def profiles_yaml_path() -> Path:
    """Return ``~/.agentalloy/profiles.yaml``."""
    return profiles_root() / "profiles.yaml"


def project_marker_path(root: Path) -> Path:
    """Return ``<project>/.agentalloy/profile``."""
    return root / ".agentalloy" / "profile"


# ---------------------------------------------------------------------------
# Profile config loading
# ---------------------------------------------------------------------------


def load_profiles_config() -> ProfilesConfig:
    """Load ``~/.agentalloy/profiles.yaml``.

    Returns a default config if the file is missing or empty. The returned
    config always has at least the built-in ``default`` profile entry.
    """
    path = profiles_yaml_path()
    if not path.exists():
        return ProfilesConfig()

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}  # type: ignore[assignment]
    except yaml.YAMLError:
        # Corrupted profiles.yaml — fall back to defaults.
        return ProfilesConfig()

    profiles_raw: dict[str, dict[str, Any]] = data.get("profiles", {}) or {}  # type: ignore[assignment]
    default_profile_raw = data.get("default_profile", DEFAULT_PROFILE_NAME)  # type: ignore[assignment]

    if not profiles_raw:
        profiles_raw = {DEFAULT_PROFILE_NAME: {}}  # type: ignore[assignment]
    if not default_profile_raw:
        default_profile_raw = DEFAULT_PROFILE_NAME

    return ProfilesConfig(
        profiles=profiles_raw if isinstance(profiles_raw, dict) else {},  # type: ignore[arg-type]
        default_profile=str(default_profile_raw),  # pyright: ignore[reportUnknownArgumentType]
    )


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def _git_remote_url(cwd: Path) -> str | None:
    """Return the origin remote URL, or None if not a git repo."""
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


def _match_pattern(pattern: str, value: str) -> bool:
    """Check if ``value`` matches a glob-style pattern (fnmatch)."""
    return fnmatch.fnmatch(value, pattern)


def detect_profile(cwd: Path | None = None) -> Profile:
    """Resolve the active profile for ``cwd``.

    Priority order:
      1. Explicit project marker  (<project>/.agentalloy/profile)
      2. Git remote URL pattern   (match_remote in profiles.yaml)
      3. Path prefix              (match_path in profiles.yaml)
      4. Fallback to default_profile

    Returns a Profile object. If no profile configuration exists, returns
    the built-in default profile.

    Performance: <10ms. Called on every retrieval and every hook fire.
    """
    if cwd is None:
        cwd = Path.cwd()

    config = load_profiles_config()

    # 1. Check for explicit project marker
    marker_path = project_marker_path(cwd)
    if marker_path.exists():
        try:
            marker_data = yaml.safe_load(marker_path.read_text(encoding="utf-8")) or {}  # type: ignore[assignment]
            marker_profile = str(marker_data.get("profile", "")).strip()  # type: ignore[arg-type]
            if marker_profile and marker_profile in config.profiles:
                return _load_profile(marker_profile, config)
            elif marker_profile and marker_profile == DEFAULT_PROFILE_NAME:
                return _load_default_profile()
            elif marker_profile:
                # Marker references an unknown profile — fall through to
                # detection rules; don't error out on stale markers.
                pass
        except (yaml.YAMLError, OSError):
            pass

    # 2. Try git remote URL match
    remote_url = _git_remote_url(cwd)
    if remote_url:
        for name, rules in config.profiles.items():
            match_remote = rules.get("match_remote", []) or []  # type: ignore[union-attr]
            for pattern in match_remote:
                if _match_pattern(str(pattern), remote_url):  # type: ignore[arg-type]
                    return _load_profile(name, config)

    # 3. Try path prefix match
    cwd_abs = cwd.resolve()
    for name, rules in config.profiles.items():
        match_path = rules.get("match_path", []) or []  # type: ignore[union-attr]
        for pattern in match_path:
            # Expand ~, then strip a trailing "/*" or "/**" "everything-under"
            # glob so the documented PATH-PREFIX semantics actually hold: a
            # pattern matches when cwd equals its directory or is nested beneath
            # it. (Path.match alone is anchored at the right, so "<dir>/*" only
            # matched direct children and a bare "<dir>" matched nothing.)
            expanded_pattern = Path(str(pattern)).expanduser()  # type: ignore[arg-type]
            parts = expanded_pattern.parts
            while len(parts) > 1 and parts[-1] in ("*", "**"):
                parts = parts[:-1]
            base = Path(*parts)
            if any(ch in str(base) for ch in "*?["):
                # Still glob-y after stripping the trailing wildcard (e.g. a
                # mid-path "*") — fall back to fnmatch-style anchored matching.
                try:
                    if cwd_abs.match(str(base)):
                        return _load_profile(name, config)
                except ValueError:
                    pass
                continue
            try:
                cwd_abs.relative_to(base.resolve())
                return _load_profile(name, config)
            except ValueError:
                pass

    # 4. Fall back to default_profile
    default_name = config.default_profile
    if default_name == DEFAULT_PROFILE_NAME:
        return _load_default_profile()
    if default_name in config.profiles:
        return _load_profile(default_name, config)
    return _load_default_profile()


def _load_default_profile() -> Profile:
    """Load or create the built-in default profile."""
    _ensure_profile_dir(DEFAULT_PROFILE_NAME)
    return Profile(
        name=DEFAULT_PROFILE_NAME,
        skills_dir=profile_skills_dir(DEFAULT_PROFILE_NAME),
        datastore_path=profile_datastore_path(DEFAULT_PROFILE_NAME),
        is_default=True,
    )


def _load_profile(name: str, config: ProfilesConfig | None = None) -> Profile:
    """Load a profile by name. Creates the directory structure if needed."""
    _ = config  # Reserved for future use
    return Profile(
        name=name,
        skills_dir=profile_skills_dir(name),
        datastore_path=profile_datastore_path(name),
        is_default=(name == DEFAULT_PROFILE_NAME),
    )


def _ensure_profile_dir(name: str) -> Path:
    """Ensure the profile directory structure exists.

    Creates:
      ~/.agentalloy/profiles/<name>/skills/system/
      ~/.agentalloy/profiles/<name>/skills/workflow/
      ~/.agentalloy/profiles/<name>/skills.duck (empty)

    Returns the base profile directory.
    """
    base = profile_dir(name)
    skills = profile_skills_dir(name)
    (skills / "system").mkdir(parents=True, exist_ok=True)
    (skills / "workflow").mkdir(parents=True, exist_ok=True)

    # Create empty DuckDB datastore. The per-profile ``skills.duck`` holds only
    # the lightweight ``profile_skills`` table, which is created lazily on first
    # write (see install.subcommands.customize); here we just materialize the file.
    ds = profile_datastore_path(name)
    if not ds.exists():
        ds.parent.mkdir(parents=True, exist_ok=True)
        import duckdb

        duckdb.connect(str(ds)).close()

    return base


# ---------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------


def list_profiles(cwd: Path | None = None) -> list[dict[str, Any]]:
    """Return all configured profiles plus the built-in default.

    For each profile returns:
      - name: Profile name.
      - active_for_cwd: Whether this profile resolves for the given cwd.
      - match_remote: match_remote patterns (empty list if not set).
      - match_path: match_path patterns (empty list if not set).
      - has_overrides: True if override files exist.
    """
    config = load_profiles_config()
    active_profile = detect_profile(cwd) if cwd else None

    all_profiles: list[dict[str, Any]] = []

    # Always include the built-in default
    all_profiles.append(
        {
            "name": DEFAULT_PROFILE_NAME,
            "active_for_cwd": active_profile and active_profile.name == DEFAULT_PROFILE_NAME,
            "match_remote": [],
            "match_path": [],
            "has_overrides": _profile_has_overrides(DEFAULT_PROFILE_NAME),
            "is_default": True,
        }
    )

    # Include user-configured profiles
    for name, rules in config.profiles.items():
        if name == DEFAULT_PROFILE_NAME:
            continue
        all_profiles.append(
            {
                "name": name,
                "active_for_cwd": active_profile and active_profile.name == name,
                "match_remote": rules.get("match_remote", []) or [],
                "match_path": rules.get("match_path", []) or [],
                "has_overrides": _profile_has_overrides(name),
                "is_default": False,
            }
        )

    return all_profiles


def get_profile(name: str) -> Profile:
    """Look up a profile by name.

    Raises KeyError if the profile is not configured (and not the built-in
    default, which is always available).
    """
    if name == DEFAULT_PROFILE_NAME:
        return _load_default_profile()
    config = load_profiles_config()
    if name not in config.profiles:
        raise KeyError(f"Profile '{name}' is not configured")
    return _load_profile(name, config)


def init_profile(
    name: str,
    match_remote: list[str] | None = None,
    match_path: list[str] | None = None,
) -> Profile:
    """Create a new profile directory, datastore, and profiles.yaml entry.

    Args:
        name: Profile name (must be a valid identifier).
        match_remote: List of fnmatch patterns for git remote matching.
        match_path: List of fnmatch patterns for path matching.

    Returns:
        The newly created Profile object.
    """
    # Validate name
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid profile name: {name!r}. Use letters, digits, hyphens, underscores."
        )
    if name == DEFAULT_PROFILE_NAME:
        raise ValueError(f"Cannot use reserved name: {DEFAULT_PROFILE_NAME}")

    config = load_profiles_config()

    # Add to config
    config.profiles[name] = {}
    if match_remote:
        config.profiles[name]["match_remote"] = match_remote
    if match_path:
        config.profiles[name]["match_path"] = match_path

    # Write profiles.yaml
    data: dict[str, Any] = {
        "profiles": config.profiles,
        "default_profile": config.default_profile,
    }
    _atomic_yaml_write(profiles_yaml_path(), data)

    # Create directory structure
    _ensure_profile_dir(name)
    return _load_profile(name)


def set_default_profile(name: str) -> None:
    """Change the fallback default profile.

    Raises KeyError if the profile doesn't exist.
    """
    if name == DEFAULT_PROFILE_NAME:
        # Already the default — no-op
        return
    config = load_profiles_config()
    if name not in config.profiles:
        raise KeyError(f"Profile '{name}' is not configured")

    config.default_profile = name
    data: dict[str, Any] = {
        "profiles": config.profiles,
        "default_profile": config.default_profile,
    }
    _atomic_yaml_write(profiles_yaml_path(), data)


def delete_profile(name: str) -> None:
    """Remove a profile directory and config entry.

    Refuses to delete the built-in default profile (raises ValueError).
    Refuses to delete a profile that is currently set as default_profile
    (raises ValueError).
    """
    if name == DEFAULT_PROFILE_NAME:
        raise ValueError("Cannot delete the built-in default profile")

    config = load_profiles_config()
    if name not in config.profiles:
        raise KeyError(f"Profile '{name}' is not configured")

    if config.default_profile == name:
        raise ValueError(
            "Cannot delete the current default profile. "
            "Set a different default first with "
            "'agentalloy profile set-default <name>'."
        )

    # Remove profile directory
    pdir = profile_dir(name)
    if pdir.exists():
        shutil.rmtree(pdir)

    # Remove from config
    del config.profiles[name]
    data: dict[str, Any] = {
        "profiles": config.profiles,
        "default_profile": config.default_profile,
    }
    _atomic_yaml_write(profiles_yaml_path(), data)


def _profile_has_overrides(name: str) -> bool:
    """Check if a profile has any override files."""
    skills = profile_skills_dir(name)
    for class_dir in ("system", "workflow"):
        d = skills / class_dir
        if d.exists() and any(d.iterdir()):
            return True
    return False


def _atomic_yaml_write(target: Path, data: dict[str, Any]) -> None:
    """Write a YAML file atomically (write to temp, then rename)."""
    tmp = target.with_suffix(".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(target))
    except BaseException:
        with contextlib.suppress(FileNotFoundError, OSError):
            tmp.unlink()
        raise
