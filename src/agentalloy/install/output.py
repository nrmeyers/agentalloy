"""Shared CLI output utilities for human-readable formatting.

Provides Rich-based rendering with graceful fallback to plain print()
when Rich is unavailable, plus helpers for common output patterns used
across install subcommands.

Flag priority: --quiet (suppresses all) > --json (raw JSON) > default (human)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table

    # Auto-detect terminal: force only when stdout is a real TTY so piped
    # output stays clean of ANSI codes while interactive sessions get
    # full Rich formatting.
    _console = Console(force_terminal=sys.stdout.isatty(), soft_wrap=True)
    HAS_RICH: bool = True
except ImportError:
    Console = None  # type: ignore[misc,assignment]
    Progress = None  # type: ignore[misc,assignment]
    SpinnerColumn = None  # type: ignore[misc,assignment]
    TextColumn = None  # type: ignore[misc,assignment]
    TimeElapsedColumn = None  # type: ignore[misc,assignment]
    Table = None  # type: ignore[misc,assignment]
    _console = None  # type: ignore[assignment]
    HAS_RICH = False  # type: ignore[possibly-unused-assignment]


# ---------------------------------------------------------------------------
# Core output helpers
# ---------------------------------------------------------------------------


def print_rich(*args: Any, **kwargs: Any) -> None:
    """Print with Rich if available, plain stdout otherwise.

    When Rich is unavailable, Rich markup tags (e.g. [bold], [green])
    are stripped so the fallback output remains clean.
    """
    if HAS_RICH and _console is not None:
        _console.print(*args, **kwargs)
    else:
        # Strip Rich markup tags for clean plain-text fallback
        stripped = [_strip_markup(str(a)) for a in args]
        print(*stripped, **kwargs)


# ---------------------------------------------------------------------------
# Rich markup stripper for non-Rich fallback
# ---------------------------------------------------------------------------

_markup_re = re.compile(
    r"\[(/?)?(bold|dim|red|green|yellow|blue|magenta|cyan|white|black|default|link|on\s+\w+|default|link\s+\S+|[a-z_]+)\]"
)


def _strip_markup(text: str) -> str:
    """Remove Rich markup tags from a string for plain-text output."""
    return _markup_re.sub("", text)


def print_rich_stderr(*args: Any, **kwargs: Any) -> None:
    """Print to stderr with Rich if available, plain stderr otherwise."""
    if HAS_RICH and _console is not None:
        err_console = Console(force_terminal=True, soft_wrap=True, file=sys.stderr)  # type: ignore[union-attr]
        err_console.print(*args, **kwargs)
    else:
        print(*args, file=sys.stderr, **kwargs)


# ---------------------------------------------------------------------------
# Live activity spinner (long, output-silent steps)
# ---------------------------------------------------------------------------


@contextmanager
def progress_activity(message: str, *, enabled: bool = True) -> Iterator[None]:
    """Show a live spinner + elapsed timer while a long, output-silent step runs.

    Wrap a blocking call whose own output is captured (a package swap, a pack
    ingest, a corpus migration) so the terminal keeps animating instead of
    looking hung. Rich's ``Progress`` refreshes on its own background thread,
    so the spinner spins and the elapsed clock keeps ticking even while the
    main thread is blocked inside ``subprocess.run`` — visible proof the step
    is still working rather than wedged.

    No-op when ``enabled`` is False, Rich is unavailable, or stdout isn't a
    real terminal (so ``--json``/``--quiet`` and piped runs stay byte-clean).
    In that no-op path it prints a single ``-> message…`` line so non-TTY logs
    still record what is running.
    """
    if not (enabled and HAS_RICH and _console is not None and _console.is_terminal):
        if enabled:
            print_rich(f"  [dim]-> {message}…[/dim]")
        yield
        return

    progress = Progress(  # type: ignore[union-attr]
        SpinnerColumn(),  # type: ignore[union-attr]
        TextColumn("[dim]{task.description}[/dim]"),  # type: ignore[union-attr]
        TimeElapsedColumn(),  # type: ignore[union-attr]
        console=_console,
        transient=True,
    )
    with progress:
        progress.add_task(message, total=None)
        yield


# ---------------------------------------------------------------------------
# --json flag + output dispatcher
# ---------------------------------------------------------------------------


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add --json flag to a parser for machine-readable output."""
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output raw JSON instead of human-readable text.",
    )


def should_output_json(args: argparse.Namespace) -> bool:
    """Check if --json is set (and --quiet is not)."""
    return getattr(args, "json", False) and not getattr(args, "quiet", False)


def should_output_human(args: argparse.Namespace) -> bool:
    """Check if we should emit human-readable output (no --quiet, no --json)."""
    return not getattr(args, "quiet", False) and not getattr(args, "json", False)


