"""Install state file management.

Owns read/write of the user-scoped install state. As of schema v3, state
lives at ``${XDG_CONFIG_HOME:-~/.config}/agentalloy/install-state.json``
rather than per-repo so a single AgentAlloy install can serve multiple
repos. The corpus and any output artifacts live under
``${XDG_DATA_HOME:-~/.local/share}/agentalloy/``.

The legacy per-repo path (``<repo>/.agentalloy/install-state.json``)
is detected on load and surfaces a warning telling the user to move it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CURRENT_SCHEMA_VERSION = 3

STATE_DIR_NAME = "agentalloy"  # under XDG_CONFIG_HOME
STATE_FILE_NAME = "install-state.json"
OUTPUTS_DIR_NAME = "outputs"
CORPUS_DIR_NAME = "corpus"
LEGACY_STATE_DIR_NAME = ".agentalloy"  # used pre-v2; per-repo


def user_config_dir() -> Path:
    """Return ``${XDG_CONFIG_HOME:-~/.config}/agentalloy``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / STATE_DIR_NAME


def user_data_dir() -> Path:
    """Return ``${XDG_DATA_HOME:-~/.local/share}/agentalloy``."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / STATE_DIR_NAME


def corpus_dir() -> Path:
    """User-writable corpus location. Populated on first run from the wheel."""
    return user_data_dir() / CORPUS_DIR_NAME


def env_path() -> Path:
    """User-scoped ``.env`` location. Holds runtime config for the service."""
    return user_config_dir() / ".env"


def parse_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse a .env file into a plain dict. Pure — no side effects.

    KEY=value lines, # comments, ``export KEY=value`` shorthand, optional
    matching outer quotes stripped. Path defaults to ``env_path()``.
    Returns ``{}`` if the file is missing.
    """
    p = path if path is not None else env_path()
    if not p.exists():
        return {}
    parsed: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            parsed[key] = val
    return parsed


def load_env_into_environ(path: Path | None = None) -> list[str]:
    """Parse a .env file and inject its keys into ``os.environ``.

    Returns the list of keys loaded (for logging). Process-env values
    already set take precedence over the file (matching pydantic-settings'
    priority model). For non-mutating use see ``parse_env_file``.
    """
    loaded: list[str] = []
    for key, val in parse_env_file(path).items():
        if key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


def _is_real_corpus(p: Path) -> bool:
    """Sentinel check — returns True only if ``p`` looks like a complete corpus.

    A directory that exists but lacks ``skills.duck`` is either a partial
    install (interrupted copy) or a same-named dir from a different
    package. Either way we should refuse to use it.
    """
    return p.exists() and (p / "skills.duck").exists()


def bundled_corpus_dir() -> Path | None:
    """Return the read-only corpus shipped inside the wheel, if locatable.

    Uses ``importlib.resources`` to find ``agentalloy/_corpus/``. Falls
    back to the repo's source tree for development checkouts. Returns
    ``None`` if no usable corpus is bundled (operator must seed manually).

    The sentinel check (``skills.duck`` must exist inside the dir) blocks
    silently returning a same-named-but-empty directory from a different
    package on the Python path.
    """
    try:
        from importlib import resources

        # The corpus is package data under agentalloy/_corpus/. resources.files
        # returns a Traversable; for filesystem-backed package layouts (the
        # only kind we ship) this exposes __fspath__ and we can convert to
        # a Path for shutil. Zipped wheels don't currently apply (the
        # corpus is binary data not safe to read from a zip).
        corpus = resources.files("agentalloy") / "_corpus"
        fspath = getattr(corpus, "__fspath__", None)
        if fspath is not None:
            p = Path(fspath())
            if _is_real_corpus(p):
                return p
    except (ModuleNotFoundError, FileNotFoundError, AttributeError, OSError):
        pass
    # Dev fallback: walk up from this file looking for src/agentalloy/_corpus.
    # Apply the same sentinel check so a stub _corpus dir on PYTHONPATH
    # can't shadow the real one.
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "src" / "agentalloy" / "_corpus"
        if _is_real_corpus(candidate):
            return candidate
    return None


