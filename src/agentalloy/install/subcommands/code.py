"""``agentalloy code`` — CLI for the code-index module (``/code/*``).

Thin HTTP clients against the local agentalloy service:

    agentalloy code index [path] [--force] [--wait]     Start (and follow) an index job
    agentalloy code status                              Indexed repos + active jobs
    agentalloy code search <query> [--repo] [--lexical] [-k N]
    agentalloy code symbol <fqn> [--repo]
    agentalloy code callers <fqn> [--repo] [--depth N]
    agentalloy code callees <fqn> [--repo]
    agentalloy code bundle <task> [--repo] [--budget N]
    agentalloy code remove [path] [--yes]
    agentalloy code watch enable|disable [path]         Per-repo watch enrollment
    agentalloy code watch status|start|stop             Master switch + enrollment report

The module is served by the main agentalloy service (port from user state,
default 47950) when ``CODE_INDEX_ENABLED=1``; there is no separate daemon.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from agentalloy.code_index.slug import repo_slug
from agentalloy.code_index.staleness import check_staleness
from agentalloy.install import state as install_state

_TERMINAL_JOB_STATES = frozenset({"done", "failed", "cancelled", "interrupted"})
_POLL_INTERVAL_S = 0.5


def _resolve_port(args: argparse.Namespace) -> int:
    """Service port: explicit ``--port``, else user state, else 47950."""
    override = getattr(args, "port", None)
    if override is not None:
        return install_state.validate_port(override)
    st = install_state.load_state()
    return install_state.validate_port(st.get("port", 47950))


def _make_client(port: int) -> httpx.Client:
    """One httpx client per invocation. Tests monkeypatch this seam."""
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0)


def _service_down_error(port: int, exc: Exception) -> int:
    print(f"ERROR: Cannot reach the agentalloy service on port {port}.", file=sys.stderr)
    print(f"CAUSE: {exc}", file=sys.stderr)
    print(
        "FIX:   Start it with `agentalloy server-start` (or `agentalloy serve`).", file=sys.stderr
    )
    return 1


def _module_state_error(state: str | None) -> int:
    """ERROR/CAUSE/FIX for a reachable service whose code_index module is off."""
    if state == "unavailable":
        print(
            "ERROR: The code-index module failed to load in the running service.", file=sys.stderr
        )
        print(
            "CAUSE: CODE_INDEX_ENABLED is on but the [code-index] extra is not installed.",
            file=sys.stderr,
        )
        print(
            "FIX:   Install the extra (`uv tool install 'agentalloy[code-index]'`) "
            "and restart the service.",
            file=sys.stderr,
        )
        return 1
    print("ERROR: The code-index module is disabled in the running service.", file=sys.stderr)
    print(f"CAUSE: /health reports modules.code_index={state!r}.", file=sys.stderr)
    print(
        "FIX:   Set CODE_INDEX_ENABLED=1 (`agentalloy write-env --preset <preset> "
        "--overrides CODE_INDEX_ENABLED=1`) and restart the service, or re-run "
        "`agentalloy setup` and pick the codebase-indexer module.",
        file=sys.stderr,
    )
    return 1


def _check_module(client: httpx.Client) -> str | None:
    """Return the service's ``modules.code_index`` state, or None when unknown."""
    resp = client.get("/health")
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    modules = body.get("modules")
    if isinstance(modules, dict):
        state = modules.get("code_index")
        return state if isinstance(state, str) else None
    return None


def _guard_module(client: httpx.Client) -> int | None:
    """Exit-code error when the module isn't enabled, else None (proceed)."""
    state = _check_module(client)
    if state != "enabled":
        return _module_state_error(state)
    return None


def _resolve_repo_slug(repo_arg: str | None) -> str:
    """Slug for ``--repo PATH`` (default cwd). A non-path value is taken as a slug."""
    if repo_arg and not Path(repo_arg).expanduser().is_dir():
        return repo_arg  # already a slug
    root = Path(repo_arg).expanduser().resolve() if repo_arg else Path.cwd().resolve()
    return repo_slug(root)


def _not_indexed_error(slug: str, detail: str) -> int:
    print(f"ERROR: Repo {slug!r} is not indexed.", file=sys.stderr)
    print(f"CAUSE: {detail}", file=sys.stderr)
    print("FIX:   Index it first: `agentalloy code index` (run in the repo).", file=sys.stderr)
    return 1


