"""``reembed`` subcommand — forward to ``agentalloy.reembed.cli``.

The reembed CLI itself lives in ``agentalloy.reembed.cli`` and has its
own argparse. This subcommand re-declares the same flags and forwards
them so users can run::

    |    agentalloy reembed [--skill-id ID] [--limit N] [--force] [--model M] [--dry-run] [--no-restart]

The remediation strings emitted by ``install-packs`` reference
``agentalloy reembed``, so this must stay in the registered subcommand
list.
"""

from __future__ import annotations

import argparse


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "reembed",
        help="Compute embeddings for unembedded skill-store fragments.",
        description=(
            "Compute embeddings for the skill store's active fragments and write "
            "them to the Lance fragment store. Idempotent on re-run."
        ),
    )
    p.add_argument(
        "--skill-id",
        help="Only embed fragments for this skill_id (default: all skills)",
    )
    p.add_argument(
        "--limit",
        type=int,
        help="Cap the number of fragments processed (after skip-filtering)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Delete existing embeddings for the scope and re-embed from scratch",
    )
    p.add_argument(
        "--model",
        help="Override the embedding model id (default: runtime_embedding_model from config)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be embedded without calling LM Studio or writing DuckDB",
    )
    p.add_argument(
        "--rebuild-fts",
        action="store_true",
        help=(
            "Force rebuild of the BM25 FTS index even when nothing needs "
            "re-embedding. Use to recover from an install-time FTS warning."
        ),
    )
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart the agentalloy service after operation",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    from agentalloy.reembed.cli import main as reembed_main

    forwarded: list[str] = []
    if args.skill_id:
        forwarded += ["--skill-id", args.skill_id]
    if args.limit is not None:
        forwarded += ["--limit", str(args.limit)]
    if args.force:
        forwarded.append("--force")
    if args.model:
        forwarded += ["--model", args.model]
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.rebuild_fts:
        forwarded.append("--rebuild-fts")
    if args.no_restart:
        forwarded.append("--no-restart")
    return reembed_main(forwarded)
