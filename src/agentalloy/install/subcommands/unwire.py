"""``unwire`` verb — remove AgentAlloy sentinels from the current repo.

Per-repo cleanup by default. Walks ``harness_files_written`` entries and removes
the sentinels/dedicated files that belong to the cwd-derived repo — repo-local
files and the user-scope harness configs (~/.claude, ~/.agentalloy, ...) that were
recorded for *this* repo. Entries from other repos, and shared user-scope configs
recorded for another repo, are left alone so unwiring one repo never unwires the
rest. Does NOT touch user-scope state directories, ``.env``, the corpus, or
services.

``--all`` widens the walk to every repo's wiring (and all user-scope harness
configs), matching the historical behavior. ``uninstall`` is still the full
user-scope teardown (state + corpus + .env + services).
"""

from __future__ import annotations

import argparse

from agentalloy.install.output import add_json_flag, render_lifecycle_result, write_result
from agentalloy.install.subcommands.uninstall import uninstall


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "unwire",
        help="Remove AgentAlloy sentinels from the current repo (keeps user state).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Force removal even when sentinel content has been edited.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="all_repos",
        help="Unwire every repo's harness wiring (and shared user-scope configs), "
        "not just the current repo. Still keeps user state, .env, corpus, and services.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    # `unwire` is per-repo by default: remove sentinels for entries belonging to the
    # cwd-derived repo, leave the user-scope state and `.env` untouched. `--all`
    # (args.all_repos) widens the harness walk to every repo + shared user-scope
    # configs. `remove_user_state=False` and `remove_env=False` skip the user-scope
    # teardown branches in `uninstall()` in both modes; the sentinel work is the same
    # as a full uninstall otherwise.
    result = uninstall(
        remove_data=False,
        force=args.force,
        remove_user_state=False,
        remove_env=False,
        all_repos=getattr(args, "all_repos", False),
        # `unwire` is sentinel-only: keep services running, keep models,
        # keep all user-scope state. The new explicit kwargs preserve
        # this behavior independently of how the meta-uninstall defaults
        # evolve.
        stop_services=False,
        remove_models=False,
        remove_wiring=True,
    )
    write_result(result, args, human_fn=lambda r: render_lifecycle_result(r, "Unwire"))
    return 0