def _http_error(exc: httpx.HTTPStatusError, *, slug: str | None = None) -> int:
    """Map a service error response to ERROR/CAUSE/FIX. 404 → not-indexed hint."""
    try:
        detail = str(exc.response.json().get("detail", exc.response.text))
    except Exception:  # noqa: BLE001 — non-JSON body
        detail = exc.response.text
    if exc.response.status_code == 404 and slug is not None and "not indexed" in detail:
        return _not_indexed_error(slug, detail)
    print(f"ERROR: Service returned {exc.response.status_code}.", file=sys.stderr)
    print(f"CAUSE: {detail}", file=sys.stderr)
    print(
        "FIX:   See the cause above; `agentalloy code status` shows indexed repos.", file=sys.stderr
    )
    return 1


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Subactions
# ---------------------------------------------------------------------------


def _run_index(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve() if args.path else Path.cwd().resolve()
    if not root.is_dir():
        print(f"ERROR: Not a directory: {root}", file=sys.stderr)
        print("CAUSE: `agentalloy code index [path]` needs an existing repo path.", file=sys.stderr)
        print("FIX:   Pass a valid repository path (default: cwd).", file=sys.stderr)
        return 1
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.post("/code/index", json={"repo_path": str(root), "force": args.force})
            resp.raise_for_status()
            job: dict[str, Any] = resp.json()
            if not args.wait:
                if args.json:
                    _print_json(job)
                else:
                    print(f"Index job started: id={job.get('id')} slug={job.get('slug')}")
                    print("Follow it with `agentalloy code status` (or re-run with --wait).")
                return 0
            return _wait_for_job(client, str(job.get("id")), as_json=args.json)
    except httpx.HTTPStatusError as exc:
        return _http_error(exc)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)


def _wait_for_job(client: httpx.Client, job_id: str, *, as_json: bool) -> int:
    """Poll one job to a terminal state, printing a single progress line."""
    job: dict[str, Any] = {}
    while True:
        resp = client.get(f"/code/index/{job_id}/status")
        resp.raise_for_status()
        job = resp.json()
        state = str(job.get("state", ""))
        if not as_json:
            phase = job.get("phase") or state
            progress = float(job.get("progress") or 0.0)
            print(f"\r  indexing [{phase}] {progress:5.1f}%", end="", flush=True)
        if state in _TERMINAL_JOB_STATES:
            break
        time.sleep(_POLL_INTERVAL_S)
    if not as_json:
        print()  # end the progress line
    if as_json:
        _print_json(job)
    state = str(job.get("state"))
    if state == "done":
        if not as_json:
            print(
                f"Indexed {job.get('slug')}: {job.get('symbol_count')} symbols, "
                f"{job.get('edge_count')} edges, {job.get('embedding_count')} embeddings."
            )
        return 0
    if not as_json:
        print(f"ERROR: Index job ended in state {state!r}.", file=sys.stderr)
        if job.get("error"):
            print(f"CAUSE: {job['error']}", file=sys.stderr)
        print(
            "FIX:   Re-run `agentalloy code index --force` after addressing the cause.",
            file=sys.stderr,
        )
    return 1


def _staleness_marker(repo: dict[str, Any]) -> str:
    """`` [stale ...]`` suffix when the repo's HEAD moved since its index.

    Silent (empty string) for non-git repos, missing paths, or when the
    comparison is impossible — a nudge, never an error.
    """
    repo_path = repo.get("repo_path")
    head_sha = repo.get("head_sha")
    if not isinstance(repo_path, str) or not isinstance(head_sha, str):
        return ""
    verdict = check_staleness(Path(repo_path), head_sha)
    if not verdict.stale:
        return ""
    if verdict.commits_behind is not None:
        return (
            f"  [stale — {verdict.commits_behind} commits behind; "
            f"run `agentalloy code index {repo_path}`]"
        )
    return f"  [stale — run `agentalloy code index {repo_path}`]"


