# pyright: reportPrivateUsage=false
"""``telemetry`` subcommand group — telemetry table management.

Exposes three sub-verbs:

    agentalloy telemetry clear [--confirm]
    agentalloy telemetry savings [--json] [--all]
    agentalloy telemetry phases [--phase] [--event-type] [--since] [--until] [--limit] [--json]

``clear`` deletes ``composition_traces`` from the user-scoped DuckDB without
touching ``fragment_embeddings`` (the corpus).

``savings`` aggregates token-savings telemetry from the consolidated proxy
compose traces and prints overall totals plus a per-phase breakdown.

``savings`` defaults to the repo you're in (resolved via the git toplevel);
pass ``--all`` to aggregate across every repo.

``phases`` queries the ``phase_events`` table (phase_start/phase_complete/
phase_error/llm_sent/llm_received/llm_error) written by ``PhaseTelemetryWriter``:
per-phase event counts, avg/p95 latency for the ``llm_*`` events, and a
chronological timeline of the most recent events.
"""

from __future__ import annotations

import argparse
import functools
import sys
from typing import Any, cast

from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
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
        help="Show token-savings summary (current repo by default; --all for every repo).",
    )
    add_json_flag(savings_p)
    _add_scope_flag(savings_p)
    savings_p.set_defaults(func=_run_savings)

    phases_p = sub.add_parser(
        "phases",
        help="Query phase_events: per-phase counts, llm_* latency, and a timeline.",
    )
    add_json_flag(phases_p)
    _add_scope_flag(phases_p)
    phases_p.add_argument("--phase", default=None, help="Filter to one phase (e.g. design).")
    phases_p.add_argument(
        "--event-type",
        default=None,
        help=(
            "Filter to one event_type (phase_start|phase_complete|phase_error|"
            "llm_sent|llm_received|llm_error)."
        ),
    )
    phases_p.add_argument(
        "--since", type=int, default=None, help="Only events at/after this unix ms timestamp."
    )
    phases_p.add_argument(
        "--until", type=int, default=None, help="Only events at/before this unix ms timestamp."
    )
    phases_p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows in the chronological timeline (default: 20).",
    )
    phases_p.set_defaults(func=_run_phases)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    return args.func(args)


def _add_scope_flag(p: argparse.ArgumentParser) -> None:
    """Add ``--all`` (aggregate every repo); default scope is the current repo."""
    p.add_argument(
        "--all",
        dest="all_repos",
        action="store_true",
        help="Aggregate across all repos. Default: only the repo you're in.",
    )


def _current_repo_key() -> str:
    """Resolve the project root used to scope telemetry to "this repo".

    Prefers the git toplevel so the scope covers the whole repo even when the
    command (or the recorded trace) came from a subdirectory — the store filter
    matches the root or anything nested under it. Falls back to the process cwd
    when this is not a git checkout.
    """
    import subprocess
    from pathlib import Path

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        top = out.stdout.strip()
        if out.returncode == 0 and top:
            return str(Path(top))
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.cwd())


def _resolve_scope(args: argparse.Namespace) -> str | None:
    """Return the repo filter for this invocation: None for ``--all``, else the
    current repo key."""
    return None if getattr(args, "all_repos", False) else _current_repo_key()


def _scope_label(repo: str | None) -> str:
    """Human-readable scope banner shown above savings output."""
    return "all repos" if repo is None else f"this repo · {repo}"


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
    from agentalloy.storage.open import open_telemetry

    settings = get_settings()
    ts = open_telemetry(settings, read_only=False)
    try:
        result = ts.clear_telemetry()
    finally:
        ts.close()

    write_result(result, args, human_fn=_render_clear)
    return 0


def _render_clear(result: dict[str, Any]) -> None:
    """Render telemetry clear result in human-readable format."""
    print_rich("\n  [bold]Telemetry Clear[/bold]\n")
    print_rich(f"  Traces deleted: {result['traces_deleted']}")
    print_rich()


def _service_port() -> int:
    """Resolve the configured service port from user-scope state (fallback 47950)."""
    from agentalloy.install import state as install_state

    return install_state.validate_port(install_state.load_state().get("port", 47950))


