"""``agentalloy worktree <harness> <branch>`` — spin up an isolated parallel session.

Create a git worktree for *branch* and wire *harness* through the proxy at that
new path in one shot. Because the proxy keys per-repo state on
``base64url(realpath(cwd))`` (see ``api.proxy_context.encode_proj_token``), a
worktree's distinct path yields its own ``/proj/<token>`` — so its
``.agentalloy/`` phase + upstream are independent of the main checkout's, while
both sessions share the one user-scoped corpus and the one running service. This
is the supported way to run multiple agent sessions against the same repo at once.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentalloy.api.proxy_context import Upstream
from agentalloy.install.subcommands import add
from agentalloy.providers import REGISTRY


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Register the ``worktree`` subcommand."""
    p = subparsers.add_parser(
        "worktree",
        help="Create a git worktree for a branch and wire a harness through the proxy there.",
    )
    p.add_argument("harness", help="Harness to wire (e.g. hermes-agent).")
    p.add_argument("branch", help="Branch to check out (or create with -b) in the worktree.")
    p.add_argument(
        "--path",
        default=None,
        help="Worktree directory (default: ../<branch-leaf> beside the repo root).",
    )
    p.add_argument(
        "-b",
        "--new-branch",
        action="store_true",
        help="Create <branch> as a new branch instead of checking out an existing one.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--upstream-url",
        default=None,
        help="Override the captured upstream base URL (e.g. http://host:8080/v1).",
    )
    p.add_argument(
        "--upstream-model",
        default=None,
        help="Override the captured upstream model name.",
    )
    p.add_argument(
        "--key-env",
        default=None,
        help="Name of the env var holding the upstream API key (a reference, not the secret).",
    )
    p.set_defaults(func=_run)


def _repo_toplevel(cwd: Path) -> Path | None:
    """Return the git work-tree root for *cwd*, or ``None`` if not in a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return Path(top) if out.returncode == 0 and top else None


def _default_worktree_path(toplevel: Path, branch: str) -> Path:
    """Default worktree dir: ``<repo-parent>/<branch-leaf>`` (slashes → leaf)."""
    leaf = branch.rstrip("/").split("/")[-1] or "worktree"
    return toplevel.parent / leaf


def _create_worktree(toplevel: Path, target: Path, branch: str, *, new_branch: bool) -> str | None:
    """Run ``git worktree add``. Returns ``None`` on success, else an error string."""
    cmd = ["git", "-C", str(toplevel), "worktree", "add"]
    if new_branch:
        cmd += ["-b", branch, str(target)]
    else:
        cmd += [str(target), branch]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if out.returncode != 0:
        return out.stderr.strip() or out.stdout.strip() or "git worktree add failed"
    return None


def _run(args: argparse.Namespace) -> int:
    harness: str = args.harness
    if REGISTRY.get(harness) is None:
        print(f"ERROR: Unknown harness: {harness}.", file=sys.stderr)
        print(f"FIX:   Choices: {', '.join(sorted(REGISTRY))}.", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()
    toplevel = _repo_toplevel(cwd)
    if toplevel is None:
        print(f"ERROR: Not inside a git repository: {cwd}.", file=sys.stderr)
        print("FIX:   Run this from within the repo you want a worktree of.", file=sys.stderr)
        return 1

    target = (
        Path(args.path).resolve() if args.path else _default_worktree_path(toplevel, args.branch)
    )
    if target.exists():
        print(f"ERROR: Worktree path already exists: {target}.", file=sys.stderr)
        print("FIX:   Pass --path to choose a different directory.", file=sys.stderr)
        return 1

    err = _create_worktree(toplevel, target, args.branch, new_branch=args.new_branch)
    if err is not None:
        print(f"ERROR: git worktree add failed: {err}", file=sys.stderr)
        print(
            "FIX:   Pass -b to create a new branch, or check the branch/path is valid.",
            file=sys.stderr,
        )
        return 1

    port = add.resolve_port(args.port)
    upstream, result, phase_seeded = add.adopt_and_wire(
        harness,
        target.resolve(),
        port=port,
        upstream_url=args.upstream_url,
        upstream_model=args.upstream_model,
        key_env=args.key_env,
    )

    _render(harness, args.branch, target, upstream, result, phase_seeded)
    return 0


def _render(
    harness: str,
    branch: str,
    target: Path,
    upstream: Upstream | None,
    result: dict[str, Any],
    phase_seeded: str | None,
) -> None:
    """Human-readable summary of the worktree + wiring."""
    print(f"[AgentAlloy] worktree {harness}  branch={branch}")
    print(f"  worktree: {target}")
    if upstream is not None:
        key_note = f"  key_env={upstream.key_env}" if upstream.key_env else "  (no key)"
        print(f"  upstream: {upstream.url}  model={upstream.model}{key_note}")
    else:
        print("  upstream: (none adopted — auth-transparent or global fallback)")
    touched = [*(result.get("files_written") or []), *(result.get("files_modified") or [])]
    for f in touched:
        print(f"  wired: {f.get('path')}")
    if phase_seeded:
        print(f"  phase: {phase_seeded} (worktree activated; isolated from the main checkout)")
    print(f"  next:  cd {target}")