def ensure_corpus_seeded() -> tuple[Path, bool]:
    """Copy the bundled corpus into the user data dir if not already present.

    Returns ``(corpus_path, was_seeded)`` — ``was_seeded`` is True if this
    call created the user copy, False if it was already there. Idempotent.
    """
    user_corpus = corpus_dir()
    if (user_corpus / "skills.duck").exists() and (user_corpus / "ladybug").exists():
        return user_corpus, False
    bundled = bundled_corpus_dir()
    if bundled is None:
        # No bundled corpus — caller must surface a clear error.
        return user_corpus, False
    user_corpus.mkdir(parents=True, exist_ok=True)
    src_duck = bundled / "skills.duck"
    src_ladybug = bundled / "ladybug"

    def _atomic_copy(src: Path, dst: Path) -> None:
        """Copy src → dst via a temp sibling so a partial / interrupted
        copy never leaves a half-written file at the final path. Without
        this, an aborted copytree leaves a directory at `ladybug/` whose
        `.exists()` is True forever, and the next run skips it.
        """
        if dst.exists():
            return
        tmp = dst.with_name(dst.name + ".part")
        # Wipe any leftover from a previous failed attempt.
        if tmp.exists():
            if tmp.is_dir():
                shutil.rmtree(tmp)
            else:
                tmp.unlink()
        try:
            if src.is_dir():
                shutil.copytree(src, tmp)
            else:
                shutil.copyfile(src, tmp)
            os.replace(tmp, dst)  # atomic on the same filesystem
        except BaseException:
            with contextlib.suppress(FileNotFoundError, OSError):
                if tmp.is_dir():
                    shutil.rmtree(tmp)
                else:
                    tmp.unlink()
            raise

    if src_duck.exists():
        _atomic_copy(src_duck, user_corpus / "skills.duck")
    if src_ladybug.exists():
        _atomic_copy(src_ladybug, user_corpus / "ladybug")
    return user_corpus, True