def _run_status(args: argparse.Namespace) -> int:
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            repos_resp = client.get("/code/repos")
            repos_resp.raise_for_status()
            repos: list[dict[str, Any]] = repos_resp.json()
            jobs_resp = client.get("/code/index/jobs", params={"limit": 50})
            jobs_resp.raise_for_status()
            jobs: list[dict[str, Any]] = jobs_resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)

    active = [j for j in jobs if j.get("state") in ("queued", "running")]
    if args.json:
        _print_json({"repos": repos, "active_jobs": active})
        return 0

    print(f"Indexed repos ({len(repos)}):")
    if not repos:
        print("  (none — run `agentalloy code index` in a repo)")
    for r in repos:
        line = (
            f"  {r.get('slug')}  {r.get('repo_path')}  "
            f"symbols={r.get('symbol_count')} edges={r.get('edge_count')}"
        )
        if r.get("watch_enabled"):
            line += "  watch=on"
        line += _staleness_marker(r)
        print(line)
    print(f"Active jobs ({len(active)}):")
    if not active:
        print("  (none)")
    for j in active:
        print(
            f"  {j.get('id')}  {j.get('slug')}  [{j.get('phase') or j.get('state')}] "
            f"{float(j.get('progress') or 0.0):.1f}%"
        )
    return 0


def _run_search(args: argparse.Namespace) -> int:
    slug = _resolve_repo_slug(args.repo)
    endpoint = "/code/search/lexical" if args.lexical else "/code/search/semantic"
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.get(endpoint, params={"repo": slug, "q": args.query, "k": args.k})
            resp.raise_for_status()
            hits: list[dict[str, Any]] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)

    if args.json:
        _print_json(hits)
        return 0
    if not hits:
        print("(no results)")
        return 0
    for i, h in enumerate(hits, 1):
        loc = f"{h.get('file_path')}:{h.get('start_line')}" if h.get("file_path") else "?"
        print(
            f"{i:2}. {h.get('qualified_name')}  [{h.get('kind')}]  {loc}  "
            f"score={float(h.get('score') or 0.0):.3f}"
        )
    return 0


def _run_symbol(args: argparse.Namespace) -> int:
    slug = _resolve_repo_slug(args.repo)
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.get("/code/search/symbol", params={"repo": slug, "fqn": args.fqn})
            resp.raise_for_status()
            sym: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)

    if args.json:
        _print_json(sym)
        return 0
    loc = f"{sym.get('file_path')}:{sym.get('start_line')}-{sym.get('end_line')}"
    print(f"{sym.get('qualified_name')}  [{sym.get('kind')}]  {loc}")
    if sym.get("docstring"):
        print(f"  {sym['docstring']}")
    if sym.get("source_code"):
        print(str(sym["source_code"]))
    return 0


def _run_structural(args: argparse.Namespace, query: str) -> int:
    slug = _resolve_repo_slug(args.repo)
    params: dict[str, Any] = {"repo": slug, "query": query, "fqn": args.fqn}
    if query == "transitive_callers":
        params["depth"] = args.depth
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.get("/code/search/structural", params=params)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)

    if args.json:
        _print_json(body)
        return 0
    results = body.get("results") or []
    if not results:
        print("(no results)")
        return 0
    for r in results:
        loc = f"{r.get('file_path')}:{r.get('line')}" if r.get("file_path") else "?"
        print(f"  {r.get('qualified_name')}  {loc}")
    return 0


def _run_callers(args: argparse.Namespace) -> int:
    query = "transitive_callers" if args.depth > 1 else "callers"
    return _run_structural(args, query)


def _run_callees(args: argparse.Namespace) -> int:
    return _run_structural(args, "callees")


def _run_bundle(args: argparse.Namespace) -> int:
    slug = _resolve_repo_slug(args.repo)
    port = _resolve_port(args)
    payload = {"repo": slug, "task": args.task, "budget_chars": args.budget}
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.post("/code/context-bundle", json=payload)
            resp.raise_for_status()
            bundle: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)

    if args.json:
        _print_json(bundle)
        return 0
    items = bundle.get("items") or []
    print(
        f"Bundle for {bundle.get('repo')}: {len(items)} item(s), "
        f"{bundle.get('total_chars')}/{bundle.get('budget_chars')} chars"
    )
    for it in items:
        loc = (
            f"{it.get('file_path')}:{it.get('start_line')}-{it.get('end_line')}"
            if it.get("file_path")
            else "?"
        )
        print(f"  {it.get('qualified_name')}  [{it.get('reason')}]  {loc}")
    return 0


def _run_remove(args: argparse.Namespace) -> int:
    slug = _resolve_repo_slug(args.path)
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                f"ERROR: Refusing to remove index for {slug!r} without confirmation.",
                file=sys.stderr,
            )
            print("CAUSE: Non-interactive run and --yes was not passed.", file=sys.stderr)
            print("FIX:   Re-run with `agentalloy code remove --yes`.", file=sys.stderr)
            return 1
        answer = input(f"Remove the code index for {slug!r}? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.delete(f"/code/index/{slug}")
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)
    if args.json:
        _print_json(body)
    else:
        print(f"Removed index for {slug}.")
    return 0


