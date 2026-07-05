"""``unwire`` verb — remove AgentAlloy sentinels from the current repo.

Per-repo cleanup by default. Walks ``harness_files_written`` entries and removes
the sentinels/dedicated files that belong to the cwd-derived repo — repo-local
files and the user-scope harness configs (~/.claude, ~/.agentalloy, ...) that were
recorded for *this* repo. Entries from other repos, and shared user-scope configs
recorded for another repo, are left alone so unwiring one repo never unwires the
rest. Does NOT touch user-scope state directories, ``.env``, the corpus, or
services.

``--all`` widens the walk to every repo's wiring (and all user-scope harness
configs), matching the historical behavior. ``uninstall`` is still the full
user-scope teardown (state + corpus + .env + services).
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, render_lifecycle_result, write_result
from agentalloy.install.subcommands.uninstall import uninstall
from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "unwire",
        help="Remove AgentAlloy sentinels from the current repo (keeps user state).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Force removal even when sentinel content has been edited.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        dest="all_repos",
        help="Unwire every repo's harness wiring (and shared user-scope configs), "
        "not just the current repo. Still keeps user state, .env, corpus, and services.",
    )
    p.add_argument(
        "--harness",
        choices=sorted(VALID_HARNESSES),
        default=None,
        help="Unwire only this harness; leave any other wired harness and the repo's "
        "shared lifecycle state (.agentalloy/phase, config) intact. Combine with --all "
        "to unwire this harness across every recorded repo.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        dest="assume_yes",
        help="Answer the unwire prompts non-interactively. The code index is KEPT "
        "unless --remove-index is also passed (it is expensive to rebuild).",
    )
    p.add_argument(
        "--remove-index",
        action="store_true",
        dest="remove_index",
        help="Also remove this repo's code index (store directory + registry row). "
        "Without it, unwire keeps the index.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


# ---------------------------------------------------------------------------
# Code-index cleanup: prompt (default NO) to drop the repo's index on unwire.
# Service-first (DELETE /code/index/{slug} — stops watches, honors the
# active-job 409); direct fallback when the service is down. The direct path
# deliberately avoids importing agentalloy.code_index.store (its __init__
# pulls DuckDB/Lance, which may not be installed without the extra): the
# registry is plain SQLite and removal is an rmtree — no DB engine needed.
# ---------------------------------------------------------------------------


def _service_port() -> int:
    st = install_state.load_state()
    return install_state.validate_port(st.get("port", 47950))


def _jobs_db_path() -> Path:
    from agentalloy.config import get_settings

    return Path(get_settings().code_index_data_dir) / "jobs.sqlite"


def _registry_row(slug: str) -> tuple[str, str] | None:
    """(repo_path, data_dir) for *slug* from the registry, or None.

    Read-only raw-SQLite lookup that works whether or not the service is up
    (WAL readers never block on the writer) and never creates the DB file.
    """
    db = _jobs_db_path()
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT repo_path, data_dir FROM indexed_repos WHERE slug = ?", (slug,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (str(row[0]), str(row[1])) if row is not None else None


def _remove_index_via_service(slug: str, port: int) -> bool | None:
    """DELETE /code/index/{slug}. True=removed, False=refused (active job /
    error reported), None=service unreachable (caller falls back)."""
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            resp = client.delete(f"/code/index/{slug}")
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return True
    detail: object
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:  # noqa: BLE001 — non-JSON body
        detail = resp.text
    print(f"ERROR: Could not remove the code index for {slug!r}.", file=sys.stderr)
    print(f"CAUSE: Service returned {resp.status_code}: {detail}", file=sys.stderr)
    print(
        "FIX:   Retry with `agentalloy code remove` once no index job is active.", file=sys.stderr
    )
    return False


def _remove_index_direct(slug: str, data_dir: str) -> bool:
    """Service-down removal: refuse on an active job, rmtree + registry delete.

    Mirrors the DELETE endpoint's semantics without opening DuckDB — the store
    directory is removed with rmtree and the registry rows are plain SQLite.
    """
    db = _jobs_db_path()
    try:
        conn = sqlite3.connect(db)
        try:
            active = conn.execute(
                "SELECT job_id FROM jobs WHERE slug = ? AND status IN ('queued','running') LIMIT 1",
                (slug,),
            ).fetchone()
            if active is not None:
                print(f"ERROR: Could not remove the code index for {slug!r}.", file=sys.stderr)
                print(f"CAUSE: Index job {active[0]} is still recorded as active.", file=sys.stderr)
                print(
                    "FIX:   Wait for it to finish (or start the service and cancel it), "
                    "then run `agentalloy code remove`.",
                    file=sys.stderr,
                )
                return False
            target = Path(data_dir)
            if target.is_dir():
                shutil.rmtree(target)
            conn.execute("DELETE FROM indexed_repos WHERE slug = ?", (slug,))
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        print(f"ERROR: Could not remove the code index for {slug!r}.", file=sys.stderr)
        print(f"CAUSE: {exc}", file=sys.stderr)
        print("FIX:   Start the service and run `agentalloy code remove`.", file=sys.stderr)
        return False
    return True


def _maybe_remove_code_index(
    root: Path, *, assume_yes: bool, remove_index: bool
) -> dict[str, Any] | None:
    """Offer to drop *root*'s code index after unwiring. None = not indexed.

    Default is KEEP: the index is expensive to rebuild and removing the harness
    block is not a statement about the data. Only an explicit ``--remove-index``
    or an interactive "y" removes it; ``--yes`` alone and non-TTY runs keep it.
    """
    from agentalloy.code_index.slug import repo_slug

    slug = repo_slug(root)
    row = _registry_row(slug)
    if row is None:
        return None
    if not remove_index:
        if assume_yes or not sys.stdin.isatty():
            return {"slug": slug, "removed": False, "kept": "default"}
        answer = input(f"Also remove its code index ({slug!r})? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            return {"slug": slug, "removed": False, "kept": "declined"}
    port = _service_port()
    removed = _remove_index_via_service(slug, port)
    if removed is None:
        removed = _remove_index_direct(slug, row[1])
    return {"slug": slug, "removed": bool(removed)}


def _run(args: argparse.Namespace) -> int:
    # `unwire` is per-repo by default: remove sentinels for entries belonging to the
    # cwd-derived repo, leave the user-scope state and `.env` untouched. `--all`
    # (args.all_repos) widens the harness walk to every repo + shared user-scope
    # configs. `remove_user_state=False` and `remove_env=False` skip the user-scope
    # teardown branches in `uninstall()` in both modes; the sentinel work is the same
    # as a full uninstall otherwise.
    result = uninstall(
        remove_data=False,
        force=args.force,
        remove_user_state=False,
        remove_env=False,
        all_repos=getattr(args, "all_repos", False),
        # `unwire` is sentinel-only: keep services running, keep models,
        # keep all user-scope state. The new explicit kwargs preserve
        # this behavior independently of how the meta-uninstall defaults
        # evolve.
        stop_services=False,
        remove_models=False,
        remove_wiring=True,
        # When set, scope the teardown to a single harness: leave any other wired
        # harness and the repo's shared lifecycle state (.agentalloy/phase, config)
        # in place. None == today's behavior (every harness in scope).
        harness=getattr(args, "harness", None),
    )

    # Code-index cleanup for THIS repo (cwd). Skipped for a harness-scoped
    # unwire — the repo stays wired for the remaining harness, so its index
    # is still in use.
    if getattr(args, "harness", None) is None:
        ci = _maybe_remove_code_index(
            Path.cwd().resolve(),
            assume_yes=bool(getattr(args, "assume_yes", False)),
            remove_index=bool(getattr(args, "remove_index", False)),
        )
        if ci is not None:
            result["code_index"] = ci
            if ci.get("removed"):
                print(f"Removed the code index for {ci['slug']}.", file=sys.stderr)
            elif ci.get("kept"):
                print(
                    f"Kept the code index for {ci['slug']} "
                    "(remove later with `agentalloy code remove`).",
                    file=sys.stderr,
                )

    write_result(result, args, human_fn=lambda r: render_lifecycle_result(r, "Unwire"))
    return 0
