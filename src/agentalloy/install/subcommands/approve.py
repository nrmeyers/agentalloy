"""``approve`` subcommand — record a human approval marker and auto-advance.

    agentalloy approve spec      — sign off the spec phase, then advance to design
    agentalloy approve design    — sign off the design phase, then advance to build
    agentalloy approve sdd-fast  — sign off the fast lane (gated only when enabled)
    agentalloy approve add-skill — sign off the custom skill, then return to intake

The marker lives at ``.agentalloy/approved/<phase>`` and records who approved,
when, and a SHA-256 over the phase's exit artifact(s). The digest gives post-hoc
detectability of *which* artifact state was approved — a cooperative-trust model
(consistent with the existing ``--force`` parity), not hard unforgeability.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_APPROVABLE = ("spec", "design", "sdd-fast", "add-skill")
_EXIT_ARTIFACT_GLOB = {
    "spec": "docs/spec/*.md",
    "design": "docs/design/**/*.md",
    "sdd-fast": "docs/fast/*.md",
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}


def _digest(root: Path, glob: str) -> str:
    """Stable SHA-256 over the phase's exit artifact(s): path + content, sorted."""
    files = sorted(p for p in root.glob(glob) if p.is_file())
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(b":")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def run_approve(
    phase: str, root: Path | None = None, approver: str | None = None
) -> dict[str, Any]:
    """Record approval for *phase* and advance to the next phase.

    Refuses if the live phase isn't *phase* or its exit artifact is absent.
    Returns ``{"ok": False, "error": ...}`` on refusal, else ``{"ok": True, ...,
    "advanced": <run_phase_set result>}`` (which itself may carry ``blocked`` if a
    downstream artifact-completeness gate still isn't met).
    """
    from agentalloy.install.state import _repo_root  # pyright: ignore[reportPrivateUsage]
    from agentalloy.install.subcommands.phase import (  # noqa: PLC0415
        _read_phase,  # pyright: ignore[reportPrivateUsage]
        run_phase_set,
    )
    from agentalloy.signals.gates import (  # noqa: PLC0415
        _PHASE_GRAPH,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.predicates import approval_marker_path  # noqa: PLC0415

    root = root or _repo_root()
    existing = _read_phase(root)
    current = existing.get("phase") if existing else None
    if current != phase:
        return {"ok": False, "error": f"current phase is '{current}', not '{phase}'"}

    glob = _EXIT_ARTIFACT_GLOB[phase]
    if not any(p.is_file() for p in root.glob(glob)):
        return {"ok": False, "error": f"no exit artifact at '{glob}' to approve"}

    approver = approver or os.environ.get("USER") or "unknown"
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha = _digest(root, glob)
    marker = approval_marker_path(root, phase)
    _atomic_write(
        marker,
        f'approver: {approver}\napproved_at: "{now}"\nartifact_sha256: {sha}\n',
    )

    nxt = _PHASE_GRAPH.get(phase, phase)
    advanced = run_phase_set(nxt, root=root)  # marker now exists → approval gate passes
    return {
        "ok": True,
        "phase": phase,
        "approver": approver,
        "marker": str(marker),
        "advanced": advanced,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "approve",
        help="Record human approval for the current SDD phase and advance.",
    )
    p.add_argument(
        "phase",
        choices=_APPROVABLE,
        help="Phase to approve: spec, design, sdd-fast.",
    )
    p.add_argument(
        "--approver",
        default=None,
        help="Approver identity to record (default: $USER).",
    )
    _add_project_root_flag(p)
    p.set_defaults(func=_run)


def _add_project_root_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-root",
        default=None,
        help=(
            "Repo directory to read/write the .agentalloy/ approval marker in. "
            "Default: auto-detect from cwd (stops at $HOME)."
        ),
    )


def _resolve_root(args: argparse.Namespace) -> Path | None:
    """Explicit --project-root wins; otherwise None defers to _repo_root()."""
    pr = getattr(args, "project_root", None)
    return Path(pr).expanduser().resolve() if pr else None


def _run(args: argparse.Namespace) -> int:
    result = run_approve(
        args.phase, root=_resolve_root(args), approver=getattr(args, "approver", None)
    )
    if not result.get("ok"):
        print(f"Cannot approve '{args.phase}': {result.get('error')}", file=sys.stderr)
        return 1

    print(f"Approval recorded for '{result['phase']}' by {result['approver']}")
    print(f"  Marker: {result['marker']}")

    advanced: dict[str, Any] = result.get("advanced") or {}
    if advanced.get("blocked"):
        # Approval is logged, but the forward step still needs its completeness
        # gate (e.g. design → build needs a build contract). Surface why.
        print(
            f"  Approval saved, but staying in '{advanced.get('phase')}' — "
            f"{advanced.get('target')} not yet reachable:",
            file=sys.stderr,
        )
        for advisory in advanced.get("advisories", []):
            print(f"    {advisory}", file=sys.stderr)
        return 1
    print(f"  Advanced to: {advanced.get('phase')}")
    return 0