# ---------------------------------------------------------------------------
# watch — master switch is env-driven (honest howto); per-repo enrollment is a
# live service call (POST /code/repos/{slug}/watch) so it reacts immediately.
# ---------------------------------------------------------------------------

_WATCH_HOWTO = (
    "The master file-watcher switch is a service-side setting (CODE_INDEX_WATCH),\n"
    "not a runtime toggle. To change it:\n"
    "  1. agentalloy write-env --preset <preset> --overrides CODE_INDEX_ENABLED=1 "
    "CODE_INDEX_WATCH={value}\n"
    "  2. agentalloy server-restart\n"
    "Per-repo enrollment is `agentalloy code watch enable|disable [path]`."
)


def _run_watch_toggle(args: argparse.Namespace, *, enabled: bool) -> int:
    """``watch enable|disable [path]`` — flip per-repo enrollment via the service."""
    slug = _resolve_repo_slug(getattr(args, "path", None))
    port = _resolve_port(args)
    try:
        with _make_client(port) as client:
            rc = _guard_module(client)
            if rc is not None:
                return rc
            resp = client.post(f"/code/repos/{slug}/watch", json={"enabled": enabled})
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as exc:
        return _http_error(exc, slug=slug)
    except httpx.HTTPError as exc:
        return _service_down_error(port, exc)
    if args.json:
        _print_json(body)
        return 0
    if enabled:
        if body.get("watching"):
            print(f"Watch enabled for {slug} (observer running).")
        elif not body.get("master_switch"):
            print(
                f"Watch enrollment recorded for {slug}. The master switch "
                "(CODE_INDEX_WATCH) is off, so no observer runs until it is enabled."
            )
        else:
            print(f"Watch enrollment recorded for {slug} (observer not started).")
    else:
        print(f"Watch disabled for {slug}.")
    return 0


def _run_watch_enable(args: argparse.Namespace) -> int:
    return _run_watch_toggle(args, enabled=True)


def _run_watch_disable(args: argparse.Namespace) -> int:
    return _run_watch_toggle(args, enabled=False)


