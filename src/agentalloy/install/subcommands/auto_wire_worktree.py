"""``agentalloy auto-wire-worktree`` — invoked by the post-checkout git hook.

Not a command users run directly (hidden from ``--help``; see
``agentalloy.install.git_hooks``). Detects "this cwd is a freshly created,
not-yet-wired linked worktree of an already-wired repo" and replicates that
repo's wiring (harness + upstream + lifecycle mode) into it automatically, so
a new worktree composes without a manual ``agentalloy worktree``/``add`` run.

Runs from inside a git hook chained to ``git worktree add``/``git checkout``,
so every path here is soft-fail: an exception must never propagate and make
the checkout itself fail. ``run_auto_wire_worktree`` always returns 0.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_S = 5.0


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    """Register the (hidden) ``auto-wire-worktree`` subcommand.

    ``help=argparse.SUPPRESS`` alone renders a literal ``==SUPPRESS==`` line in
    ``--help`` for subparsers (unlike regular arguments, where it fully hides
    the entry) — a known argparse quirk. Stripping our pseudo-action from the
    subparsers action's internal ``_choices_actions`` list removes the
    "subcommands:" listing line entirely; the command remains fully
    parseable/callable (still present in ``choices``), it's just not
    advertised to users browsing ``--help``.
    """
    p = subparsers.add_parser(
        "auto-wire-worktree",
        help=argparse.SUPPRESS,  # internal — invoked by the post-checkout hook only
    )
    p.set_defaults(func=_run)
    subparsers._choices_actions = [  # pyright: ignore[reportPrivateUsage]
        a
        for a in subparsers._choices_actions
        if a.dest != "auto-wire-worktree"  # pyright: ignore[reportPrivateUsage]
    ]


def _git_common_dir(root: Path) -> Path | None:
    """Absolute path to *root*'s common git dir (shared across worktrees)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    val = out.stdout.strip()
    return Path(val) if out.returncode == 0 and val else None


def _main_checkout_root(cwd: Path) -> Path | None:
    """The original checkout's root for *cwd*, or ``None`` if unresolvable.

    A linked worktree's ``.git`` is a *file* pointing at
    ``<main>/.git/worktrees/<name>``; ``--git-common-dir`` always resolves to
    the main checkout's real ``.git`` directory regardless of which worktree
    it's run from, and that directory's parent is the main working tree root.
    Returns ``None`` when *cwd* IS the main checkout (nothing to replicate
    from itself) or the common dir can't be resolved (not a git repo, or an
    unusual separate ``--git-dir``/``--work-tree`` layout).
    """
    common = _git_common_dir(cwd)
    if common is None or common.name != ".git":
        return None
    main_root = common.parent
    return None if main_root == cwd else main_root


def run_auto_wire_worktree(cwd: Path | None = None) -> int:
    """Auto-wire *cwd* if it's an unwired worktree of an already-wired repo.

    Always returns 0 (soft-fail throughout) — this runs from a git hook and
    must never fail the checkout it's chained to, nor spam output for the
    overwhelmingly common case (an ordinary checkout that isn't a fresh,
    unwired worktree).
    """
    cwd = (cwd or Path.cwd()).resolve()
    with contextlib.suppress(Exception):  # a git hook must never propagate
        _try_auto_wire(cwd)
    return 0


def _try_auto_wire(cwd: Path) -> None:
    if (cwd / ".agentalloy" / "phase").exists() or (cwd / ".agentalloy" / "upstream").exists():
        return  # already wired — nothing to do

    main_root = _main_checkout_root(cwd)
    if main_root is None or not (main_root / ".agentalloy").exists():
        return  # not a worktree, or the main checkout was never wired

    from agentalloy.install import state as install_state
    from agentalloy.install.subcommands.add import adopt_and_wire, resolve_port
    from agentalloy.install.subcommands.uninstall import (
        _harnesses_in_repo,  # pyright: ignore[reportPrivateUsage]
    )
    from agentalloy.signals.skill_loader import (
        _read_lifecycle_mode,  # pyright: ignore[reportPrivateUsage]
    )

    st = install_state.load_state()
    entries: list[dict[str, Any]] = st.get("harness_files_written") or []
    harnesses = sorted(_harnesses_in_repo(entries, main_root))
    if not harnesses:
        return

    upstream_url = upstream_model = key_env = None
    upstream_file = main_root / ".agentalloy" / "upstream"
    if upstream_file.exists():
        import yaml

        data = yaml.safe_load(upstream_file.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            upstream_url = data.get("url")
            upstream_model = data.get("model")
            key_env = data.get("key_env")

    lifecycle_mode = _read_lifecycle_mode(main_root)
    port = resolve_port(None)

    for harness in harnesses:
        adopt_and_wire(
            harness,
            cwd,
            port=port,
            upstream_url=upstream_url,
            upstream_model=upstream_model,
            key_env=key_env,
            lifecycle_mode=lifecycle_mode,
        )
        print(f"[AgentAlloy] auto-wired {harness} for this worktree (from {main_root})")


def _run(args: argparse.Namespace) -> int:
    del args
    return run_auto_wire_worktree()
