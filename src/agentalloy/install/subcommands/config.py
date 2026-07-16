"""``config`` subcommand for managing agentalloy configuration via the .env file.

    agentalloy config status
    agentalloy config enable|disable <feature>

Toggles are targeted KEY=VALUE upserts into the user-scoped ``.env``
(``install.state.upsert_env_file``) — comments, ordering, and unrelated keys
are preserved verbatim, matching the same helper the web UI's ``/api/config``
uses, not a full-file regeneration.

There is no separate ``knowledge-graph`` feature here: the Knowledge module
(decision-graph linkage, ``agentalloy knowledge why``) rides the same router
and store as ``code-index`` with no independent runtime toggle, so
``code-index`` covers both.
"""

from __future__ import annotations

import argparse
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

# Mapping of feature names to their environment variable names.
# This matches the mapping used in src/agentalloy/config.py
# and src/agentalloy/install/subcommands/write_env.py
_FEATURE_TO_ENV = {
    "code-index": "CODE_INDEX_ENABLED",
}


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p: argparse.ArgumentParser = subparsers.add_parser(
        "config",
        help="Manage agentalloy configuration via the user-scoped .env file.",
    )
    add_json_flag(p)
    p.set_defaults(func=run)

    config_subparsers = p.add_subparsers(dest="config_subcommand", required=True)

    status_p = config_subparsers.add_parser(
        "status",
        help="Show current configuration values from the .env file.",
    )
    add_json_flag(status_p)

    enable_p = config_subparsers.add_parser(
        "enable",
        help="Set a feature to enabled (True).",
    )
    add_json_flag(enable_p)
    enable_p.add_argument(
        "feature",
        choices=sorted(_FEATURE_TO_ENV.keys()),
        help="The feature to enable.",
    )

    disable_p = config_subparsers.add_parser(
        "disable",
        help="Set a feature to disabled (False).",
    )
    add_json_flag(disable_p)
    disable_p.add_argument(
        "feature",
        choices=sorted(_FEATURE_TO_ENV.keys()),
        help="The feature to disable.",
    )


def _render_status(result: dict[str, Any]) -> None:
    print_rich("\n  [bold]Current Configuration (from .env)[/bold]\n")
    for feature, env_var in sorted(_FEATURE_TO_ENV.items()):
        print_rich(f"  {feature}: {result[env_var]}")
    print_rich()


def _render_toggle(result: dict[str, Any]) -> None:
    if result["changed"]:
        print_rich(f"  [green]{result['env_var']}={result['value']}[/green]")
    else:
        print_rich(f"  [dim]{result['feature']} is already {result['value'].lower()}.[/dim]")


def run(args: argparse.Namespace) -> int:
    """Execute the config subcommand."""
    env_vars = install_state.parse_env_file()

    if args.config_subcommand == "status":
        result = {env_var: env_vars.get(env_var, "False") for env_var in _FEATURE_TO_ENV.values()}
        write_result(result, args, human_fn=_render_status)
        return 0

    feature_name: str = args.feature
    env_var = _FEATURE_TO_ENV[feature_name]
    new_value = "True" if args.config_subcommand == "enable" else "False"

    changed = env_vars.get(env_var) != new_value
    if changed:
        install_state.upsert_env_file({env_var: new_value})

    write_result(
        {"feature": feature_name, "env_var": env_var, "value": new_value, "changed": changed},
        args,
        human_fn=_render_toggle,
    )
    return 0