def is_inside_root(path: Path, root: Path) -> bool:
    """Return True if ``path`` resolves to a location inside ``root``.

    Used as a containment guard around any code path that takes a
    filesystem path from install-state.json (which lives inside the
    user's repo and could be tampered with by a hostile dependency or
    attacker) before writing or unlinking. Resolution failures count as
    "not inside" so callers fall through their skip / warn branch.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False
    return True


def validate_port(value: Any) -> int:
    """Coerce a state-file `port` value to a sane integer.

    Hostile state may set port to a string (`"1@evil.com:80"` for URL
    confusion), a float, a negative int, or > 65535. Reject all of those
    rather than letting them flow into URL construction or socket calls.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        print(
            f"ERROR: install-state.json `port` must be an integer; got {type(value).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if value < 1 or value > 65535:
        print(
            f"ERROR: install-state.json `port` {value} is out of range (1–65535).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def _repo_root() -> Path:
    """Locate the current repo root.

    Used by subcommands that need cwd-relative paths: ``wire-harness``
    (for sentinel injection target), and as a back-compat ``root`` value
    passed to state functions that ignore it (``doctor``, ``update``,
    ``reset-step``, ``pull-models``, ``uninstall``). Walks up from cwd
    looking for a project marker (pyproject.toml, package.json, .git,
    etc.). Falls back to cwd.

    Note: this is *not* used to locate agentalloy state — state is
    user-scoped (see ``state_dir``, ``pack_source_dir``).
    """
    cwd = Path.cwd().resolve()
    markers = ("pyproject.toml", "package.json", ".git", "Cargo.toml", "go.mod")
    for ancestor in (cwd, *cwd.parents):
        if any((ancestor / m).exists() for m in markers):
            return ancestor
    return cwd


# `root` parameters are accepted on the public state functions for
# backwards-compatibility with subcommand call sites. They are ignored
# for state/outputs paths (those are user-scoped now); they still apply
# to wire-harness's per-repo file targets.


def state_dir(root: Path | None = None) -> Path:  # noqa: ARG001 — kept for back-compat
    """Return the user-scoped state directory."""
    return user_config_dir()


def pack_source_dir() -> Path:
    """Return the user-scoped directory holding installed pack YAML drafts.

    Co-located with LadybugDB and other install state. Replaces the
    previous cwd-dependent ``<_repo_root()>/skill-source/`` location.
    """
    return user_config_dir() / "skill-source"


def state_path(root: Path | None = None) -> Path:  # noqa: ARG001
    """Return the user-scoped install-state.json path."""
    return user_config_dir() / STATE_FILE_NAME


def outputs_dir(root: Path | None = None) -> Path:  # noqa: ARG001
    """Return the user-scoped outputs directory."""
    return user_data_dir() / OUTPUTS_DIR_NAME


def _legacy_state_path_from_repo(repo_root: Path) -> Path:
    """Pre-v2 (per-repo) install-state.json path. For migration detection."""
    return repo_root / LEGACY_STATE_DIR_NAME / STATE_FILE_NAME


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "install_started_at": datetime.now(UTC).isoformat(),
        "completed_steps": [],
        "harness_files_written": [],
        "models_pulled": [],
        "env_original_content": None,  # Backup of original .env content for restore
        "env_path": None,
        "port": 47950,
        "last_verify_passed_at": None,
        # Pack selection from setup wizard, awaiting consumption by
        # install-packs. Cleared once install-packs has applied it. When
        # absent/None, install-packs falls back to interactive prompt
        # (or always-on defaults in non-TTY mode).
        "pending_pack_selection": None,
        # Deployment type: "native" (direct host install) or "container"
        # (podman/docker compose). None until setup has been run.
        "deployment": None,
        # Absolute path to the compose YAML file used for container deploys.
        "compose_file": None,
        # Compose binary label for display/state: "podman compose" or
        # "docker compose". Used by uninstall to reconstruct the command.
        "compose_binary": None,
        # Absolute path to the compose binary detected at install time
        # (e.g. "/usr/bin/podman"). Used by uninstall so PATH changes
        # between install and uninstall don't break compose down.
        "compose_binary_path": None,
    }


def load_state(root: Path | None = None) -> dict[str, Any]:
    """Load install state, or return a fresh empty state if no file exists.

    Exits with code 3 if the file's schema_version is newer than the code supports.
    """
    fp = state_path()
    if not fp.exists():
        # Detect legacy per-repo state and warn (not an error — first run is
        # the common case). Migration is a manual one-liner.
        legacy = _legacy_state_path_from_repo(root or _repo_root())
        if legacy.exists():
            print(
                f"WARNING: Found legacy per-repo state at {legacy}.\n"
                f"         AgentAlloy now uses user-scoped state at {fp}.\n"
                f"         To migrate: mv {legacy} {fp}\n"
                f"         (Or delete the legacy file and re-run setup.)",
                file=sys.stderr,
            )
        return _empty_state()
    data = json.loads(fp.read_text())
    raw_version = data.get("schema_version", 0)
    # A hostile state file can set schema_version to a string or other
    # type; coercing through int() with a fallback gives a clean exit
    # instead of an unhandled TypeError downstream.
    try:
        file_version = int(raw_version)
    except (TypeError, ValueError):
        print(
            f"ERROR: install-state.json schema_version {raw_version!r} is not an integer.",
            file=sys.stderr,
        )
        raise SystemExit(3) from None
    if file_version > CURRENT_SCHEMA_VERSION:
        print(
            f"ERROR: install-state.json schema_version {file_version} "
            f"is newer than this code supports ({CURRENT_SCHEMA_VERSION}). "
            f"Update agentalloy before re-running install.",
            file=sys.stderr,
        )
        raise SystemExit(3)
    if file_version < CURRENT_SCHEMA_VERSION:
        data = _migrate(data, file_version)
    return data


def _atomic_write(target: Path, content: str, *, mode: int = 0o644) -> None:
    """Write to a sibling tempfile then os.replace into target.

    Atomic on POSIX and Windows (when on the same filesystem). Survives
    crashes and concurrent writers — readers see either the old file or
    the new one, never a torn write.

    Symlink-safe: opens the temp file with O_NOFOLLOW so a symlink
    pre-planted at the .tmp path can't redirect the write. Stale .tmp
    files (e.g. from a previous crashed run) are removed first; if the
    .tmp path is a symlink, removing it deletes only the link, never the
    target.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    # Remove a stale tempfile (or hostile symlink) before opening exclusively.
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    # O_NOFOLLOW only exists on POSIX; on Windows the O_EXCL above is
    # sufficient because Windows does not follow reparse points the same way.
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        # If writing fails, remove the half-written tempfile so the next
        # invocation's O_EXCL doesn't trip on it.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    os.replace(tmp, target)


def save_state(data: dict[str, Any], root: Path | None = None) -> Path:  # noqa: ARG001
    """Write install state to disk atomically. Creates the directory if needed."""
    fp = state_path()
    _atomic_write(fp, json.dumps(data, indent=2) + "\n")
    return fp


def record_step(
    data: dict[str, Any],
    step: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a completed-step entry. Returns the mutated state dict."""
    entry: dict[str, Any] = {
        "step": step,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    if extra:
        entry.update(extra)
    data["completed_steps"].append(entry)
    return data


def is_step_completed(data: dict[str, Any], step: str) -> bool:
    return any(s["step"] == step for s in data.get("completed_steps", []))


def get_step_output(data: dict[str, Any], step: str) -> dict[str, Any] | None:
    for s in data.get("completed_steps", []):
        if s["step"] == step:
            return s
    return None


def get_pending_pack_selection(data: dict[str, Any]) -> list[str] | None:
    """Return the user's pending pack selection from setup, or None if absent.

    Setup (``simple_setup``) writes this on first run after the user picks
    packs interactively; ``install-packs`` reads and clears it. A return of
    ``None`` means there is no pending selection — caller should fall back
    to interactive prompt or non-interactive defaults.
    """
    raw = data.get("pending_pack_selection")
    if raw is None:
        return None
    if isinstance(raw, list):
        items: list[Any] = raw  # type: ignore[assignment] — runtime-validated above
        return [str(p) for p in items if isinstance(p, str)]
    return None


def set_pending_pack_selection(data: dict[str, Any], packs: list[str]) -> dict[str, Any]:
    """Record a pack selection to be consumed by the next install-packs run."""
    data["pending_pack_selection"] = list(packs)
    return data


def clear_pending_pack_selection(data: dict[str, Any]) -> dict[str, Any]:
    """Clear a previously-recorded pack selection after install-packs consumed it."""
    data["pending_pack_selection"] = None
    return data


def save_output_file(
    content: dict[str, Any],
    filename: str,
    root: Path | None = None,  # noqa: ARG001 — kept for back-compat
) -> tuple[Path, str]:
    """Write a JSON output file to the user outputs dir. Returns (path, sha256)."""
    fp = outputs_dir() / filename
    raw = json.dumps(content, indent=2) + "\n"
    _atomic_write(fp, raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return fp, f"sha256:{digest}"


def _migrate(data: dict[str, Any], from_version: int) -> dict[str, Any]:
    """Migrate state file forward to CURRENT_SCHEMA_VERSION."""
    if from_version < 1:
        data.setdefault("harness_files_written", [])
        data.setdefault("models_pulled", [])
        data.setdefault("env_path", None)
        data.setdefault("port", 47950)
        data.setdefault("last_verify_passed_at", None)
    if from_version < 2:
        # v1 → v2: state moved from per-repo to user-scope. v1 had a
        # single top-level `harness` field; in v2 each harness_files_written
        # entry carries its own `harness` so multi-repo wiring is first
        # class. Read the old top-level value BEFORE popping so existing
        # entries inherit the correct harness rather than a fixed guess.
        legacy_harness = data.get("harness") or "claude-code"
        legacy_repo_root = data.get("repo_root")
        data.pop("harness", None)
        data.pop("repo_root", None)
        for entry in data.get("harness_files_written", []):
            entry.setdefault("harness", legacy_harness)
            if legacy_repo_root:
                entry.setdefault("repo_root", legacy_repo_root)
    if from_version < 3:
        # v2 -> v3: add container deployment fields
        data.setdefault("deployment", None)
        data.setdefault("compose_file", None)
        data.setdefault("compose_binary", None)
    # Forward-compatibility: ensure newly added optional fields exist on
    # older state files. Not a version bump because the field is purely
    # additive and defaults to None.
    data.setdefault("pending_pack_selection", None)
    data["schema_version"] = CURRENT_SCHEMA_VERSION
    return data
