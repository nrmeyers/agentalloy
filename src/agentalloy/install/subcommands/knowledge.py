"""``agentalloy knowledge`` — CLI for the Knowledge module (decisions over code).

    agentalloy knowledge why <symbol> [--repo]    Decisions governing a symbol

A thin HTTP client of the local service's ``/code/search/structural`` route
(``query=governing_decisions``). It is a **distinct namespace** from ``code`` —
decisions are a typed overlay on the same store, so the query rail is shared, but
the module boundary shows up where users meet it, on the command line (DK7). The
shared HTTP/slug/error helpers are reused from the ``code`` subcommand rather than
duplicated.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from agentalloy.install.subcommands import code
from agentalloy.install.subcommands.code import (
    _add_common,
    _guard_module,
    _http_error,
    _print_json,
    _resolve_port,
    _resolve_repo_slug,
    _service_down_error,
)


def _run_why(args: argparse.Namespace) -> int:
    slug = _resolve_repo_slug(args.repo)
    params: dict[str, Any] = {"repo": slug, "query": "governing_decisions", "fqn": args.symbol}
    port = _resolve_port(args)
    try:
        # via the code module so the _make_client seam stays monkeypatchable
        with code._make_client(port) as client:
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
        print("(no governing decisions)")
        return 0
    for d in results:
        loc = f"{d.get('file_path')}:{d.get('start_line')}" if d.get("file_path") else "?"
        print(f"  {d.get('qualified_name')}  {loc}  {d.get('heading', '')}".rstrip())
    return 0


def _run_knowledge(args: argparse.Namespace) -> int:
    """Bare ``agentalloy knowledge`` → usage."""
    print("Usage: agentalloy knowledge why <symbol> ...", file=sys.stderr)
    return 1


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "knowledge",
        help="Knowledge module: the decisions governing a symbol (why it's this way).",
    )
    p.set_defaults(func=_run_knowledge)
    sub: argparse._SubParsersAction[argparse.ArgumentParser] = p.add_subparsers(  # pyright: ignore[reportPrivateUsage]
        dest="knowledge_cmd"
    )

    why_p = sub.add_parser("why", help="Decisions governing a fully-qualified symbol.")
    why_p.add_argument("symbol", help="Fully-qualified symbol name.")
    _add_common(why_p, repo_flag=True)
    why_p.set_defaults(func=_run_why)
