"""Install CLI dispatcher.

Usage::

    python -m agentalloy.install <subcommand> [args]

Exit codes
----------
0  success
1  user-correctable failure (precondition not met)
2  system failure (unexpected exception)
3  schema-version mismatch
4  already-completed (idempotent skip / no-op)
"""

from __future__ import annotations

import argparse
import sys

from agentalloy.install.subcommands import (
    add,
    approve,
    cleanup,
    code,
    compose,
    contract,
    customize,
    detect,
    doctor,
    enable_service,
    install_pack,
    install_packs,
    new_skill_pack,
    phase,
    preflight,
    profile,
    pull_models,
    pull_web,
    recommend_host_targets,
    recommend_models,
    reembed,
    rerank_warmup,
    reset,
    reset_step,
    seed_corpus,
    serve,
    server_restart,
    server_start,
    server_status,
    server_stop,
    simple_setup,
    start_embed_server,
    start_rerank_server,
    status,
    statusline,
    task,
    telemetry,
    uninstall,
    unwire,
    update,
    upgrade,
    validate_pack,
    verify,
    verify_pack,
    wire,
    wire_harness,
    worktree,
    wrap,
    write_env,
)
from agentalloy.install.subcommands import (
    watch as watch_cmd,
)

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYSTEM = 2
EXIT_SCHEMA = 3
EXIT_NOOP = 4

_SUBCOMMANDS = [
    # User-facing verbs first — these are what end users typically run.
    preflight,
    simple_setup,
    add,
    worktree,
    profile,
    customize,
    new_skill_pack,
    validate_pack,
    reset,
    contract,
    compose,
    code,
    watch_cmd,
    wire,
    unwire,
    serve,
    server_start,
    server_stop,
    server_restart,
    server_status,
    enable_service,
    status,
    statusline,
    task,
    approve,
    cleanup,
    # Underlying step subcommands (still available for power-users + the
    # runbook LLM that drives them individually).
    detect,
    recommend_host_targets,
    recommend_models,
    seed_corpus,
    pull_models,
    pull_web,
    start_embed_server,
    start_rerank_server,
    rerank_warmup,
    write_env,
    wire_harness,
    verify,
    doctor,
    wrap,
    phase,
    uninstall,
    reset_step,
    update,
    upgrade,
    install_pack,
    install_packs,
    reembed,
    verify_pack,
    telemetry,
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentalloy.install",
        description="AgentAlloy installer CLI.",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress non-error output (silent mode).",
    )
    from agentalloy import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"agentalloy {__version__}",
        help="Print the installed agentalloy version and exit.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    for mod in _SUBCOMMANDS:
        mod.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand is None:
        parser.print_help(sys.stderr)
        return EXIT_USER

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
