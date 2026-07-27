"""``agentalloy compose`` — CLI wrapper that calls the running compose service.

Usage::

    agentalloy compose --contract <path>          # print composed output
    agentalloy compose --contract <path> --inject # output with [agentalloy] prefix
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p = subparsers.add_parser(
        "compose",
        help="Compose task guidance by calling the running agentalloy service.",
    )
    p.add_argument(
        "--contract",
        required=True,
        help="Absolute or relative path to a contract markdown file.",
    )
    p.add_argument(
        "--inject",
        action="store_true",
        help="Output with [agentalloy] prefix (for harness hook consumption).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=47950,
        help="AgentAlloy service port (default: 47950).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    import urllib.error
    import urllib.request

    contract_path = Path(args.contract).resolve()
    if not contract_path.exists():
        print(f"  [error] Contract not found: {contract_path}", file=sys.stderr)
        return 1

    url = f"http://localhost:{args.port}/compose/from-contract"
    payload = json.dumps({"contract_id": str(contract_path)}).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            detail = json.loads(body)
            print(f"  [error] Service returned {exc.code}: {detail}", file=sys.stderr)
        except Exception:
            print(f"  [error] Service returned {exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(
            f"  [error] Cannot reach agentalloy service at port {args.port}: {exc.reason}. "
            "Is the service running? Try: agentalloy serve",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"  [error] Unexpected error: {exc}", file=sys.stderr)
        return 1

    output = data.get("output", "")
    if not output:
        print("  (no output composed — corpus may be empty for this phase/tags)", file=sys.stderr)
        return 0

    if args.inject:
        print(f"[agentalloy]\n{output}\n[/agentalloy]")
    else:
        print(output)

    return 0
