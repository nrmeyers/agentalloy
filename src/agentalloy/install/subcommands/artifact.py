"""Artifact CLI — record deliverable artifacts into the state store.

An artifact's only home is the state store. This verb exists so an agent can
pipe content straight from its context into the store — stdin by default, an
existing file with ``--file`` — without writing workflow deliverables (spec,
approach, tasks, test plan) to disk on the way.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentalloy.api.state_client import StateClientError
from agentalloy.install.subcommands._state import fail_on_state_error, phase_access


def run_artifact_put(args: argparse.Namespace) -> int:
    """Record (upsert) a deliverable artifact body into the state store."""
    if args.file is not None:
        path = Path(args.file)
        if not path.is_file():
            print(f"Error: no such file: {path}", file=sys.stderr)
            return 1
        content = path.read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()
    if not content.strip():
        print(
            "Error: artifact content is empty — pipe the body on stdin "
            "or pass --file <path>.",
            file=sys.stderr,
        )
        return 1
    access = phase_access(Path.cwd())
    try:
        access.artifact_handle().set_artifact(args.phase, args.slug, args.name, content)
    except StateClientError as exc:
        fail_on_state_error(exc)
    print(f"Recorded {args.phase}/{args.slug}/{args.name} ({len(content)} chars).")
    return 0


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the 'artifact' subcommand and its verbs."""
    artifact_parser = subparsers.add_parser(
        "artifact",
        help="Deliverable artifacts (put: record a body into the state store)",
    )
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_verb", required=True)

    put_parser = artifact_subparsers.add_parser(
        "put",
        help="Record an artifact body (stdin by default; the store is its only home)",
    )
    put_parser.add_argument(
        "--phase", required=True, help="Workflow phase (e.g. spec, design, plan)"
    )
    put_parser.add_argument(
        "--slug", required=True, help="Task/work-item slug (e.g. llm-config)"
    )
    put_parser.add_argument(
        "--name",
        required=True,
        help="Artifact name the phase's exit gate matches (e.g. tasks.artifact)",
    )
    put_parser.add_argument(
        "--file", help="Read content from this existing file instead of stdin"
    )
    put_parser.set_defaults(func=run_artifact_put)
