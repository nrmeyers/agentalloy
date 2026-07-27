"""``agentalloy resume`` — cold-session bootstrap in one command.

Prints phase, the cursor'd work-item, its ``domain_tags``, ``scope``,
owed artifacts, and governing decisions from a single
``GET /state/resume`` round-trip.  The server assembles the payload so
the CLI cannot drift from the proxy's orientation block.

Service down means a non-zero exit naming the service — never a silent
local write or stale output.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agentalloy.api.state_client import StateClient, StateClientError
from agentalloy.install.output import add_json_flag, print_rich, write_result


def _render_resume(result: dict[str, Any]) -> None:
    """Render resume data in human-readable format."""
    phase = result.get("phase")
    cursor_contract = result.get("cursor_contract") or {}
    owed_artifacts = result.get("owed_artifacts") or []
    governing_decisions = result.get("governing_decisions") or []

    print_rich("\n  [bold]Session Resume[/bold]")

    if phase:
        print_rich(f"  Phase: {phase}")
    else:
        print_rich("  Phase: [none]")

    if cursor_contract:
        print_rich("\n  [bold]Work Item[/bold]")
        print_rich(f"  ID: {cursor_contract.get('contract_id', 'N/A')}")
        print_rich(f"  Slug: {cursor_contract.get('slug', 'N/A')}")

        tags = cursor_contract.get("domain_tags") or []
        if tags:
            print_rich(f"  Tags: {', '.join(tags)}")

        touches = cursor_contract.get("scope_touches") or []
        avoids = cursor_contract.get("scope_avoids") or []
        if touches or avoids:
            print_rich("\n  [bold]Scope[/bold]")
            if touches:
                print_rich(f"  Touches: {', '.join(touches)}")
            if avoids:
                print_rich(f"  Avoids: {', '.join(avoids)}")

        body = cursor_contract.get("body")
        if body:
            print_rich(f"\n  [bold]Body[/bold]\n{body}")
    else:
        print_rich("\n  [yellow]No cursor'd work item[/yellow]")

    if owed_artifacts:
        print_rich(f"\n  [bold]Owed Artifacts ({len(owed_artifacts)})[/bold]")
        for artifact in owed_artifacts:
            print_rich(f"  - {artifact}")

    if governing_decisions:
        print_rich(f"\n  [bold]Governing Decisions ({len(governing_decisions)})[/bold]")
        for decision in governing_decisions:
            print_rich(f"  - {decision}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    """Fetch and render resume data from the state service."""
    client = StateClient()
    if not client.is_running():
        print(
            "Error: agentalloy service is not running. "
            "Start the service or run `agentalloy start`.",
            file=sys.stderr,
        )
        return 1

    try:
        data = client.get_resume()
    except StateClientError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return 1

    # The response may be a dict with nested cursor_contract info
    cursor_contract = data.get("cursor_contract")
    if isinstance(cursor_contract, dict):
        # Ensure list fields are proper lists (may come as JSON arrays)
        for key in ("domain_tags", "scope_touches", "scope_avoids"):
            val = cursor_contract.get(key)
            if isinstance(val, str):
                try:
                    cursor_contract[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    cursor_contract[key] = []

    result = {
        "phase": data.get("phase"),
        "cursor_contract": cursor_contract,
        "owed_artifacts": data.get("owed_artifacts") or [],
        "governing_decisions": data.get("governing_decisions") or [],
    }

    write_result(result, args, human_fn=_render_resume)
    return 0


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "resume",
        help="Reconstruct a cold session: phase, work item, scope, artifacts, decisions.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)
