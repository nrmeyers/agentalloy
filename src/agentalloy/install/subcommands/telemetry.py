"""``telemetry`` subcommand group — telemetry table management.

Exposes two sub-verbs:

    agentalloy telemetry clear [--confirm]
    agentalloy telemetry savings [--json]

``clear`` deletes ``composition_traces`` and ``prompt_loads`` from the
user-scoped DuckDB without touching ``fragment_embeddings`` (the corpus).

``savings`` aggregates token-savings telemetry from stored compose traces
and prints overall totals plus a per-phase breakdown.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "telemetry",
        help="Telemetry table management (clear, savings, etc.).",
    )
    add_json_flag(p)
    sub = p.add_subparsers(dest="telemetry_verb", metavar="verb")
    sub.required = True

    clear_p = sub.add_parser(
        "clear",
        help="Delete all composition traces and prompt-load records.",
    )
    clear_p.add_argument(
        "--confirm",
        action="store_true",
        help="Skip the interactive confirmation prompt (required in non-TTY environments).",
    )
    clear_p.set_defaults(func=_run_clear)

    savings_p = sub.add_parser(
        "savings",
        help="Show token-savings summary aggregated from compose telemetry.",
    )
    add_json_flag(savings_p)
    savings_p.set_defaults(func=_run_savings)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    return args.func(args)


def _run_clear(args: argparse.Namespace) -> int:
    if not args.confirm:
        if not sys.stdin.isatty():
            print(
                "ERROR: telemetry clear requires --confirm in non-interactive mode.",
                file=sys.stderr,
            )
            return 1
        try:
            answer = (
                input(
                    "This will permanently delete all composition traces and prompt-load "
                    "records from the local DuckDB.\nContinue? [y/N]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 0
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            return 0

    from agentalloy.config import get_settings
    from agentalloy.storage.vector_store import open_or_create

    settings = get_settings()
    vs = open_or_create(settings.duckdb_path)
    try:
        result = vs.clear_telemetry()
    finally:
        vs.close()

    write_result(result, args, human_fn=_render_clear)
    return 0


def _render_clear(result: dict[str, Any]) -> None:
    """Render telemetry clear result in human-readable format."""
    print_rich("\n  [bold]Telemetry Clear[/bold]\n")
    print_rich(f"  Traces deleted: {result['traces_deleted']}")
    print_rich(f"  Prompt loads deleted: {result['prompt_loads_deleted']}")
    print_rich()


def _run_savings(args: argparse.Namespace) -> int:
    """Read composition_traces and print token-savings aggregation."""
    from agentalloy.config import get_settings
    from agentalloy.storage.vector_store import open_or_create

    settings = get_settings()
    vs = open_or_create(settings.duckdb_path)
    try:
        result = vs.aggregate_savings()
    finally:
        vs.close()

    write_result(result, args, human_fn=_render_savings)
    return 0


def _render_savings(result: dict[str, Any]) -> None:
    """Render token-savings aggregation in human-readable format."""
    total = int(result["total_composes"])
    if total == 0:
        print_rich("\n  [bold]Token Savings[/bold]\n")
        print_rich("  No compose traces recorded yet.")
        print_rich()
        return

    tokens_returned = int(result["tokens_returned"])
    tokens_flat = int(result["tokens_flat_equivalent"])
    tokens_saved = int(result["tokens_saved"])
    savings_pct = float(result["savings_pct"])

    print_rich("\n  [bold]Token Savings Summary[/bold]\n")
    print_rich(f"  Total composes:          {total:,}")
    print_rich(f"  Tokens returned:         {tokens_returned:,}")
    print_rich(f"  Flat-injection equiv:    {tokens_flat:,}")
    print_rich(f"  Tokens saved:            {tokens_saved:,}")
    print_rich(f"  Savings:                 {savings_pct:.1f}%")

    per_phase: list[dict[str, Any]] = list(result.get("per_phase") or [])
    if per_phase:
        print_rich()
        print_rich("  [bold]Per-phase breakdown[/bold]")
        print_rich()
        header = f"  {'Phase':<12}  {'Composes':>9}  {'Returned':>10}  {'Flat equiv':>11}  {'Saved':>10}  {'%':>7}"
        print_rich(header)
        print_rich("  " + "-" * (len(header) - 2))
        for row in per_phase:
            ph_flat = int(row["tokens_flat_equivalent"])
            note = "" if ph_flat > 0 else " *"
            print_rich(
                f"  {str(row['phase']):<12}  {int(row['composes']):>9,}  "
                f"{int(row['tokens_returned']):>10,}  {ph_flat:>11,}  "
                f"{int(row['tokens_saved']):>10,}  {float(row['savings_pct']):>6.1f}%{note}"
            )
        if any(int(r["tokens_flat_equivalent"]) == 0 for r in per_phase):
            print_rich()
            print_rich(
                "  * flat-equivalent is 0 for traces recorded before this feature "
                "was deployed or with a non-RuntimeCache source."
            )
    print_rich()