def _run_watch(args: argparse.Namespace) -> int:
    action = getattr(args, "watch_action", None)
    if action == "start":
        print("The master watch switch is enabled via config, not by this command.")
        print(_WATCH_HOWTO.format(value="1"))
        return 0
    if action == "stop":
        print("The master watch switch is disabled via config, not by this command.")
        print(_WATCH_HOWTO.format(value="0"))
        return 0
    if action == "status":
        env = install_state.parse_env_file()
        configured = env.get("CODE_INDEX_WATCH", "0") in ("1", "true", "True")
        port = _resolve_port(args)
        service_state: str | None = None
        enrolled: list[dict[str, Any]] | None = None
        try:
            with _make_client(port) as client:
                service_state = _check_module(client)
                if service_state == "enabled":
                    resp = client.get("/code/repos")
                    resp.raise_for_status()
                    repos: list[dict[str, Any]] = resp.json()
                    enrolled = [r for r in repos if r.get("watch_enabled")]
        except httpx.HTTPError:
            service_state = None
        report: dict[str, Any] = {
            "configured": configured,
            "module": service_state or "unreachable",
            "enrolled_repos": enrolled,
        }
        if args.json:
            _print_json(report)
            return 0
        print(f"CODE_INDEX_WATCH (master switch): {'on' if configured else 'off'}")
        print(f"code_index module (service):      {report['module']}")
        if service_state is None:
            print("(service unreachable — the configured value applies on next start)")
        elif enrolled is not None:
            print(f"Watch-enrolled repos ({len(enrolled)}):")
            if not enrolled:
                print("  (none — enroll with `agentalloy code watch enable [path]`)")
            for r in enrolled:
                print(f"  {r.get('slug')}  {r.get('repo_path')}")
        return 0
    print("Usage: agentalloy code watch {enable,disable,status,start,stop}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser, *, repo_flag: bool = False) -> None:
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Service port (default: read from user state, fallback 47950).",
    )
    p.add_argument("--json", action="store_true", default=False, help="Output raw JSON.")
    if repo_flag:
        p.add_argument(
            "--repo",
            default=None,
            help="Repo path (or slug) to query. Default: the current directory.",
        )


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "code",
        help="Code-index module: index repos, search code, trace call graphs.",
    )
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = p.add_subparsers(dest="code_cmd")  # pyright: ignore[reportPrivateUsage]

    index_p = sub.add_parser("index", help="Index a repository (async job on the service).")
    index_p.add_argument("path", nargs="?", default=None, help="Repo path (default: cwd).")
    index_p.add_argument(
        "--force", action="store_true", help="Full rebuild, ignore content hashes."
    )
    index_p.add_argument("--wait", action="store_true", help="Poll the job to completion.")
    _add_common(index_p)
    index_p.set_defaults(func=_run_index)

    status_p = sub.add_parser("status", help="List indexed repos and active index jobs.")
    _add_common(status_p)
    status_p.set_defaults(func=_run_status)

    search_p = sub.add_parser("search", help="Hybrid semantic (or --lexical) code search.")
    search_p.add_argument("query", help="Natural-language or symbol query.")
    search_p.add_argument("--lexical", action="store_true", help="BM25-only lexical search.")
    search_p.add_argument("-k", type=int, default=10, help="Max results (default 10).")
    _add_common(search_p, repo_flag=True)
    search_p.set_defaults(func=_run_search)

    symbol_p = sub.add_parser("symbol", help="Exact symbol lookup by fully-qualified name.")
    symbol_p.add_argument("fqn", help="Fully-qualified symbol name.")
    _add_common(symbol_p, repo_flag=True)
    symbol_p.set_defaults(func=_run_symbol)

    callers_p = sub.add_parser("callers", help="Call sites of a symbol (--depth for transitive).")
    callers_p.add_argument("fqn", help="Fully-qualified symbol name.")
    callers_p.add_argument("--depth", type=int, default=1, help="Transitive hop cap (default 1).")
    _add_common(callers_p, repo_flag=True)
    callers_p.set_defaults(func=_run_callers)

    callees_p = sub.add_parser("callees", help="Symbols a function calls.")
    callees_p.add_argument("fqn", help="Fully-qualified symbol name.")
    _add_common(callees_p, repo_flag=True)
    callees_p.set_defaults(func=_run_callees)

    bundle_p = sub.add_parser("bundle", help="Assemble a budgeted code context for a task.")
    bundle_p.add_argument("task", help="Natural-language task description.")
    bundle_p.add_argument(
        "--budget", type=int, default=24000, help="Character budget (default 24000)."
    )
    _add_common(bundle_p, repo_flag=True)
    bundle_p.set_defaults(func=_run_bundle)

    remove_p = sub.add_parser("remove", help="Remove a repo's code index (with confirmation).")
    remove_p.add_argument("path", nargs="?", default=None, help="Repo path (default: cwd).")
    remove_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    _add_common(remove_p)
    remove_p.set_defaults(func=_run_remove)

    watch_p = sub.add_parser(
        "watch", help="Per-repo watch enrollment + the CODE_INDEX_WATCH master switch."
    )
    watch_sub: argparse._SubParsersAction[argparse.ArgumentParser] = watch_p.add_subparsers(  # pyright: ignore[reportPrivateUsage]
        dest="watch_action"
    )
    enable_p = watch_sub.add_parser(
        "enable", help="Enroll a repo for watching (observer starts if the master switch is on)."
    )
    enable_p.add_argument("path", nargs="?", default=None, help="Repo path (default: cwd).")
    _add_common(enable_p)
    enable_p.set_defaults(func=_run_watch_enable)
    disable_p = watch_sub.add_parser(
        "disable", help="Unenroll a repo from watching (stops its observer immediately)."
    )
    disable_p.add_argument("path", nargs="?", default=None, help="Repo path (default: cwd).")
    _add_common(disable_p)
    disable_p.set_defaults(func=_run_watch_disable)
    for name, help_text in (
        ("start", "How to enable the service-side master switch."),
        ("stop", "How to disable the service-side master switch."),
        ("status", "Master switch, module state, and watch-enrolled repos."),
    ):
        wp = watch_sub.add_parser(name, help=help_text)
        _add_common(wp)
        wp.set_defaults(func=_run_watch)
    watch_p.set_defaults(func=_run_watch)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    if getattr(args, "code_cmd", None) is None:
        print(
            "Usage: agentalloy code "
            "{index,status,search,symbol,callers,callees,bundle,remove,watch} ...",
            file=sys.stderr,
        )
        return 1
    # Subparsers override func via set_defaults; reaching here means argparse
    # matched `code` without a subaction (handled above).
    return 1
