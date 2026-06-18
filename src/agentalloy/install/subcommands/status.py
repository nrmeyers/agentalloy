"""``status`` verb — show the current install snapshot.

Reads the user-scope state and reports:
  * Which user-scope steps have completed (detect, recommend-*,
    pull-models, seed-corpus, write-env).
  * Which repos have been wired, grouped by ``repo_root`` from
    ``harness_files_written`` entries.
  * Whether the corpus is present at the user data dir.
  * Whether the service is reachable on the configured port.

Read-only. Never mutates state. Safe to run anywhere.
"""

from __future__ import annotations

import argparse
import socket
from collections import defaultdict
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, write_result

SCHEMA_VERSION = 1


def _path_scope(path: str | None) -> str:
    """Classify a wired-file path as 'global' (user-scope) or 'repo' (project).

    The claude-code hook lives in the user-global ``~/.claude/settings.json``
    (and ``~/.agentalloy/hooks/``), so it fires in every session — it is not a
    per-repo file even though it was written *during* a per-repo `wire`. Flag
    those so status stops implying the hook is repo-scoped.
    """
    if not path:
        return "repo"
    try:
        p = Path(path).resolve()
    except OSError:
        return "repo"
    home = Path.home().resolve()
    global_roots = (home / ".claude", home / ".agentalloy")
    return "global" if any(g == p or g in p.parents for g in global_roots) else "repo"


def _repo_phase(repo_root: str) -> str | None:
    """Return the activated phase for *repo_root*, or None if not activated.

    A repo composes only when it has an ``.agentalloy/phase`` file (the real
    per-repo activation gate). This is what makes status self-diagnosing: a
    wired-but-unphased repo is exactly the "wired but nothing happens" case.
    """
    try:
        root = Path(repo_root)
        if not (root / ".agentalloy" / "phase").exists():
            return None
        from agentalloy.install.subcommands.phase import run_phase_get  # noqa: PLC0415

        phase = run_phase_get(root=root).get("phase")
        return phase if phase and phase != "none" else None
    except Exception:
        return None


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "status",
        help="Show user-scope install state, wired repos, and service reachability.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(snapshot: dict[str, Any]) -> None:
    """Render install status dashboard in human-readable format."""
    from agentalloy.install.output import print_rich

    print_rich("\n  [bold]Install Status[/bold]\n")

    # Paths
    print_rich(f"  Config dir: {snapshot.get('user_config_dir', 'N/A')}")
    print_rich(f"  Data dir:   {snapshot.get('user_data_dir', 'N/A')}")

    # Completed steps
    steps = snapshot.get("completed_steps", [])
    if steps:
        print_rich(f"\n  Completed steps ({len(steps)}):")
        for s in steps:
            print_rich(f"    [green]✓[/green] {s}")
    else:
        print_rich("\n  Completed steps: none")

    # Corpus
    corpus = snapshot.get("corpus", {})
    corpus_status = "[green]present[/green]" if corpus.get("present") else "[red]missing[/red]"
    print_rich(f"\n  Corpus: {corpus_status}")
    print_rich(f"    Path: {corpus.get('path', 'N/A')}")

    # Service
    service = snapshot.get("service", {})
    port = service.get("port", "N/A")
    reachable = service.get("reachable_on_loopback", False)
    status_icon = "[green]✓ reachable[/green]" if reachable else "[red]✗ not reachable[/red]"
    print_rich(f"\n  Service (port {port}): {status_icon}")

    # Wired repos
    repos = snapshot.get("wired_repos", [])
    if repos:
        print_rich(f"\n  Wired repos ({len(repos)}):")
        for repo in repos:
            repo_root = repo.get("repo_root", "<unknown>")
            entries = repo.get("entries", [])
            if repo.get("activated"):
                act = f"[green]✓ activated[/green] [dim](phase: {repo.get('phase')})[/dim]"
            else:
                act = (
                    "[yellow]⚠ not activated[/yellow] "
                    "[dim](no .agentalloy/phase — run `agentalloy wire` here)[/dim]"
                )
            print_rich(f"    [bold]{repo_root}[/bold] ({len(entries)} file(s)) — {act}")
            for entry in entries:
                harness = entry.get("harness", "unknown")
                path = entry.get("path", "")
                scope_tag = " [dim](global hook)[/dim]" if entry.get("scope") == "global" else ""
                print_rich(f"      {harness}: {path}{scope_tag}")
    else:
        print_rich("\n  Wired repos: none")

    # Env file
    env = snapshot.get("env_file", {})
    env_status = "[green]exists[/green]" if env.get("exists") else "[red]missing[/red]"
    print_rich(f"\n  .env file: {env_status}")
    print_rich(f"    Path: {env.get('path', 'N/A')}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    st = install_state.load_state()
    completed_step_names = [s.get("step") for s in st.get("completed_steps", [])]

    # Group harness entries by repo_root so multi-repo users see a clean
    # per-project picture.
    repos: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in st.get("harness_files_written", []):
        repo_root = entry.get("repo_root") or "<unknown>"
        path = entry.get("path")
        repos[repo_root].append(
            {
                "harness": entry.get("harness"),
                "path": path,
                "action": entry.get("action"),
                "scope": _path_scope(path),
            }
        )

    # Corpus presence
    corpus_path = install_state.corpus_dir()
    corpus_present = (corpus_path / "skills.duck").exists() and (corpus_path / "ladybug").exists()

    # Service reachability — TCP connect only; doctor/verify do the deeper /health probe.
    port_raw = st.get("port", 47950)
    try:
        port = install_state.validate_port(port_raw)
        service_reachable = _port_open("127.0.0.1", port)
    except SystemExit:
        port = None
        service_reachable = False

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "user_config_dir": str(install_state.user_config_dir()),
        "user_data_dir": str(install_state.user_data_dir()),
        "completed_steps": completed_step_names,
        "corpus": {
            "path": str(corpus_path),
            "present": corpus_present,
        },
        "service": {
            "port": port,
            "reachable_on_loopback": service_reachable,
        },
        "wired_repos": [
            {
                "repo_root": repo_root,
                "entries": entries,
                "phase": _repo_phase(repo_root),
                "activated": _repo_phase(repo_root) is not None,
            }
            for repo_root, entries in sorted(repos.items())
        ],
        "env_file": {
            "path": str(install_state.env_path()),
            "exists": install_state.env_path().exists(),
        },
    }
    write_result(snapshot, args, human_fn=_render_human)
    return 0


def _port_open(host: str, port: int) -> bool:
    """Return True if a TCP connect to host:port succeeds within 1 second."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False