def _fetch_savings_via_api(port: int, repo: str | None = None) -> dict[str, Any] | None:
    """GET /telemetry/savings from the running service; None on any failure.

    Returns the same dict shape as ``TelemetryStore.aggregate_savings()`` so the
    existing renderer works unchanged. ``repo`` scopes to one project root.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"http://127.0.0.1:{port}/telemetry/savings"
    if repo is not None:
        url += "?" + urllib.parse.urlencode({"repo": repo})
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (localhost only)
            if resp.status != 200:
                return None
            data = cast(dict[str, Any], json.loads(resp.read().decode("utf-8")))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data


def _run_savings(args: argparse.Namespace) -> int:
    """Print token-savings aggregation.

    When the service is up it holds the single read-write DuckDB lock, so a
    direct file open would conflict. Route through the service API in that case;
    fall back to a direct read only when the service is down (offline diagnostics).
    """
    from agentalloy.install import server_proc

    repo = _resolve_scope(args)
    render = functools.partial(_render_savings, repo=repo)

    port = _service_port()
    if server_proc.port_reachable(port):
        result = _fetch_savings_via_api(port, repo)
        if result is not None:
            result["repo"] = repo
            write_result(result, args, human_fn=render)
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

    # Service is down — safe to open the telemetry store directly.
    from pathlib import Path

    from agentalloy.config import get_settings
    from agentalloy.storage.open import open_telemetry

    settings = get_settings()
    # A fresh install has no telemetry.duck yet. DuckDB refuses to open a
    # non-existent file read-only, and a read command must not create it, so
    # synthesize the empty aggregate (same shape as aggregate_savings()).
    if not Path(settings.telemetry_db_path).exists():
        result = _empty_savings()
    else:
        ts = open_telemetry(settings, read_only=True)
        try:
            result = ts.aggregate_savings(repo)
        finally:
            ts.close()

    result["repo"] = repo
    write_result(result, args, human_fn=render)
    return 0


def _empty_savings() -> dict[str, Any]:
    """Zero-valued savings aggregate matching ``TelemetryStore.aggregate_savings``."""
    return {
        "total_composes": 0,
        "tokens_returned": 0,
        "tokens_flat_equivalent": 0,
        "tokens_saved": 0,
        "savings_pct": 0.0,
        "per_phase": [],
    }


def _render_savings(result: dict[str, Any], repo: str | None = None) -> None:
    """Render token-savings aggregation in human-readable format."""
    total = int(result["total_composes"])
    if total == 0:
        print_rich("\n  [bold]Token Savings[/bold]")
        print_rich(f"  [dim]{_scope_label(repo)}[/dim]\n")
        if repo is not None:
            print_rich("  No compose traces recorded for this repo yet.")
            print_rich("  [dim]Run with --all to see every repo.[/dim]")
        else:
            print_rich("  No compose traces recorded yet.")
        print_rich()
        return

    tokens_returned = int(result["tokens_returned"])
    tokens_flat = int(result["tokens_flat_equivalent"])
    tokens_saved = int(result["tokens_saved"])
    savings_pct = float(result["savings_pct"])

    print_rich("\n  [bold]Token Savings Summary[/bold]")
    print_rich(f"  [dim]{_scope_label(repo)}[/dim]\n")
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


# ---------------------------------------------------------------------------
# ``phases`` — query the phase_events table
# ---------------------------------------------------------------------------

_LLM_EVENT_TYPES = ("llm_sent", "llm_received", "llm_error")


def _phase_events_where(
    *,
    phase: str | None,
    event_type: str | None,
    since: int | None,
    until: int | None,
    repo: str | None = None,
) -> tuple[str, list[object]]:
    from agentalloy.storage.telemetry_store import _repo_clause

    clauses: list[str] = []
    params: list[object] = []
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since is not None:
        clauses.append("request_ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("request_ts <= ?")
        params.append(until)
    repo_clause, repo_params = _repo_clause(repo)
    if repo_clause:
        clauses.append(repo_clause)
        params.extend(repo_params)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _query_phase_events(
    store: Any,
    *,
    phase: str | None,
    event_type: str | None,
    since: int | None,
    until: int | None,
    limit: int,
    repo: str | None = None,
) -> dict[str, Any]:
    """Run the three phase_events aggregations via ``DuckDBTelemetryStore.query``.

    ``phase_events`` is the phase-writer's schema, not this store's own typed
    read surface, so this goes through the generic ``query()`` raw-SQL escape
    hatch rather than a dedicated method. Returns a JSON-shaped result dict.

    ``repo`` mirrors ``savings``'s scoping: ``None`` aggregates across every
    repo, otherwise matches rows for that repo (and anything nested under it).
    """
    where, params = _phase_events_where(
        phase=phase, event_type=event_type, since=since, until=until, repo=repo
    )

    counts_rows = store.query(
        f"""
        SELECT phase, event_type, COUNT(*) AS n
        FROM phase_events
        {where}
        GROUP BY phase, event_type
        ORDER BY phase, event_type
        """,
        params,
    )
    per_phase: list[dict[str, Any]] = [
        {"phase": str(r[0]), "event_type": str(r[1]), "count": int(r[2])} for r in counts_rows
    ]

    # Latency aggregation for llm_* events. Respect an explicit --event-type
    # filter (e.g. --event-type llm_received); otherwise cover all three llm_*
    # types together.
    latency_clauses = list(params)
    latency_where = where
    if event_type is None:
        extra = "event_type IN ('llm_sent', 'llm_received', 'llm_error')"
        latency_where = f"{where} AND {extra}" if where else f"WHERE {extra}"
    latency_where_notnull = (
        f"{latency_where} AND latency_ms IS NOT NULL"
        if latency_where
        else "WHERE latency_ms IS NOT NULL"
    )
    latency_rows = store.query(
        f"""
        SELECT
            COUNT(*) AS n,
            AVG(latency_ms) AS avg_latency,
            quantile_cont(latency_ms, 0.95) AS p95_latency
        FROM phase_events
        {latency_where_notnull}
        """,
        latency_clauses,
    )
    latency_row = latency_rows[0] if latency_rows else None
    latency = {
        "count": int(latency_row[0]) if latency_row else 0,
        "avg_latency_ms": round(float(latency_row[1]), 1)
        if latency_row and latency_row[1] is not None
        else None,
        "p95_latency_ms": round(float(latency_row[2]), 1)
        if latency_row and latency_row[2] is not None
        else None,
    }

    timeline_rows = store.query(
        f"""
        SELECT trace_id, request_ts, phase, event_type, model, latency_ms, success
        FROM phase_events
        {where}
        ORDER BY request_ts DESC
        LIMIT ?
        """,
        [*params, limit],
    )
    timeline: list[dict[str, Any]] = [
        {
            "trace_id": r[0],
            "request_ts": int(r[1]),
            "phase": r[2],
            "event_type": r[3],
            "model": r[4],
            "latency_ms": r[5],
            "success": r[6],
        }
        for r in timeline_rows
    ]

    return {"per_phase": per_phase, "llm_latency": latency, "timeline": timeline}


def _phase_events_table_exists(store: Any) -> bool:
    rows = store.query("SELECT 1 FROM information_schema.tables WHERE table_name = 'phase_events'")
    return bool(rows)


def _phase_events_has_repo_column(store: Any) -> bool:
    """True once the ``repo`` column (issue #522) exists on ``phase_events``.

    ``PhaseTelemetryWriter._ensure_schema`` migrates the column lazily on the
    next *write* — this CLI only ever reads (``open_telemetry(..., read_only=
    True)``), so a pre-#522 14-column table on disk is never touched by that
    migration here. Querying it with a ``repo`` scope clause would raise a
    DuckDB BinderException. Given the column has zero historical rows to miss
    (see issue #522's "why now"), an un-migrated table is necessarily empty
    of anything a repo scope could match, so the caller treats "column
    missing" the same as "table missing": an empty result.
    """
    rows = store.query(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'phase_events' AND column_name = 'repo'"
    )
    return bool(rows)


def _empty_phases_result() -> dict[str, Any]:
    return {
        "per_phase": [],
        "llm_latency": {"count": 0, "avg_latency_ms": None, "p95_latency_ms": None},
        "timeline": [],
    }


def _run_phases(args: argparse.Namespace) -> int:
    """Query phase_events: per-phase counts, llm_* latency, chronological timeline.

    Mirrors ``savings``'s access pattern: the running service holds the
    read-write DuckDB lock, so a direct open would conflict with it. Refuse
    with actionable guidance when the service is up (no /telemetry/phases API
    exists yet to route through); read directly only when the service is down.
    """
    from pathlib import Path

    from agentalloy.config import get_settings
    from agentalloy.install import server_proc
    from agentalloy.storage.open import open_telemetry

    phase = getattr(args, "phase", None)
    event_type = getattr(args, "event_type", None)
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    limit = getattr(args, "limit", 20)
    repo = _resolve_scope(args)

    port = _service_port()
    if server_proc.port_reachable(port):
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

    settings = get_settings()
    if not Path(settings.telemetry_db_path).exists():
        result = _empty_phases_result()
    else:
        ts = open_telemetry(settings, read_only=True)
        try:
            if not _phase_events_table_exists(ts):
                # phase_events is created lazily on first PhaseTelemetryWriter
                # write, not at DB-open time — an existing-but-untouched
                # telemetry.duck has the composition_traces table only.
                result = _empty_phases_result()
            elif repo is not None and not _phase_events_has_repo_column(ts):
                # A pre-#522 14-column phase_events table: the writer's
                # migration only runs on the next write, and this CLI path
                # only reads (read_only=True), so a repo-scoped query here
                # would raise a DuckDB BinderException on the missing column.
                # Zero historical rows on that table can match any repo
                # scope, so empty is the correct (and safe) answer.
                result = _empty_phases_result()
            else:
                result = _query_phase_events(
                    ts,
                    phase=phase,
                    event_type=event_type,
                    since=since,
                    until=until,
                    limit=limit,
                    repo=repo,
                )
        finally:
            ts.close()

    result["repo"] = repo
    write_result(result, args, human_fn=functools.partial(_render_phases, phase=phase, repo=repo))
    return 0


def _render_phases(
    result: dict[str, Any], phase: str | None = None, repo: str | None = None
) -> None:
    """Render the phase_events query in human-readable format."""
    per_phase: list[dict[str, Any]] = list(result.get("per_phase") or [])
    latency: dict[str, Any] = result.get("llm_latency") or {}
    timeline: list[dict[str, Any]] = list(result.get("timeline") or [])

    print_rich("\n  [bold]Phase Events[/bold]")
    print_rich(f"  [dim]{_scope_label(repo)}[/dim]")
    if phase is not None:
        print_rich(f"  [dim]phase = {phase}[/dim]")
    print_rich()

    if not per_phase:
        print_rich("  No phase events recorded yet.")
        print_rich()
        return

    print_rich("  [bold]Per-phase event counts[/bold]")
    print_rich()
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for row in per_phase:
        by_phase.setdefault(str(row["phase"]), []).append(row)
    for ph, rows in by_phase.items():
        total = sum(int(r["count"]) for r in rows)
        breakdown = ", ".join(f"{r['event_type']}={r['count']}" for r in rows)
        print_rich(f"  {ph:<12} {total:>6,} total   [dim]({breakdown})[/dim]")

    print_rich()
    print_rich("  [bold]LLM latency (llm_sent/llm_received/llm_error)[/bold]")
    print_rich()
    if latency.get("count"):
        avg = latency.get("avg_latency_ms")
        p95 = latency.get("p95_latency_ms")
        print_rich(f"  Samples:    {int(latency['count']):,}")
        print_rich(f"  Avg latency: {avg:.1f} ms" if avg is not None else "  Avg latency: n/a")
        print_rich(f"  P95 latency: {p95:.1f} ms" if p95 is not None else "  P95 latency: n/a")
    else:
        print_rich("  No latency samples for the current filter.")

    print_rich()
    print_rich("  [bold]Timeline[/bold] (most recent first)")
    print_rich()
    if not timeline:
        print_rich("  No events for the current filter.")
    else:
        header = f"  {'Timestamp':<15}  {'Phase':<10}  {'Event':<16}  {'Model':<20}  {'Latency':>8}"
        print_rich(header)
        print_rich("  " + "-" * (len(header) - 2))
        for row in timeline:
            model = str(row["model"]) if row["model"] else "-"
            lat = f"{row['latency_ms']}ms" if row["latency_ms"] is not None else "-"
            print_rich(
                f"  {row['request_ts']:<15}  {str(row['phase']):<10}  "
                f"{str(row['event_type']):<16}  {model:<20}  {lat:>8}"
            )
    print_rich()
