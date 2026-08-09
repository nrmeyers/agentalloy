# pyright: reportPrivateUsage=false
"""``approve`` subcommand — record a human approval marker and auto-advance.

    agentalloy approve spec      — sign off the spec phase, then advance to design
    agentalloy approve design    — sign off the design phase, then advance to build
    agentalloy approve sdd-fast  — sign off the fast lane (gated only when enabled)
    agentalloy approve add-skill — sign off the custom skill, then return to intake

The marker lives in the state store (``sdd_state``, kind ``approved``, scoped by
phase) and records a SHA-256 digest over the phase's artifact bodies at approval
time. The digest gives post-hoc detectability of *which* artifact state was
approved — a cooperative-trust model (consistent with the existing ``--force``
parity), not hard unforgeability.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from agentalloy.api.state_client import StateClientError

_APPROVABLE = ("spec", "design", "plan", "sdd-fast", "add-skill")
_STORE_BACKED_PHASES = frozenset({"spec", "design", "plan", "sdd-fast"})
# add-skill alone keeps a disk-glob exit gate: a custom-skill pack YAML is
# tool-written configuration (`agentalloy new-skill-pack`), not a phase
# deliverable body an agent hand-writes. Every lifecycle artifact is store-backed
# — naming a disk path in agent-facing output is what taught agents to write
# `docs/fast/*.md` (gitignored, and invisible to the store-backed exit gate).
_DISK_EXIT_ARTIFACT_GLOB = {
    "add-skill": ".agentalloy/custom-skills/**/*.yaml",
}


def _atomic_write(path: Path, text: str) -> None:
    import contextlib
    import uuid

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _digest_disk(root: Path, glob: str) -> str:
    """Stable SHA-256 over the phase's exit artifact(s) on disk: path + content, sorted."""
    import hashlib

    files = sorted(p for p in root.glob(glob) if p.is_file())
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(b":")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


def run_approve(
    phase: str,
    root: Path | None = None,
    approver: str | None = None,
) -> dict[str, Any]:
    """Record approval for *phase* and advance to the next phase.

    Refuses if the live phase isn't *phase* or its exit artifact is absent.
    Returns ``{"ok": False, "error": ...}`` on refusal, else ``{"ok": True, ...,
    "advanced": <run_phase_set result>}`` (which itself may carry ``blocked`` if a
    downstream artifact-completeness gate still isn't met).

    Runs entirely here rather than handing off to ``POST /state/approve``.  That
    route only writes an ``approved`` row: it does not check the live phase, does
    not write the marker the approval predicate actually reads, and does not
    advance the phase.  Short-circuiting to it meant that with the service up,
    ``agentalloy approve design`` reported success while approving nothing and
    advancing nowhere.  The phase read and write below go through the store
    either way, so there is no second source of truth left to route around.
    """
    from agentalloy.install.state import _repo_root
    from agentalloy.install.subcommands._state import (  # noqa: PLC0415
        fail_on_state_error,
        phase_access,
    )
    from agentalloy.install.subcommands.phase import run_phase_set  # noqa: PLC0415
    from agentalloy.signals.graph import (  # noqa: PLC0415
        _NEXT as _PHASE_GRAPH,
    )

    root = root or _repo_root()
    access = phase_access(root)

    try:
        existing = access.read()
    except StateClientError as exc:
        fail_on_state_error(exc)
        raise  # unreachable
    current = existing.phase if existing else None
    if current != phase:
        return {"ok": False, "error": f"current phase is '{current}', not '{phase}'"}

    approver = approver or os.environ.get("USER") or "unknown"
    marker_desc: str

    if phase in _STORE_BACKED_PHASES:
        from agentalloy.signals.gates import (  # noqa: PLC0415
            _APPROVAL_STORE_NAME_GLOB,
        )
        from agentalloy.signals.predicates import (  # noqa: PLC0415
            _artifact_digest,
            _resolve_workitem_slug_for,
        )

        handle = access.contracts_handle()
        # MUST digest exactly the set the gate re-digests when it checks the
        # marker. Approving over a wider set than the gate's `since_name_glob`
        # records a digest that can never match, so the approval silently never
        # counts. Live for design now that the split narrowed its glob to
        # approach.md while a pre-split repo may still hold tasks.md and
        # test-plan.md under phase=design.
        #
        # Scope to the active work-item with the SAME resolver the gate uses, so
        # both sides re-digest an identical row set (#501/#518). When no single
        # work item resolves (degenerate repo with no contract), both sides fall
        # back to repo-global (slug=None) — identical, so the digest still
        # matches. Only a *non-empty differentiated* row set would diverge.
        slug = _resolve_workitem_slug_for(handle, root, phase)
        rows = handle.list_artifacts(
            phase,
            slug=slug,
            name_glob=_APPROVAL_STORE_NAME_GLOB.get(phase),
        )
        if not rows:
            return {
                "ok": False,
                # Name the store verb, not a path. An error that says only
                # "no exit artifact" invites the agent to create a file; the
                # gate reads the store, so a file would satisfy nothing.
                "error": (
                    f"no exit artifact recorded for phase '{phase}' to approve — "
                    f"record it with `agentalloy contract artifact-set --phase {phase} "
                    f"--slug <slug> --name <name>.md` (the artifact lives in the "
                    f"store, not on disk)"
                ),
            }
        digest = _artifact_digest(rows)
        handle.set_approval(phase, digest, approver=approver)
        marker_desc = f"state store (approved/{phase})"
    else:
        from datetime import UTC, datetime  # noqa: PLC0415

        from agentalloy.signals.predicates import approval_marker_path  # noqa: PLC0415

        glob = _DISK_EXIT_ARTIFACT_GLOB[phase]
        if not any(p.is_file() for p in root.glob(glob)):
            return {"ok": False, "error": f"no exit artifact at '{glob}' to approve"}
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha = _digest_disk(root, glob)
        marker = approval_marker_path(root, phase)
        _atomic_write(
            marker,
            f'approver: {approver}\napproved_at: "{now}"\nartifact_sha256: {sha}\n',
        )
        marker_desc = str(marker)

    nxt = _PHASE_GRAPH.get(phase, phase)
    advanced = run_phase_set(nxt, root=root)  # marker now exists → approval gate passes
    return {
        "ok": True,
        "phase": phase,
        "approver": approver,
        "marker": marker_desc,
        "advanced": advanced,
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
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
        args.phase,
        root=_resolve_root(args),
        approver=getattr(args, "approver", None),
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
