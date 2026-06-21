"""``wire`` verb — per-repo harness wiring.

Convenience wrapper over ``wire-harness``. Auto-detects the harness from
markers in the cwd (`.cursor/` → cursor, `GEMINI.md` → gemini-cli,
`.continuerc.json` → continue-closed, etc.) and reads the service port
from user-scope state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result
from agentalloy.install.subcommands.wire_harness import (
    SENTINEL_BEGIN,
    SENTINEL_END,
    VALID_HARNESSES,
    wire_harness,
)
from agentalloy.providers.base import WireRecord
from agentalloy.signals.skill_loader import LIFECYCLE_MODES

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
    p.add_argument(
        "--lifecycle-mode",
        choices=LIFECYCLE_MODES,
        default=None,
        help=(
            "How AgentAlloy behaves in this repo. 'full' (default): intake + "
            "phase lifecycle. 'assist': defer to your own workflow — no intake "
            "front-door, keep skill suggestions. 'off': wire but inject nothing. "
            "When omitted and the repo already defines its own agents/commands, "
            "you're prompted (TTY only); non-interactive runs default to 'full'."
        ),
    )
    p.add_argument(
        "--clean-room",
        action="store_true",
        help=(
            "Claude Code only: also exclude your global ~/.claude/CLAUDE.md from "
            "THIS repo (writes claudeMdExcludes into .claude/settings.json). "
            "Off by default — this suppresses ALL your global directives here, "
            "not just conflicting ones."
        ),
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _redact_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return copies of *records* without ``original_content``.

    ``original_content`` is the verbatim prior config (e.g. ``~/.claude/settings.json``,
    which can hold secrets). It's persisted to ``install-state.json`` for
    unwire-restore, but must never reach stdout / ``--json``. Copies, so the
    on-disk state already saved by the wiring functions is untouched.
    """
    return [{k: v for k, v in r.items() if k != "original_content"} for r in records]


def _describe(f: dict[str, Any]) -> str:
    """One-line summary of a wired-file record (path + action) — never the raw dict."""
    path = f.get("path", "?")
    action = f.get("action")
    return f"{path}  [dim]({action})[/dim]" if action else str(path)


def _git_exclude_agentalloy(root: Path) -> None:
    """Append ``.agentalloy/`` to ``<root>/.git/info/exclude`` (idempotent).

    Uses the local, never-committed exclude file rather than touching a shared
    ``.gitignore``, so the per-repo phase/contract state can't be accidentally
    committed. No-op when there's no git repo. Best-effort: wiring never fails
    over this.
    """
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if any(line.strip() == ".agentalloy/" for line in existing.splitlines()):
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(existing + prefix + ".agentalloy/\n", encoding="utf-8")
    except OSError:
        pass


def _seed_entry_phase(root: Path) -> str | None:
    """Activate *root* by seeding the entry phase, returning the phase or None.

    Composition short-circuits (hook and proxy paths alike) when ``.agentalloy/
    phase`` is absent, so a wired-but-phaseless repo is inert. Seed ``intake``
    so the intent-interview workflow composes on the next prompt. Create-only:
    never clobber a repo already mid-lifecycle. Also git-excludes ``.agentalloy/``.
    """
    from agentalloy.install.subcommands.phase import _phase_path, run_phase_set  # noqa: PLC0415

    if _phase_path(root).exists():
        return None
    result = run_phase_set("intake", root=root)
    _git_exclude_agentalloy(root)
    return result.get("phase")


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
        print_rich(f"    [green]+[/green] {_describe(f)}")
    for f in files_modified:
        print_rich(f"    [yellow]~[/yellow] {_describe(f)}")

    if not files_written and not files_modified:
        print_rich("  [dim]No files to wire.[/dim]")

    phase_seeded = result.get("phase_seeded")
    if phase_seeded:
        print_rich(
            f"  Phase: [bold]{phase_seeded}[/bold] [dim](repo activated; composes next prompt)[/dim]"
        )

    detected = result.get("custom_workflow_detected")
    if detected:
        print_rich(f"  [dim]Detected your own workflow: {', '.join(detected)}[/dim]")

    mode = result.get("lifecycle_mode")
    if mode and mode != "full":
        note = (
            "defers to your workflow; keeps skill suggestions"
            if mode == "assist"
            else "wired, injection muted"
        )
        print_rich(f"  Lifecycle: [bold]{mode}[/bold] [dim]({note})[/dim]")

    if result.get("stale_phase_cleared"):
        print_rich("  [dim]Cleared a stale phase file (lifecycle is not full)[/dim]")

    if result.get("soft_precedence_note"):
        print_rich("  [dim].claude/CLAUDE.md note added (repo workflow loads last)[/dim]")

    if result.get("clean_room_excludes"):
        print_rich(
            "  [yellow]Clean-room:[/yellow] global ~/.claude/CLAUDE.md excluded from this repo "
            "[dim](suppresses ALL global directives here, not just conflicting ones)[/dim]"
        )

    print_rich()


