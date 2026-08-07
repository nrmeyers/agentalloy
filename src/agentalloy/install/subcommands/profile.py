# pyright: reportPrivateUsage=false
"""``agentalloy profile`` — profile management subcommand.

Commands:
    agentalloy profile list                       — list all configured profiles
    agentalloy profile current                    — show active profile for cwd
    agentalloy profile init <name>                — create a new profile
    agentalloy profile set-default <name>         — change the fallback default
    agentalloy profile delete <name>              — remove a profile
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agentalloy.install.output import add_json_flag, print_rich, write_result


def _render_profile_list(profiles: list[dict[str, Any]]) -> None:
    """Render profile list in human-readable format."""
    print_rich("\n  [bold]Profiles[/bold]\n")
    for p in profiles:
        active = " *" if p["active_for_cwd"] else ""
        print_rich(f"  {p['name']}{active}")
        if p.get("match_remote"):
            print_rich(f"    match_remote: {p['match_remote']}")
        if p.get("match_path"):
            print_rich(f"    match_path: {p['match_path']}")
        print_rich(f"    has_overrides: {p['has_overrides']}")
    print_rich()


def _list(args: argparse.Namespace) -> int:
    from agentalloy.profiles import list_profiles

    profiles = list_profiles(cwd=Path.cwd())
    write_result(profiles, args, human_fn=lambda r: _render_profile_list(r))  # type: ignore[arg-type]
    return 0


def _render_current(result: dict[str, Any]) -> None:
    """Render current profile in human-readable format."""
    print_rich("\n  [bold]Current Profile[/bold]\n")
    print_rich(f"  Profile: [bold]{result['name']}[/bold]")
    print_rich(f"  Datastore: {result['datastore_path']}")
    print_rich(f"  Skills: {result['skills_dir']}")
    print_rich(f"  Default: {result['is_default']}")
    print_rich()


def _current(args: argparse.Namespace) -> int:
    from agentalloy.profiles import detect_profile

    profile = detect_profile(cwd=Path.cwd())
    result: dict[str, Any] = {
        "name": profile.name,
        "datastore_path": str(profile.datastore_path),
        "skills_dir": str(profile.skills_dir),
        "is_default": profile.is_default,
    }
    write_result(result, args, human_fn=_render_current)
    return 0


def _render_init(result: dict[str, Any]) -> None:
    """Render profile init result in human-readable format."""
    print_rich("\n  [bold]Profile Init[/bold]\n")
    print_rich(f"  Profile: [bold]{result['name']}[/bold]")
    print_rich(f"  Skills: {result['skills_dir']}")
    if result.get("match_remote"):
        print_rich(f"  Match remote: {result['match_remote']}")
    if result.get("match_path"):
        print_rich(f"  Match path: {result['match_path']}")
    print_rich()


def _init(args: argparse.Namespace) -> int:
    from agentalloy.profiles import init_profile

    name: str = args.name
    match_remote: list[str] | None = getattr(args, "match_remote", None) or None
    match_path: list[str] | None = getattr(args, "match_path", None) or None

    # Interactive prompts when running in a TTY and no patterns were given.
    if sys.stdin.isatty() and not args.non_interactive:
        if not match_remote:
            raw = input("  Match git remote pattern (leave blank to skip): ").strip()
            if raw:
                match_remote = [raw]
        if not match_path:
            raw = input("  Match path pattern (leave blank to skip): ").strip()
            if raw:
                match_path = [raw]

    try:
        profile = init_profile(name, match_remote=match_remote, match_path=match_path)
    except (ValueError, KeyError) as exc:
        print(f"  [error] {exc}", file=sys.stderr)
        return 1

    result: dict[str, Any] = {
        "name": profile.name,
        "datastore_path": str(profile.datastore_path),
        "skills_dir": str(profile.skills_dir),
        "match_remote": match_remote or [],
        "match_path": match_path or [],
    }
    write_result(result, args, human_fn=_render_init)
    return 0


def _render_set_default(result: dict[str, Any]) -> None:
    """Render set default profile result in human-readable format."""
    print_rich("\n  [bold]Set Default Profile[/bold]\n")
    print_rich(f"  Default: [bold]{result['default_profile']}[/bold]")
    print_rich()


def _set_default(args: argparse.Namespace) -> int:
    from agentalloy.profiles import set_default_profile

    try:
        set_default_profile(args.name)
    except (ValueError, KeyError) as exc:
        print(f"  [error] {exc}", file=sys.stderr)
        return 1

    result = {"default_profile": args.name}
    write_result(result, args, human_fn=_render_set_default)
    return 0


def _render_delete(result: dict[str, Any]) -> None:
    """Render profile delete result in human-readable format."""
    print_rich("\n  [bold]Delete Profile[/bold]\n")
    print_rich(f"  Profile: {result['deleted']}")
    print_rich("  [green]Deleted[/green]")
    print_rich()


def _delete(args: argparse.Namespace) -> int:
    from agentalloy.profiles import delete_profile

    name: str = args.name

    # Confirmation prompt unless --yes passed.
    if not getattr(args, "yes", False) and sys.stdin.isatty():
        confirm = input(f"  Delete profile '{name}' and all its overrides? (yes/n): ").strip()
        if confirm.lower() != "yes":
            print("  Cancelled.")
            return 0

    try:
        delete_profile(name)
    except (ValueError, KeyError) as exc:
        print(f"  [error] {exc}", file=sys.stderr)
        return 1

    result = {"deleted": name}
    write_result(result, args, human_fn=_render_delete)
    return 0


_PROFILE_SUBCOMMANDS = {
    "list": _list,
    "current": _current,
    "init": _init,
    "set-default": _set_default,
    "delete": _delete,
}


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = subparsers.add_parser("profile", help="Manage agentalloy profiles.")
    add_json_flag(p)
    p.add_argument("--non-interactive", action="store_true", dest="non_interactive", default=False)
    sub = p.add_subparsers(dest="profile_cmd")

    # list
    sub.add_parser("list", help="List all configured profiles.")

    # current
    sub.add_parser("current", help="Show the active profile for the current directory.")

    # init
    init_p = sub.add_parser("init", help="Create a new profile.")
    init_p.add_argument("name", help="Profile name.")
    init_p.add_argument(
        "--match-remote",
        dest="match_remote",
        nargs="*",
        help="Git remote URL glob patterns that activate this profile.",
    )
    init_p.add_argument(
        "--match-path",
        dest="match_path",
        nargs="*",
        help="Path glob patterns that activate this profile.",
    )

    # set-default
    sd_p = sub.add_parser("set-default", help="Change the fallback default profile.")
    sd_p.add_argument("name", help="Profile name.")

    # delete
    del_p = sub.add_parser("delete", help="Remove a profile.")
    del_p.add_argument("name", help="Profile name.")
    del_p.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    cmd = getattr(args, "profile_cmd", None)
    if not cmd:
        print("  Usage: agentalloy profile {list,current,init,set-default,delete}", file=sys.stderr)
        return 1
    handler = _PROFILE_SUBCOMMANDS.get(cmd)
    if not handler:
        print(f"  Unknown profile command: {cmd}", file=sys.stderr)
        return 1
    return handler(args)
