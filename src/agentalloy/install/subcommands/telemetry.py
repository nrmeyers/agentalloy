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

    # clear() writes, so it needs the read-write lock. If the service is up it
    # holds that lock — a direct open would throw a raw DuckDB IOException. Give
    # actionable guidance instead.
    from agentalloy.install import server_proc

    if server_proc.port_reachable(_service_port()):
        print(
            "ERROR: the agentalloy service is running and holds the telemetry DB lock.",
            file=sys.stderr,
        )
        print(
            "FIX:   stop it first, then retry: `agentalloy server-stop` "
            "(or `systemctl --user stop agentalloy`).",
            file=sys.stderr,
        )
        return 1

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


def _service_port() -> int:
    """Resolve the configured service port from user-scope state (fallback 47950)."""
    from agentalloy.install import state as install_state

    return install_state.validate_port(install_state.load_state().get("port", 47950))


def _fetch_savings_via_api(port: int) -> dict[str, Any] | None:
    """GET /telemetry/savings from the running service; None on any failure.

    Returns the same dict shape as ``VectorStore.aggregate_savings()`` so the
    existing renderer works unchanged.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/telemetry/savings"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (localhost only)
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run_savings(args: argparse.Namespace) -> int:
    """Print token-savings aggregation.

    When the service is up it holds the single read-write DuckDB lock, so a
    direct file open would conflict. Route through the service API in that case;
    fall back to a direct read only when the service is down (offline diagnostics).
    """
    from agentalloy.install import server_proc

    port = _service_port()
    if server_proc.port_reachable(port):
        result = _fetch_savings_via_api(port)
        if result is not None:
            write_result(result, args, human_fn=_render_savings)
            return 0
        # Port is open but the API didn't answer (e.g. an older service without
        # this endpoint). Don't attempt a direct open — it would hit the lock.
        print(
            "ERROR: the agentalloy service is running but its /telemetry/savings API "
            "did not respond (older version?).",
            file=sys.stderr,
        )
        print(
            "FIX:   restart it to pick up this endpoint: `agentalloy server-restart` "
            "(or `systemctl --user restart agentalloy`).",
            file=sys.stderr,
        )
        return 1

    # Service is down — safe to open the corpus directly.
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
