"""``wire`` verb — per-repo harness wiring.

Convenience wrapper over ``wire-harness``. Auto-detects the harness from
markers in the cwd (`.cursor/` → cursor, `GEMINI.md` → gemini-cli,
`.continuerc.json` → continue-closed, etc.) and reads the service port
from user-scope state.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result
from agentalloy.install.subcommands.wire_harness import VALID_HARNESSES, wire_harness

# Harnesses that default to hook wiring (graceful degradation) rather than
# proxy wiring (a down service breaks the harness). Only claude-code today.
_HOOK_DEFAULT_HARNESSES = frozenset({"claude-code"})


def resolve_via(harness: str, via: str | None) -> str:
    """Resolve the effective wiring method for *harness*.

    Explicit ``--via`` always wins. When unset, claude-code defaults to
    ``hook`` (the failure-safe default) and every other harness to ``proxy``.
    """
    if via is not None:
        return via
    return "hook" if harness in _HOOK_DEFAULT_HARNESSES else "proxy"


def apply_hook_wiring(harness: str, port: int, root: Path) -> dict[str, Any]:
    """Wire *harness* via the provider hook_writer and record install state.

    Returns a wire-harness-shaped result dict. Records each WireRecord into
    ``harness_files_written`` (with original_content + repo_root preserved) so
    ``uninstall`` can reverse the change. Refuses (SystemExit 1) if the harness
    has no hook_writer.
    """
    from agentalloy.providers import REGISTRY

    spec = REGISTRY.get(harness)
    if spec is None or spec.hook_writer is None:
        print(
            f"ERROR: harness '{harness}' does not support hook wiring (--via hook).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    records = spec.hook_writer(port, root)

    files_written: list[dict[str, Any]] = []
    for rec in records:
        entry = rec.to_dict()
        entry.setdefault("harness", harness)
        entry.setdefault("repo_root", str(root))
        files_written.append(entry)

    # Merge into user-scoped install state, preserving prior original_content
    # on re-wire (the fresh record captured the post-first-write state).
    st = install_state.load_state(root)
    prior = st.get("harness_files_written") or []
    new_paths = {f.get("path") for f in files_written}
    prior_by_path = {e.get("path"): e for e in prior}
    for new_entry in files_written:
        prior_entry = prior_by_path.get(new_entry.get("path"))
        if prior_entry and "original_content" in prior_entry:
            new_entry.setdefault("original_content", prior_entry["original_content"])
    merged = [e for e in prior if e.get("path") not in new_paths] + files_written
    st["harness_files_written"] = merged
    install_state.save_state(st, root)

    return {
        "schema_version": 1,
        "harness": harness,
        "integration_vector": "hook",
        "files_written": files_written,
    }


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "wire",
        help="Inject AgentAlloy sentinels into the current repo's agent config.",
    )
    p.add_argument(
        "--harness",
        choices=sorted(VALID_HARNESSES),
        default=None,
        help="Force a specific harness. Default: auto-detect from cwd.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the service port (default: read from user state, fallback 47950).",
    )
    p.add_argument(
        "--via",
        choices=("hook", "proxy"),
        default=None,
        help=(
            "Wiring method. Default resolves per harness: 'hook' for claude-code "
            "(degrades gracefully if the service is down), 'proxy' for everything "
            "else. Pass --via proxy to force base-URL proxy wiring for claude-code."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an edited sentinel block (otherwise refuses).",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render wire harness result in human-readable format."""
    harness = result.get("harness", "unknown")
    files_written = result.get("files_written", [])
    files_modified = result.get("files_modified", [])
    total = len(files_written) + len(files_modified)

    print_rich("\n  [bold]Wire Harness[/bold]\n")
    print_rich(f"  Harness: [bold]{harness}[/bold]")
    print_rich(f"  Files: {total}")

    for f in files_written:
        print_rich(f"    [green]+[/green] {f}")
    for f in files_modified:
        print_rich(f"    [yellow]~[/yellow] {f}")

    if not files_written and not files_modified:
        print_rich("  [dim]No files to wire.[/dim]")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    harness = args.harness or _detect_harness(cwd)
    if harness is None:
        print(
            "ERROR: Could not detect a harness in the current directory.",
            file=sys.stderr,
        )
        print(
            f"FIX:   Pass --harness explicitly. Choices: {', '.join(sorted(VALID_HARNESSES))}.",
            file=sys.stderr,
        )
        return 1

    if args.port is not None:
        port = install_state.validate_port(args.port)
    else:
        st = install_state.load_state()
        port = install_state.validate_port(st.get("port", 47950))

    via = resolve_via(harness, getattr(args, "via", None))
    if via == "hook":
        result = apply_hook_wiring(harness, port=port, root=cwd)
    else:
        result = wire_harness(harness, port=port, root=cwd, force=args.force)
    write_result(result, args, human_fn=_render_human)
    return 0


# Detection priority (first match wins). Documented in INSTALL.md so
# users with multiple markers in the same repo know what they'll get.
# Order rationale: tool-specific dotfiles are stronger signals than
# `CLAUDE.md` (which Claude Code and many other agents now share), so
# they're checked first. A repo with both `.cursor/` and `CLAUDE.md`
# will wire as `cursor` — pass `--harness claude-code` to override.
_HARNESS_MARKERS: list[tuple[str, list[str]]] = [
    ("cursor", [".cursor", ".cursorrules"]),
    ("windsurf", [".windsurf", ".windsurfrules"]),
    ("continue-local", [".continuerc.json"]),
    ("aider", [".aider.conf.yml"]),
    ("opencode", [".opencode"]),
    ("cline", [".clinerules"]),
    ("gemini-cli", ["GEMINI.md"]),
    ("github-copilot", [".github/copilot-instructions.md"]),
    ("claude-code", ["CLAUDE.md"]),
    ("hermes-agent", [".hermes", "AGENTS.md"]),
]


def _detect_harness(cwd: Path) -> str | None:
    """Best-effort harness detection from filesystem markers in cwd.

    Returns the first harness whose marker exists, scanning in priority
    order. Multi-marker repos pick the more-specific tool first; users
    can always pass `--harness` explicitly to override.
    """
    matches = [h for h, markers in _HARNESS_MARKERS if any((cwd / m).exists() for m in markers)]
    if len(matches) > 1:
        print(
            f"NOTE: Multiple harness markers detected ({', '.join(matches)}); "
            f"defaulting to {matches[0]}. Pass --harness <name> to choose explicitly.",
            file=sys.stderr,
        )
    return matches[0] if matches else None