def write_result(
    result: dict[str, Any] | list[Any],
    args: argparse.Namespace,
    human_fn: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Write result to stdout respecting --json and --quiet flags.

    Priority: --quiet suppresses all stdout, --json forces raw JSON,
    default calls human_fn for human-readable output.
    """
    if getattr(args, "quiet", False):
        return
    if getattr(args, "json", False):
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif human_fn and isinstance(result, dict):
        human_fn(result)
    elif human_fn:
        human_fn(result)  # type: ignore[arg-type]
    else:
        # Fallback: raw JSON
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Lifecycle renderer (wire / unwire / update / reset — file-mutation verbs)
# ---------------------------------------------------------------------------


def render_lifecycle_result(result: dict[str, Any], title: str) -> None:
    """Concise human render for install-lifecycle verbs.

    Prints a title plus only the keys a given verb produced — file actions
    (written +, modified ~, removed -), kept data, a one-line summary for
    common scalar/list fields (harness, profile, migrations), and any
    warnings/errors. Verbs that lack a key simply skip it. Full fidelity is
    always available via ``--json``.
    """
    print_rich(f"\n  [bold]{title}[/bold]")

    def _path(item: Any) -> str:
        return str(item.get("path")) if isinstance(item, dict) else str(item)

    def _files(key: str, marker: str, color: str) -> None:
        for item in result.get(key) or []:
            print_rich(f"    [{color}]{marker}[/{color}] {_path(item)}")

    _files("files_written", "+", "green")
    _files("files_modified", "~", "yellow")
    _files("files_removed", "-", "red")

    for kept in result.get("data_kept") or []:
        print_rich(f"    [dim]kept[/dim]   {_path(kept)}")

    if result.get("harness"):
        print_rich(f"    harness: {result['harness']}")
    if result.get("profile"):
        print_rich(f"    profile: {result['profile']}")
    overrides = result.get("deleted_overrides")
    if overrides is not None:
        n = len(overrides) if isinstance(overrides, list) else overrides
        print_rich(f"    deleted {n} override(s)")
    if result.get("reingested_defaults"):
        print_rich("    re-ingested profile defaults")
    migrations = result.get("migrations")
    if migrations is not None:
        applied = sum(1 for m in migrations if isinstance(m, dict) and m.get("applied"))
        print_rich(f"    migrations: {applied}/{len(migrations)} applied")
    if result.get("cancelled"):
        print_rich("    [dim]cancelled[/dim]")

    for warning in result.get("warnings") or []:
        print_rich(f"    [yellow]![/yellow] {warning}")
    errors = [result["error"]] if result.get("error") else (result.get("errors") or [])
    for err in errors:
        print_rich(f"    [red]error[/red] {err}")

    summarized = (
        any(
            result.get(k)
            for k in ("files_written", "files_modified", "files_removed", "reingested_defaults")
        )
        or migrations is not None
        or overrides is not None
    )
    if not summarized and not errors and not result.get("cancelled"):
        print_rich("    [dim]no changes[/dim]")


# ---------------------------------------------------------------------------
# Checklist renderer (doctor / verify / preflight)
# ---------------------------------------------------------------------------


def render_checklist(
    result: dict[str, Any],
    title: str = "Results",
    *,
    severity_field: str = "severity",
) -> None:
    """Render a check result with PASS/FAIL markers.

    Supports optional severity levels: 'fatal' (red), 'warn' (yellow),
    or no severity (standard green/red).
    """
    checks = result.get("checks", [])
    all_passed = result.get("all_checks_passed", True)

    print_rich(f"\n  [bold]{title}[/bold]\n")

    passed = sum(1 for c in checks if c.get("passed"))
    failed = len(checks) - passed

    for check in checks:
        name = check.get("name", "unknown")
        severity = check.get(severity_field, "")

        if check.get("passed"):
            print_rich(f"  [green]PASS[/green] {name}")
            detail = check.get("detail", "")
            if detail:
                print_rich(f"         {detail}")
        else:
            if severity == "warn":
                marker = "[yellow]WARN[/yellow]"
            elif severity == "fatal":
                marker = "[red]FAIL[/red]"
            else:
                marker = "[red]FAIL[/red]"
            print_rich(f"  {marker} {name}")
            error = check.get("error", "unknown")
            print_rich(f"         {error}")
            remediation = check.get("remediation", "")
            if remediation:
                for line in str(remediation).splitlines():
                    if line.strip():
                        print_rich(f"         [dim]FIX: {line}[/dim]")

    print_rich()
    if all_passed:
        print_rich(f"  [green]All {len(checks)} checks passed.[/green]\n")
    else:
        print_rich(f"  [red]{failed} of {len(checks)} checks failed.[/red]\n")


# ---------------------------------------------------------------------------
# Key-value renderer (status / server-status)
# ---------------------------------------------------------------------------


def render_key_value(
    result: dict[str, Any],
    title: str = "Status",
    *,
    skip_keys: set[str] | None = None,
) -> None:
    """Render a simple key-value result as a formatted list."""
    skip = skip_keys or set()

    print_rich(f"\n  [bold]{title}[/bold]\n")

    for key, value in result.items():
        if key in skip:
            continue
        if isinstance(value, dict):
            for k, v in value.items():  # type: ignore[reportUnknownVariableType]
                print_rich(f"  {k}: {v}")
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                for item in value:  # type: ignore[reportUnknownVariableType]
                    print_rich(f"  - {item}")
            else:
                print_rich(f"  {key}: {value}")
        else:
            print_rich(f"  {key}: {value}")

    print_rich()


# ---------------------------------------------------------------------------
# Action result renderer (server-stop, start-embed-server, etc.)
# ---------------------------------------------------------------------------

ACTION_COLORS = {
    "started": "green",
    "stopped": "yellow",
    "already_running": "dim",
    "already_stopped": "dim",
    "seeded": "green",
    "verified_present": "dim",
    "ingested": "green",
    "wired": "green",
    "unwired": "yellow",
    "enabled": "green",
    "disabled": "yellow",
    "initialized_empty": "dim",
}

# Common keys for each category, ordered for consistent output
SERVER_KEYS = ("runner", "model", "port", "pid", "signal")
CORPUS_KEYS = ("skill_count", "fragment_count", "action", "duration_ms")
WIRE_KEYS = ("harness", "files_written", "files_modified")
SERVICE_KEYS = ("mode", "runtime", "unit")


def render_action_result(
    result: dict[str, Any],
    title: str = "Result",
    *,
    key_groups: tuple[tuple[str, ...], ...] | None = None,
) -> None:
    """Render an action result (started/stopped/seeded etc.) with key fields."""
    action = result.get("action", "unknown")
    color = ACTION_COLORS.get(action, "white")

    print_rich(f"\n  [bold]{title}[/bold]\n")
    print_rich(f"  Action: [bold {color}]{action}[/bold {color}]")

    # Render key groups in order
    all_keys = key_groups or (SERVER_KEYS, CORPUS_KEYS, WIRE_KEYS, SERVICE_KEYS)
    shown: set[str] = set()
    for key_tuple in all_keys:
        for key in key_tuple:
            if key in result and key not in shown:
                shown.add(key)
                val = result[key]
                if isinstance(val, list) and len(val) == 0:  # type: ignore[arg-type]
                    continue
                print_rich(f"  {key}: {val}")

    # Any remaining keys not in predefined groups
    for key, val in result.items():
        if key in ("action",) or key in shown:
            continue
        if isinstance(val, list) and len(val) == 0:  # type: ignore[arg-type]
            continue
        if isinstance(val, dict):
            for k, v in val.items():  # type: ignore[reportUnknownVariableType]
                print_rich(f"  {k}: {v}")
        else:
            print_rich(f"  {key}: {val}")

    print_rich()


# ---------------------------------------------------------------------------
# Table renderer (customize list, recommend-models)
# ---------------------------------------------------------------------------


def render_table(
    headers: list[str],
    rows: list[list[str]],
    title: str = "",
) -> None:
    """Render a table using Rich if available, plain text otherwise."""
    if title:
        print_rich(f"\n  [bold]{title}[/bold]\n")

    if HAS_RICH:
        table = Table(show_header=True, header_style="bold", box=None)  # type: ignore[union-attr]
        for h in headers:
            table.add_column(h)
        for row in rows:
            table.add_row(*row)
        _console.print(table)  # type: ignore[union-attr]
    else:
        # Plain text alignment
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))
        header_line = "  " + "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
        print(header_line)
        print("  " + "-".join("-" * (w + 2) for w in col_widths))
        for row in rows:
            print("  " + "  ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)))

    print_rich()


# ---------------------------------------------------------------------------
# Model recommendation renderer
# ---------------------------------------------------------------------------


def render_model_recommendations(result: dict[str, Any]) -> None:
    """Render model recommendations in a compact format."""
    preset = result.get("preset", "unknown")
    options = result.get("options", [])

    print_rich("\n  [bold]Recommended Models[/bold]\n")
    print_rich(f"  Preset: {preset}")

    for opt in options:
        is_default = opt.get("default", False)
        default_tag = "[default] " if is_default else ""
        model = opt.get("embed_model", "unknown")
        runner = opt.get("embed_runner", "unknown")
        print_rich(f"  {default_tag}[bold]{runner}[/bold] / {model}")

    print_rich()


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "HAS_RICH",
    "add_json_flag",
    "should_output_json",
    "should_output_human",
    "write_result",
    "print_rich",
    "print_rich_stderr",
    "progress_activity",
    "render_checklist",
    "render_key_value",
    "render_action_result",
    "render_table",
    "render_model_recommendations",
]
