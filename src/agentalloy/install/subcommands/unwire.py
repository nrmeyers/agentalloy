"""``unwire`` verb — remove AgentAlloy sentinels from the current repo.

Per-repo cleanup. Walks ``harness_files_written`` entries whose
``repo_root`` matches the cwd-derived repo and removes their sentinels
or dedicated files. Does NOT touch user-scope state directories,
``.env``, the corpus, or entries from other repos.

Use ``uninstall`` for a full user-scope teardown (state + corpus + .env).
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
    add_json_flag(p)
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    # `unwire` is per-repo: remove sentinels for entries pointing at the
    # cwd-derived repo, leave the user-scope state and `.env` untouched.
    # `remove_user_state=False` and `remove_env=False` skip the user-scope
    # teardown branches in `uninstall()`; the cwd-only sentinel work is
    # the same as a full uninstall otherwise.
    result = uninstall(
        remove_data=False,
        force=args.force,
        remove_user_state=False,
        remove_env=False,
        all_repos=False,
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