def _detect_custom_workflow(root: Path) -> list[str]:
    """Return human-readable signals that *root* already defines its own agent
    workflow, so wiring can offer to defer rather than impose the lifecycle.

    Checks the Claude Code subagent/command locations plus the cross-harness
    ``AGENTS.md`` convention. Glob-only and never raises — an empty list means
    nothing was detected (wiring then defaults to ``full``).
    """
    signals: list[str] = []
    try:
        agents = sorted((root / ".claude" / "agents").glob("*.md"))
        if agents:
            signals.append(f".claude/agents/ ({len(agents)})")
        commands = sorted((root / ".claude" / "commands").glob("*.md"))
        if commands:
            signals.append(f".claude/commands/ ({len(commands)})")
        if (root / "AGENTS.md").is_file():
            signals.append("AGENTS.md")
    except OSError:
        return []
    return signals


def _prompt_lifecycle_mode(detected: list[str]) -> str:
    """Interactive numbered choice for the per-repo lifecycle mode.

    Only invoked when custom-workflow signals are detected AND stdin is a TTY.
    Mirrors the numbered-choice prompt pattern used elsewhere in the installer;
    EOF/interrupt or a blank line takes the default (``full``).

    The default is ``full`` on purpose: a ``.claude/agents/`` directory is
    near-ubiquitous and does NOT imply the user wants AgentAlloy's lifecycle
    disabled. Deferral (assist/off) must be an explicit choice — defaulting to
    assist here silently turned composition off for engaged users.
    """
    options: list[tuple[str, str]] = [
        ("full", "full — run AgentAlloy's intake + phase lifecycle (default)"),
        ("assist", "assist — defer to your workflow (no intake/phase); keep skill suggestions"),
        ("off", "off — wire the proxy/hooks but inject nothing"),
    ]
    print(
        f"\nThis repo already defines its own agent workflow ({', '.join(detected)}).",
        file=sys.stderr,
    )
    print("How should AgentAlloy behave here?", file=sys.stderr)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}", file=sys.stderr)
    print(file=sys.stderr)
    while True:
        try:
            raw = input(f"Choice [1-{len(options)}] (default 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return options[0][0]
        if raw == "":
            return options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(f"  Please enter a number between 1 and {len(options)}.", file=sys.stderr)


def _resolve_lifecycle_mode(args: argparse.Namespace, cwd: Path) -> tuple[str, list[str]]:
    """Resolve the effective lifecycle mode and the detection signals.

    Precedence: an explicit ``--lifecycle-mode`` flag always wins; otherwise,
    if the repo has its own workflow AND we're on a TTY, prompt; otherwise
    default ``full`` (preserving historical behavior for non-interactive runs
    and repos with no detected customization).
    """
    flag = getattr(args, "lifecycle_mode", None)
    detected = _detect_custom_workflow(cwd)
    if flag is not None:
        return flag, detected
    if detected and sys.stdin.isatty():
        return _prompt_lifecycle_mode(detected), detected
    return "full", detected


# ---------------------------------------------------------------------------
# Repo-local instruction shaping (claude-code): soft-precedence note + clean-room
# ---------------------------------------------------------------------------

# Loaded last by Claude Code (project memory), so it nudges — softly, by weight,
# not by enforcement — the lifecycle ahead of conflicting global directives. We
# own the file outright (a dedicated `./.claude/CLAUDE.md`), alongside any
# user-authored `./CLAUDE.md`, which still loads.
_SOFT_NOTE_INNER = (
    "**AgentAlloy is active in this repo.** It composes just-in-time skills and "
    "drives a spec→ship workflow through hooks. Where this repo's workflow "
    "guidance conflicts with global/user-level directives, prefer the repo "
    "workflow here. Managed by AgentAlloy — edits inside these markers are "
    "overwritten on re-wire."
)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_soft_precedence_note(root: Path) -> WireRecord | None:
    """Write a dedicated `./.claude/CLAUDE.md` soft-precedence note (full mode).

    We only ever own this file when it is absent or already carries our sentinel
    — a user-authored `./.claude/CLAUDE.md` is left untouched (returns None). As
    a dedicated file the unwire is trivial: ``wrote_new_file`` → deleted.
    """
    path = root / ".claude" / "CLAUDE.md"
    if path.exists() and SENTINEL_BEGIN not in path.read_text(encoding="utf-8"):
        return None  # user owns this file — don't clobber it
    block = f"{SENTINEL_BEGIN}\n{_SOFT_NOTE_INNER}\n{SENTINEL_END}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")
    return WireRecord(
        path=str(path),
        action="wrote_new_file",
        content_sha256=_sha256(block),
        original_content=None,
        marker_key="agentalloy.soft-precedence-note",
    )


