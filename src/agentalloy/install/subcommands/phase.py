"""``phase`` subcommand — phase lock file management.

Manage the `.agentalloy/phase` YAML file that tracks the current
SDD phase for a project session.

Commands:
    agentalloy phase            — print current phase
    agentalloy phase set <phase> — write/update the phase lock file
    agentalloy phase clear      — remove the phase lock file
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# "intake" is the entry phase: a freshly-wired repo starts here so the intake
# workflow (intent interview) composes on the first prompt, then hands off to
# "spec" (see signals.gates._PHASE_GRAPH).
VALID_PHASES = ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast")

SCHEMA_VERSION = 1


def _phase_path(root: Path) -> Path:
    return root / ".agentalloy" / "phase"


def _read_phase(root: Path) -> dict[str, Any] | None:
    """Read and parse the phase lock file. Returns None if not found."""
    p = _phase_path(root)
    if not p.exists():
        return None
    # Simple YAML parser — no pyyaml dependency needed for this flat format.
    # Use partition on first colon only to handle values containing colons (e.g. ISO timestamps).
    data: dict[str, Any] = {}
    for line in p.read_text().splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data if data else None


def _write_phase(data: dict[str, Any], root: Path) -> None:
    """Write the phase lock file, creating .agentalloy/ if needed."""
    p = _phase_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, str) and "T" in value:
            # ISO timestamp — quote it so YAML parsers read it as string
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    p.write_text("\n".join(lines) + "\n")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_phase_get(root: Path | None = None) -> dict[str, Any]:
    """Get the current phase from the lock file."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    data = _read_phase(root)
    if data is None:
        return {"phase": None, "message": "No active phase"}
    return {
        "phase": data.get("phase"),
        "started_at": data.get("started_at"),
        "last_updated": data.get("last_updated"),
        "workflow": data.get("workflow"),
    }


def run_phase_set(phase: str, root: Path | None = None) -> dict[str, Any]:
    """Set or update the current phase."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()

    if phase not in VALID_PHASES:
        print(
            f"Error: invalid phase '{phase}'. Valid phases: {', '.join(VALID_PHASES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    existing = _read_phase(root)
    now = _now_iso()

    data: dict[str, Any] = {
        "phase": phase,
        "started_at": existing.get("started_at", now) if existing else now,
        "last_updated": now,
        "workflow": f"sdd-{phase}",
    }

    _write_phase(data, root)
    return data


def run_phase_clear(root: Path | None = None) -> dict[str, Any]:
    """Remove the phase lock file."""
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]

    root = root or _repo_root()
    p = _phase_path(root)
    if p.exists():
        p.unlink()
        return {"message": "Phase cleared", "phase": None}
    return {"message": "No phase to clear", "phase": None}


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "phase",
        help="Manage the current SDD phase (get, set, clear).",
    )
    _add_project_root_flag(p)  # for the default (get) action
    sub = p.add_subparsers(dest="phase_action")

    p_set = sub.add_parser("set", help="Set the current phase")
    p_set.add_argument(
        "phase",
        choices=VALID_PHASES,
        help="Phase to set: intake, spec, design, build, qa, ship, sdd-fast",
    )
    _add_project_root_flag(p_set)
    p_set.set_defaults(func=_run_set)

    p_clear = sub.add_parser("clear", help="Clear the current phase")
    _add_project_root_flag(p_clear)
    p_clear.set_defaults(func=_run_clear)

    # Default action (no subcommand) = get
    p.set_defaults(func=_run_get)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help=(
            "Repo directory to read/write the .agentalloy/phase file in. "
            "Default: auto-detect from cwd (stops at $HOME)."
        ),
    )


def _resolve_root(args: argparse.Namespace) -> Path | None:
    """Explicit --project-root wins; otherwise None defers to _repo_root()."""
    pr = getattr(args, "project_root", None)
    return Path(pr).expanduser().resolve() if pr else None


def _run_get(args: argparse.Namespace) -> int:
    result = run_phase_get(root=_resolve_root(args))
    print(f"Phase: {result.get('phase', 'none')}")
    if result.get("started_at"):
        print(f"Started: {result['started_at']}")
    if result.get("last_updated"):
        print(f"Updated: {result['last_updated']}")
    if result.get("workflow"):
        print(f"Workflow: {result['workflow']}")
    return 0


def _run_set(args: argparse.Namespace) -> int:
    result = run_phase_set(args.phase, root=_resolve_root(args))
    print(f"Phase set to: {result['phase']}")
    return 0


def _run_clear(args: argparse.Namespace) -> int:
    result = run_phase_clear(root=_resolve_root(args))
    print(result["message"])
    return 0
