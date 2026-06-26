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

    claude-code is proxy-wired per repo (``<repo>/.agentalloy/claude-code-env.sh``),
    so its carrier is genuinely repo-scoped. Some harness configs still land in
    user-global locations under ``~/.claude`` / ``~/.agentalloy`` (e.g. the
    ``--mcp-fallback`` server config), written *during* a per-repo `wire`. Flag
    those so status stops implying they are repo-scoped.
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


def _orphan_summary() -> str:
    """Return a one-line summary of stray runtime artifacts, or 'none'.

    Read-only and best-effort: ``detect_orphans`` never mutates and never
    raises, but any failure here resolves to 'none' so status never breaks.
    """
    try:
        from agentalloy.install.runtime_artifacts import detect_orphans  # noqa: PLC0415

        orphans = detect_orphans()
    except Exception:
        return "none"
    if not orphans:
        return "none"

    stale = sum(1 for o in orphans if o.kind == "process")
    shims = sum(1 for o in orphans if o.kind == "shim")
    conflicts = sum(1 for o in orphans if o.kind == "conflict")

    parts: list[str] = []
    if stale:
        parts.append(f"{stale} stale process{'es' if stale != 1 else ''}")
    if shims:
        parts.append(f"{shims} dangling shim{'s' if shims != 1 else ''}")
    if conflicts:
        parts.append(f"{conflicts} port conflict{'s' if conflicts != 1 else ''}")
    if not parts:
        return "none"
    summary = ", ".join(parts)
    # Only the reapable kinds warrant the cleanup hint.
    if stale or shims:
        summary = f"{summary} (run 'agentalloy cleanup')"
    return summary


def _release_snapshot() -> dict[str, Any]:
    """Current vs latest-known release for the dashboard. Best-effort.

    Triggers a throttled ``refresh`` (a no-op within the check interval) so an
    on-demand ``status`` stays fresh even when the service isn't running, then
    reads the cache. Any failure resolves to "latest unknown".
    """
    try:
        from agentalloy.install import release_check  # noqa: PLC0415

        release_check.refresh()
        info = release_check.notice()
        cache = release_check.read_cache()
        return {
            "current": release_check.current_version(),
            "latest": cache.get("latest_tag"),
            "update_available": bool(info),
            "bump_type": info["bump_type"] if info else None,
        }
    except Exception:
        return {"current": None, "latest": None, "update_available": False, "bump_type": None}


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
    if corpus.get("mode") == "container":
        corpus_status = (
            "[green]in container[/green]"
            if corpus.get("present")
            else "[yellow]container not running[/yellow]"
        )
    else:
        corpus_status = "[green]present[/green]" if corpus.get("present") else "[red]missing[/red]"
    print_rich(f"\n  Corpus: {corpus_status}")
    print_rich(f"    Path: {corpus.get('path', 'N/A')}")

    # Service
    service = snapshot.get("service", {})
    port = service.get("port", "N/A")
    reachable = service.get("reachable_on_loopback", False)
    status_icon = "[green]✓ reachable[/green]" if reachable else "[red]✗ not reachable[/red]"
    print_rich(f"\n  Service (port {port}): {status_icon}")

    # Orphaned runtime artifacts (stale processes / dangling shim)
    orphans = snapshot.get("orphans", "none")
    orphan_status = "[green]none[/green]" if orphans == "none" else f"[yellow]{orphans}[/yellow]"
    print_rich(f"\n  Orphans: {orphan_status}")

    # Release
    release = snapshot.get("release", {})
    current = release.get("current") or "unknown"
    if release.get("update_available"):
        rel = (
            f"[yellow]↑ {release.get('latest')} available[/yellow] "
            f"({release.get('bump_type')}) — run `agentalloy upgrade`"
        )
    elif release.get("latest"):
        rel = "[green]up to date[/green]"
    else:
        rel = "[dim]latest unknown[/dim]"
    print_rich(f"\n  Release: {current} — {rel}")

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
                scope_tag = " [dim](global)[/dim]" if entry.get("scope") == "global" else ""
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

    # Service reachability — TCP connect only; doctor/verify do the deeper /health
    # probe. Computed before the corpus check because a container deploy derives
    # corpus presence from the running service, not the host data dir.
    port_raw = st.get("port", 47950)
    try:
        port = install_state.validate_port(port_raw)
        service_reachable = _port_open("127.0.0.1", port)
    except SystemExit:
        port = None
        service_reachable = False

    # Corpus presence. Container deploys keep the corpus in the agentalloy-data
    # volume *inside* the container, so a host-path check always reports
    # "missing" — the running container is the source of truth there.
    corpus_path = install_state.corpus_dir()
    deployment = st.get("deployment")
    if deployment == "container":
        corpus_present = service_reachable
        corpus_location = "container volume (agentalloy-data)"
    else:
        corpus_present = (corpus_path / "skills.duck").exists() and (
            corpus_path / "ladybug"
        ).exists()
        corpus_location = str(corpus_path)

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "user_config_dir": str(install_state.user_config_dir()),
        "user_data_dir": str(install_state.user_data_dir()),
        "completed_steps": completed_step_names,
        "corpus": {
            "path": corpus_location,
            "present": corpus_present,
            "mode": deployment,
        },
        "service": {
            "port": port,
            "reachable_on_loopback": service_reachable,
        },
        "orphans": _orphan_summary(),
        "release": _release_snapshot(),
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