def _write_clean_room_excludes(root: Path) -> WireRecord | None:
    """Opt-in: add the global ~/.claude/CLAUDE.md to this repo's claudeMdExcludes.

    Merges into `./.claude/settings.json`, preserving any existing keys and
    excludes. Returns None if the file exists but isn't a JSON object (we won't
    stomp something we can't safely merge). unwire restores the captured
    original (or deletes the file if we created it).
    """
    settings = root / ".claude" / "settings.json"
    global_md = str(Path.home() / ".claude" / "CLAUDE.md")
    if settings.exists():
        original: str | None = settings.read_text(encoding="utf-8")
        try:
            data: Any = json.loads(original)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
    else:
        original = None
        data = {}
    existing = data.get("claudeMdExcludes")
    excludes: list[Any] = list(existing) if isinstance(existing, list) else []
    if global_md not in excludes:
        excludes.append(global_md)
    data["claudeMdExcludes"] = excludes
    serialized = json.dumps(data, indent=2) + "\n"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(serialized, encoding="utf-8")
    return WireRecord(
        path=str(settings),
        action="wrote_new_file" if original is None else "injected_block",
        content_sha256=_sha256(serialized),
        original_content=original,
        marker_key="agentalloy.clean-room-excludes",
    )


def _persist_extra_records(root: Path, harness: str, records: list[WireRecord]) -> None:
    """Merge extra wire records into install-state so unwire reverses them.

    Mirrors ``apply_hook_wiring``'s merge, but preserves the prior entry's
    *original_content and action* across re-wires — the FIRST wire captured the
    true pre-install state; a re-wire re-reads our own block and must not let
    that masquerade as the original (which would leave the block behind).
    """
    if not records:
        return
    entries: list[dict[str, Any]] = []
    for rec in records:
        entry = rec.to_dict()
        entry.setdefault("harness", harness)
        entry.setdefault("repo_root", str(root))
        entries.append(entry)
    st = install_state.load_state(root)
    prior = st.get("harness_files_written") or []
    prior_by_path = {e.get("path"): e for e in prior}
    new_paths = {e.get("path") for e in entries}
    for entry in entries:
        prior_entry = prior_by_path.get(entry.get("path"))
        if prior_entry is not None:
            entry["original_content"] = prior_entry.get("original_content")
            entry["action"] = prior_entry.get("action", entry["action"])
    merged = [e for e in prior if e.get("path") not in new_paths] + entries
    st["harness_files_written"] = merged
    install_state.save_state(st, root)


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

    # Resolve and persist the per-repo lifecycle mode the hooks read on every
    # event. `assist`/`off` let a repo with its own agents/workflows opt out of
    # the intake front-door and phase forcing (the collision this guards).
    from agentalloy.signals.skill_loader import _write_lifecycle_mode

    mode, detected = _resolve_lifecycle_mode(args, cwd)
    _write_lifecycle_mode(cwd, mode)
    result["lifecycle_mode"] = mode
    if detected:
        result["custom_workflow_detected"] = detected

    if mode == "full":
        # Activate this repo: seed the entry phase so composition engages on the
        # next prompt. Without a phase file, both the hook and proxy paths
        # short-circuit and the repo stays inert (the "wired but nothing happens"
        # trap). Create-only — an already-phased repo is left untouched.
        phase_seeded = _seed_entry_phase(cwd)
        if phase_seeded:
            result["phase_seeded"] = phase_seeded
    else:
        # assist/off must NOT seed a phase (a seeded `intake` re-arms the front
        # door). Still git-exclude `.agentalloy/` — the config file lives there.
        _git_exclude_agentalloy(cwd)
        # Reconcile a stale phase file: an existing phase (e.g. `build` from a
        # prior `full` wiring) would otherwise sit alongside `lifecycle_mode:
        # assist` and silently suppress composition while looking active. The
        # lifecycle is off here, so the phase is meaningless — clear it.
        phase_file = cwd / ".agentalloy" / "phase"
        if phase_file.exists():
            phase_file.unlink()
            result["stale_phase_cleared"] = True

    # Repo-local instruction shaping (claude-code only). Best-effort — wiring
    # already succeeded, so never fail it over these. 1b soft note is full-only
    # (AgentAlloy driving); 1c clean-room is opt-in via --clean-room.
    extra: list[WireRecord] = []
    if harness == "claude-code":
        if mode == "full":
            note = _write_soft_precedence_note(cwd)
            if note is not None:
                extra.append(note)
                result["soft_precedence_note"] = note.path
        if getattr(args, "clean_room", False):
            cr = _write_clean_room_excludes(cwd)
            if cr is not None:
                extra.append(cr)
                result["clean_room_excludes"] = cr.path
    _persist_extra_records(cwd, harness, extra)

    # Restore data (original_content) is already persisted to install-state.json
    # by the wiring functions above; strip it from the command output so a prior
    # config holding secrets is never printed to stdout / emitted via --json.
    for key in ("files_written", "files_modified"):
        if isinstance(result.get(key), list):
            result[key] = _redact_records(result[key])

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
