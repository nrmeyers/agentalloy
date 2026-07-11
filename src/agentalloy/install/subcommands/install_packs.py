# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""``install-packs`` subcommand — interactive pack picker + bulk local install.

Runs after ``seed-corpus`` in the setup composer. Discovers in-tree packs
under ``seeds/packs/*/pack.yaml``, prompts the user (TTY) or applies a
sensible default (non-TTY), installs each selected pack locally, and
triggers one bulk reembed at the end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from agentalloy.install import state as install_state
from agentalloy.install.subcommands.install_pack import install_local_pack

SCHEMA_VERSION = 1
STEP_NAME = "install-packs"

# When the container reports an install-packs lock already in place, we wait
# this long before declaring the lock stale and proceeding.
_INSTALL_PACKS_STALE_SECONDS = 30 * 60


def _maybe_route_to_container(args: argparse.Namespace) -> int | None:
    """Forward ``install-packs`` into the running container, if applicable.

    Returns:
      - ``None`` — not routing; caller should run the local code path.
      - ``int``  — routing applied; return code from the container exec
        (or an error code if the container is not running / locked).

    The decision is driven by ``install-state.json``:
      * ``deployment == "container"`` → route into the container.
      * Any other value (or unset) → local path.

    Routing is suppressed when this process is itself running inside the
    container (``is_in_container()``) — the entrypoint script runs
    ``install-packs`` directly and must NOT recurse back into a container
    exec on itself.
    """
    # Local imports to keep module import time low (this function is on the
    # cold path; install_packs is also imported by tests that don't need
    # container plumbing).
    from agentalloy.install.container_service import is_in_container  # noqa: PLC0415
    from agentalloy.install.subcommands import container_runtime  # noqa: PLC0415

    if is_in_container():
        return None

    state = install_state.load_state()
    if state.get("deployment") != "container":
        return None

    # Prefer the runtime recorded at install time; if state lacks it, detect a
    # functional one rather than blindly assuming podman. The trailing "podman"
    # only guards against ``None.split()`` when nothing is detectable at all.
    runtime = (
        state.get("runtime_binary") or container_runtime._detect_runtime_binary() or "podman"
    ).split()[0]
    container_name = state.get("container_name") or "agentalloy"

    packs = getattr(args, "packs", None)
    if not packs:
        # No --packs given: routing into the container would be ambiguous
        # (the container can't run an interactive prompt against this TTY).
        # Fall through to local path so existing list/non-interactive flows
        # still work for diagnostic purposes.
        return None

    # Concurrent-install guard: respect ``.install-packs-lock`` inside the
    # container. A fresh lock means another install-packs is already running;
    # a stale lock (>30 min) is forcibly cleared so a crashed prior run
    # doesn't permanently block.
    lock_state = _read_container_install_lock(runtime, container_name)
    if lock_state == "fresh":
        print(
            "install-packs: another install-packs is already running inside "
            f"the {container_name} container. Wait for it to finish, or remove "
            "/app/.install-packs-lock manually if you know it's stale.",
            file=sys.stderr,
        )
        return 2
    if lock_state == "stale":
        # Best-effort cleanup; failure is non-fatal — the install command
        # will just touch the lock again.
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            [runtime, "exec", container_name, "rm", "-f", "/app/.install-packs-lock"],
            check=False,
            capture_output=True,
            timeout=10,
        )

    cmd = [
        runtime,
        "exec",
        container_name,
        "sh",
        "-c",
        # Atomic lock creation: `set -C` + `: >` fails if the file already
        # exists (EEXIST), preventing two concurrent install-packs from both
        # proceeding. `set -e` ensures the EXIT trap fires on any failure.
        # `trap 'rm -f ...' EXIT` removes the lock when the shell exits.
        (
            "set -e; "
            "set -C; "
            ": > /app/.install-packs-lock || exit 1; "
            "trap 'rm -f /app/.install-packs-lock' EXIT; "
            f"uv run agentalloy install-packs --packs {_shell_quote(packs)}"
            + (" --no-restart" if getattr(args, "no_restart", False) else "")
            + (" --ignore-unknown" if getattr(args, "ignore_unknown", False) else "")
        ),
    ]
    try:
        result = subprocess.run(cmd, check=False, timeout=3600)  # noqa: S603
        return result.returncode
    except FileNotFoundError:
        print(
            f"install-packs: container runtime '{runtime}' not found on PATH.",
            file=sys.stderr,
        )
        return 3
    except subprocess.TimeoutExpired:
        print(
            "install-packs: container install timed out after 1 hour.",
            file=sys.stderr,
        )
        return 4


def _read_container_install_lock(runtime: str, container_name: str) -> str:
    """Return ``"missing"``, ``"fresh"``, ``"stale"``, or ``"error"``.

    ``"error"`` covers "container not running" / "runtime missing" —
    surfaced as a hard failure by the caller via a separate exec attempt.
    """
    # ``stat -c %Y`` returns mtime as a Unix timestamp; an empty stdout means
    # the file doesn't exist (stat exits non-zero with --quiet, but we use
    # plain stat which writes to stderr — we just check the rc).
    proc = subprocess.run(  # noqa: S603
        [runtime, "exec", container_name, "stat", "-c", "%Y", "/app/.install-packs-lock"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace").lower()
        if "no such file" in stderr or "cannot stat" in stderr:
            return "missing"
        # Container not running, or some other exec failure.
        return "error"
    try:
        mtime = int(proc.stdout.decode().strip())
    except (ValueError, AttributeError):
        return "error"
    age = int(time.time()) - mtime
    return "stale" if age > _INSTALL_PACKS_STALE_SECONDS else "fresh"


def _shell_quote(value: str) -> str:
    """Single-quote a string for safe embedding in an `sh -c` argument."""
    import shlex  # noqa: PLC0415

    return shlex.quote(value)


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "install-packs",
        help="Interactive pack picker + bulk install (called by `setup`).",
    )
    p.add_argument(
        "--packs",
        help="Comma-separated pack names. Skips the interactive picker. Use 'all' for every pack.",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Force non-TTY mode (install only always-on packs unless --packs is given).",
    )
    p.add_argument(
        "--ignore-unknown",
        action="store_true",
        help=(
            "Continue with the known subset when --packs lists names that "
            "don't exist (default: fail with the available pack list)."
        ),
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Print available pack names (one per line) and exit.",
    )
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart the agentalloy service after bulk reembed",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the full result as JSON (default: a one-line summary; full detail "
        "is always written to install-packs.json).",
    )
    p.set_defaults(func=_run)


def _ensure_skill_schema() -> None:
    """Create the skill-store schema if it's missing. Best-effort.

    A ``setup --force`` re-run can leave corpus files on disk without the
    skill-graph tables (issue #84); every subsequent ingest then fails with
    "Table skills does not exist" and the pack rolls back. ``migrate()`` is
    idempotent, so running it up front is safe. Failures (e.g. the DB
    lock is held by a concurrent writer) are non-fatal — the per-skill
    ingest errors will surface them.
    """
    from agentalloy.config import get_settings
    from agentalloy.install.subcommands.install_pack import LOCK_HELD_REMEDIATION
    from agentalloy.storage.open import open_skills
    from agentalloy.storage.skill_store import is_lock_held_error

    store = None
    try:
        settings = get_settings()
        store = open_skills(settings, read_only=False)
        store.migrate()
    except Exception as exc:  # noqa: BLE001 — best-effort guard, ingest surfaces real failures
        print(f"WARN: could not verify/create corpus graph schema: {exc}", file=sys.stderr)
        if is_lock_held_error(str(exc)):
            print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)
    finally:
        if store is not None:
            store.close()


def _summarize_install_result(result: dict[str, Any]) -> dict[str, Any]:
    """Strip bulky per-skill ingest detail from a pack result, keeping failures.

    Successful ``ingest_results`` entries are noise in install-packs.json,
    but failed ones carry the only per-skill diagnostics (``stderr_tail``)
    — dropping them left users staring at "Failures: N" (issue #84).
    """
    out = {k: v for k, v in result.items() if k != "ingest_results"}
    failed = [
        {
            "yaml": r.get("yaml"),
            "exit_code": r.get("exit_code"),
            "stderr_tail": r.get("stderr_tail"),
        }
        for r in result.get("ingest_results") or []
        if r.get("outcome") == "failed"
    ]
    if failed:
        out["failed_ingest_results"] = failed[:10]
    return out


def _render_install_summary(summary: dict[str, Any]) -> str:
    """One-line human summary of a packs_installed result.

    The full per-pack detail is always written to install-packs.json; stdout
    gets this digest unless ``--json`` (full blob) or ``--quiet`` (silent).
    """
    results: list[dict[str, Any]] = summary.get("install_results") or []

    def _total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in results)

    ingested = sum(1 for r in results if r.get("action") in ("ingested", "ingested_with_errors"))
    already = sum(1 for r in results if r.get("action") == "already_installed")
    failed = list(summary.get("failed_packs") or [])
    reembed_rc = summary.get("reembed_exit_code")
    reembed = "ok" if reembed_rc in (0, None) else f"exit {reembed_rc}"
    secs = (summary.get("duration_ms") or 0) / 1000
    line = (
        f"install-packs: {len(summary.get('selected') or [])} packs "
        f"({ingested} ingested, {already} already present, {len(failed)} failed) | "
        f"skills: +{_total('skills_ingested')} ingested, {_total('skills_already_present')} present, "
        f"{_total('skills_deprecated')} deprecated, {_total('ingest_failures')} failures | "
        f"reembed: {reembed} | {secs:.1f}s — full detail in install-packs.json"
    )
    if failed:
        line += f"\n  failed packs: {', '.join(failed)}"
    return line


def _packs_dir() -> Path:
    """Return the directory containing pack manifests.

    Resolves to ``src/agentalloy/_packs/`` in both editable and wheel
    installs (the path is the same because editable installs point
    Python at the repo's `src/agentalloy/` directly).
    """
    import agentalloy

    return Path(agentalloy.__file__).resolve().parent / "_packs"


def _run(args: argparse.Namespace) -> int:
    from agentalloy.install.state import pack_source_dir

    # Container routing: if this is a container deployment AND we're NOT
    # currently running inside the container, the corpus the user wants to
    # write to lives in the container's data volume — running install-packs
    # on the host would scribble onto the wrong (empty) corpus and fail.
    # Forward the command into the running container instead.
    rc = _maybe_route_to_container(args)
    if rc is not None:
        return rc

    root = pack_source_dir()
    root.mkdir(parents=True, exist_ok=True)
    packs_root = _packs_dir()

    available = _discover_packs(packs_root)

    if getattr(args, "list", False):
        for name in sorted(available):
            meta = available[name]
            always = " [always-on]" if meta.get("always_install") else ""
            print(f"{name}{always}")
        return 0

    if not available:
        print("install-packs: no packs found under seeds/packs/", file=sys.stderr)
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "no_packs_available",
            "packs_root": str(packs_root),
        }
        if not getattr(args, "quiet", False):
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 1

    interactive = sys.stdin.isatty() and not args.non_interactive
    selected, unknown, consumed_pending = _select_packs(
        available, args.packs, interactive=interactive
    )

    if unknown and not args.ignore_unknown:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "unknown_packs",
            "unknown": sorted(unknown),
            "available": sorted(available),
        }
        if not getattr(args, "quiet", False):
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
        print(
            f"install-packs: unknown pack(s): {sorted(unknown)}",
            file=sys.stderr,
        )
        print(
            "FIX:   re-run with valid pack names (see `available` above), "
            "or pass --ignore-unknown to skip them.",
            file=sys.stderr,
        )
        return 1
    if unknown and args.ignore_unknown:
        print(
            f"install-packs: ignoring unknown pack(s): {sorted(unknown)}",
            file=sys.stderr,
        )

    print(f"install-packs: installing {len(selected)} pack(s)", file=sys.stderr)
    t0 = time.monotonic()
    failed: list[str] = []

    # T1: single container stop/restart wrapping all ingests + reembed.
    # _run_container_guard() owns the AGENTALLOY_DB_LOCK_HELD lifecycle;
    # child ingest subprocesses inherit the sentinel via POSIX env and no-op.
    install_results, named_results, reembed_rc = _run_container_guard(
        args, selected, packs_root, root
    )

    for pack_name, r in named_results:
        if r.get("action") not in ("ingested", "ingested_with_errors", "already_installed"):
            failed.append(pack_name)  # use original pack name, not a path from the result dict

    duration_ms = int((time.monotonic() - t0) * 1000)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "action": "packs_installed" if not failed else "packs_partial",
        "selected": selected,
        "failed_packs": failed,
        "install_results": [_summarize_install_result(r) for r in install_results],
        "reembed_exit_code": reembed_rc,
        "duration_ms": duration_ms,
    }
    install_state.save_output_file(summary, "install-packs.json")
    if getattr(args, "json", False):
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif not getattr(args, "quiet", False):
        print(_render_install_summary(summary))

    if reembed_rc != 0:
        print(
            "WARN: bulk reembed exited non-zero. Some fragments may lack embeddings; "
            "run `agentalloy reembed` again to retry. Vector retrieval will skip "
            "unembedded fragments until then.",
            file=sys.stderr,
        )

    # Lock-held detection: if any per-skill ingest failure looks like the
    # skill store's single-writer lock error, surface the retry hint instead
    # of leaving the user to decode "Failures: N" (issue #84).
    from agentalloy.install.subcommands.install_pack import LOCK_HELD_REMEDIATION
    from agentalloy.storage.skill_store import is_lock_held_error

    if any(
        is_lock_held_error(str(ir.get("stderr_tail") or ""))
        for r in install_results
        for ir in r.get("ingest_results") or []
        if ir.get("outcome") == "failed"
    ):
        print(f"FIX:   {LOCK_HELD_REMEDIATION}", file=sys.stderr)

    # Clear the setup-wizard pack selection once we've acted on it, so a
    # later standalone `agentalloy install-packs` re-prompts the user with
    # the same UX (showing already-installed packs in the picker).
    if consumed_pending and not failed:
        _clear_pending_pack_selection()

    # Exit code reflects pack-level ingest failures only. Reembed failures
    # are surfaced via the stderr WARN above and ``reembed_exit_code`` in
    # the saved install-packs.json summary — strict callers can inspect
    # the summary. Conflating reembed failure into rc would regress the
    # native setup flow inside ``run_setup`` (in
    # ``agentalloy.install.subcommands.simple_setup``), which treats any
    # non-zero return from ``install_packs.run`` as a fatal abort. The
    # wizard's container branch already captures install-packs' stderr and
    # surfaces the WARN line to users.
    return 0 if not failed else 1


def _discover_packs(packs_root: Path) -> dict[str, dict[str, Any]]:
    """Return {pack_name: manifest} for every seeds/packs/*/pack.yaml."""
    out: dict[str, dict[str, Any]] = {}
    if not packs_root.is_dir():
        return out
    for pack_dir in sorted(packs_root.iterdir()):
        if not pack_dir.is_dir():
            continue
        manifest_path = pack_dir / "pack.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        name = str(manifest.get("name") or pack_dir.name)
        out[name] = manifest
    return out


def _select_packs(
    available: dict[str, dict[str, Any]],
    packs_flag: str | None,
    *,
    interactive: bool,
) -> tuple[list[str], list[str], bool]:
    """Pick packs honoring priority: --packs > pending-state > TTY prompt > defaults.

    Returns ``(selected, unknown, consumed_pending)``. ``unknown`` is the
    list of names from ``--packs`` that don't match any available pack —
    caller decides whether to fail or continue. ``consumed_pending`` is
    True iff a ``pending_pack_selection`` from setup was applied; caller
    should clear it from state after a successful install.
    """
    always_on = [n for n, m in available.items() if m.get("always_install")]
    unknown: list[str] = []

    # Explicit --packs flag wins
    if packs_flag:
        if packs_flag.strip().lower() == "all":
            chosen = list(available)
        else:
            requested = [p.strip() for p in packs_flag.split(",") if p.strip()]
            unknown = [p for p in requested if p not in available]
            chosen = [p for p in requested if p in available]
        # Always include always-on packs even if user didn't list them.
        return _ordered_with_deps(set(chosen) | set(always_on), available), unknown, False

    # Pending selection from setup wizard (written by simple_setup before
    # ever calling install-packs). An empty list still counts as explicit
    # intent — "user picked nothing extra, install always-on only".
    pending = _load_pending_pack_selection()
    if pending is not None:
        chosen = [p for p in pending if p in available]
        unknown = [p for p in pending if p not in available]
        return _ordered_with_deps(set(chosen) | set(always_on), available), unknown, True

    if not interactive:
        # Non-TTY default: only install always-on packs.
        return _ordered_with_deps(set(always_on), available), unknown, False

    # Interactive multi-select.
    chosen = _prompt_for_packs(available, always_on)
    return _ordered_with_deps(set(chosen) | set(always_on), available), unknown, False


def _load_pending_pack_selection() -> list[str] | None:
    """Read ``pending_pack_selection`` from install-state, safely.

    Best-effort: a malformed/missing state file means "no pending
    selection", not a crash. This runs in the install pipeline where
    failing early on state-read errors would block users from
    re-installing.
    """
    try:
        data = install_state.load_state()
    except Exception:  # noqa: BLE001
        return None
    return install_state.get_pending_pack_selection(data)


def _clear_pending_pack_selection() -> None:
    """Wipe ``pending_pack_selection`` after install-packs consumed it.

    Best-effort: matches the load helper. A standalone re-run of
    install-packs (no pending state on disk) goes through the
    interactive prompt with already-installed annotations.
    """
    try:
        data = install_state.load_state()
        install_state.clear_pending_pack_selection(data)
        install_state.save_state(data)
    except Exception:  # noqa: BLE001
        # Non-fatal: leaving a stale pending selection just causes the
        # NEXT install-packs run to skip prompting, which is annoying
        # but not destructive.
        pass


def _installed_pack_names() -> set[str]:
    """Return the set of pack names previously recorded as installed.

    Used to annotate the interactive picker with [installed] markers.
    Returns an empty set on any read error.
    """
    try:
        data = install_state.load_state()
    except Exception:  # noqa: BLE001
        return set()
    packs = data.get("installed_packs") or []
    names: set[str] = set()
    for p in packs:
        if isinstance(p, str):
            names.add(p)
        elif isinstance(p, dict):
            names.add(str(p.get("name", "")))
    return names


_TIER_ORDER: tuple[str, ...] = (
    "foundation",
    "language",
    "framework",
    "tooling",
    "protocol",
    "store",
    "platform",
    "domain",
    "workflow",
    "other",
)
_TIER_LABELS: dict[str, str] = {
    "foundation": "Foundation",
    "language": "Languages",
    "framework": "Frameworks",
    "tooling": "Tooling",
    "protocol": "Protocols",
    "store": "Data Stores",
    "platform": "Platforms",
    "domain": "Domain",
    "workflow": "Workflows",
    "other": "Other",
}


def _prompt_for_packs(
    available: dict[str, dict[str, Any]],
    always_on: list[str],
) -> list[str]:
    """Show packs grouped by tier and accept a comma-separated selection.

    Mirrors the setup wizard's pack picker (``simple_setup._prompt_for_packs``)
    so re-running install-packs feels identical to first-time setup. Accepts
    pack names, tier names (case-insensitive display label or internal key),
    or ``all`` / ``defaults`` / blank for always-on only.

    Packs that were recorded in a prior install are annotated ``[installed]``.
    Selecting them again is a no-op: ``install_local_pack`` reports an
    ``already_installed`` action when every skill in the pack is already
    in the corpus. The marker just spares the user from guessing.
    """
    if not available:
        return []

    installed = _installed_pack_names()

    # Group by tier, retaining (name, skill_count, always_on, installed).
    tiers: dict[str, list[tuple[str, int, bool, bool]]] = {}
    for name, m in available.items():
        tier = str(m.get("tier") or "other")
        skill_count = len(m.get("skills") or [])
        is_always = bool(m.get("always_install"))
        is_installed = name in installed
        tiers.setdefault(tier, []).append((name, skill_count, is_always, is_installed))

    # Reverse lookup so users can type "Languages" or "language" interchangeably.
    label_to_tier = {v.lower(): k for k, v in _TIER_LABELS.items()}

    print("\n=== Available skill packs ===\n", file=sys.stderr)
    pack_index: list[str] = []  # flat list for numeric selection
    for tier in _TIER_ORDER:
        rows = tiers.get(tier)
        if not rows:
            continue
        label = _TIER_LABELS.get(tier, tier.title())
        print(f"  [{label}]", file=sys.stderr)
        for name, skill_count, is_always, is_installed in sorted(rows, key=lambda x: x[0]):
            markers: list[str] = []
            if is_always:
                markers.append("always-on")
            if is_installed:
                markers.append("installed")
            marker_str = f"  ({', '.join(markers)})" if markers else ""
            print(
                f"    - {name:22} {skill_count:>3} skills{marker_str}",
                file=sys.stderr,
            )
            pack_index.append(name)
        print("", file=sys.stderr)

    print(
        f"  Always-on (auto-installed): {', '.join(sorted(always_on)) or '(none)'}",
        file=sys.stderr,
    )
    visible_tiers = [_TIER_LABELS.get(t, t) for t in _TIER_ORDER if t in tiers]
    if visible_tiers:
        print(
            "\n  Tip: You can also use tiers (comma-separated):",
            file=sys.stderr,
        )
        print(f"    {', '.join(visible_tiers)}", file=sys.stderr)
    print(
        "\n  Enter pack or tier names (comma-separated), 'all', or blank for always-on only.",
        file=sys.stderr,
    )

    try:
        raw = input("Packs to install: ").strip()
    except (EOFError, KeyboardInterrupt):
        return []

    if not raw or raw.lower() == "defaults":
        return []
    if raw.lower() == "all":
        return list(pack_index)

    chosen: list[str] = []
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        # Tier-based selection: match internal key or display label (case-insensitive).
        tier_key: str | None = None
        if t in tiers:
            tier_key = t
        elif t.lower() in label_to_tier:
            tier_key = label_to_tier[t.lower()]
        if tier_key is not None and tier_key in tiers:
            chosen.extend(name for name, _, _, _ in tiers[tier_key])
        elif t in available:
            chosen.append(t)
        elif t.isdigit() and 1 <= int(t) <= len(pack_index):
            chosen.append(pack_index[int(t) - 1])
        else:
            print(f"  ignoring unknown pack: {t}", file=sys.stderr)

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for name in chosen:
        if name not in seen:
            seen.add(name)
            deduped.append(name)
    return deduped


def _ordered_with_deps(
    chosen: set[str],
    available: dict[str, dict[str, Any]],
) -> list[str]:
    """Topological order: dependencies before dependents. Adds missing deps.

    Warns when a pack declares a dependency on a pack that isn't available.
    Without the warning, missing deps were silently ignored — masking
    misconfigurations until runtime.
    """
    closed: set[str] = set()
    missing_deps: list[tuple[str, str]] = []  # (declarant, missing_dep)
    work = list(chosen)
    while work:
        name = work.pop()
        if name in closed:
            continue
        closed.add(name)
        for dep in available.get(name, {}).get("depends_on") or []:
            if dep not in available:
                missing_deps.append((name, dep))
                continue
            if dep not in closed:
                work.append(dep)

    for declarant, dep in missing_deps:
        print(
            f"WARN: pack '{declarant}' declares depends_on '{dep}', "
            f"but that pack is not available — proceeding without it.",
            file=sys.stderr,
        )

    # Simple DFS-based topo sort
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(n: str) -> None:
        if n in visited:
            return
        visited.add(n)
        for d in available.get(n, {}).get("depends_on") or []:
            if d in closed:
                visit(d)
        ordered.append(n)

    for n in sorted(closed):
        visit(n)
    return ordered


def _reclaim_native_corpus_lock() -> bool:
    """Free the corpus DB lock from our own running native service before ingest.

    Stops the systemd ``--user`` unit (so it doesn't respawn) and kills any stale
    ``uvicorn agentalloy.app`` still squatting the service port. Mirrors
    ``enable_service._reclaim_port``. Returns True if a systemd unit was stopped
    (the caller restarts it afterwards). No-op returning False when the port is
    already free or held by a foreign process — so fresh installs and the
    setup-managed path are unaffected.
    """
    import shutil

    from agentalloy.install.server_proc import (
        configured_port,
        find_listening_pid,
        reclaim_stale_port,
    )

    port = configured_port()
    if find_listening_pid(port) is None:
        return False  # nothing holding the port → lock is free
    stopped_unit = False
    if shutil.which("systemctl"):
        rc = subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
            ["systemctl", "--user", "stop", "agentalloy.service"],
            check=False,
            capture_output=True,
            text=True,
        )
        stopped_unit = rc.returncode == 0  # 0 = unit existed and is now stopped
    # Clear any orphaned/manual holder the unit-stop didn't cover.
    reclaim_stale_port(port, ["uvicorn", "agentalloy.app"])
    return stopped_unit


def _restart_native_service() -> None:
    """Best-effort restart of the systemd ``--user`` unit after install-packs."""
    import shutil

    if shutil.which("systemctl"):
        subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
            ["systemctl", "--user", "start", "agentalloy.service"],
            check=False,
            capture_output=True,
            text=True,
        )


def _run_container_guard(
    args: argparse.Namespace,
    selected: list[str],
    packs_root: Path,
    root: Path,
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]], int]:
    """Install packs with a single container stop/restart surrounding all DB writes.

    Owns the AGENTALLOY_DB_LOCK_HELD lifecycle for the full install-packs
    invocation: stop once, run all ingests with no_restart=True (sentinel
    handles child processes via POSIX env inheritance), reembed once, restart.

    P10-R2: for-loop bounded by len(selected) ≤ total registry pack count.
    P10-R4: ≤40 lines.
    """
    from agentalloy.install.container_service import (
        is_in_container,
        restart_service_in_container,
        stop_service_in_container,
    )
    from agentalloy.reembed.cli import run_bulk_reembed

    no_restart: bool = getattr(args, "no_restart", False)
    if not isinstance(no_restart, bool):
        raise TypeError(f"no_restart must be bool, got {type(no_restart).__name__}")

    container_stopped: bool = False
    native_unit_stopped: bool = False
    if is_in_container() and not no_restart:
        container_stopped = stop_service_in_container()
        if not isinstance(container_stopped, bool):
            raise TypeError(
                f"stop_service_in_container must return bool, got {type(container_stopped).__name__}"
            )
        if container_stopped:
            print(
                "[agentalloy] Service stopped; ingesting packs with --no-restart", file=sys.stderr
            )
    elif not no_restart:
        # Native: free the corpus DB lock from our own running service before the
        # ingest (else it spews per-skill lock WARNs and leaves a partial corpus).
        native_unit_stopped = _reclaim_native_corpus_lock()
        if native_unit_stopped:
            print(
                "[agentalloy] Stopped agentalloy.service to free the corpus lock; "
                "will restart it after.",
                file=sys.stderr,
            )

    # Schema guard (issue #84): a wiped-then-recreated corpus can have DB
    # files on disk without the skill-graph tables; ingest would fail on every
    # skill with "Table skills does not exist". Migrations are idempotent.
    _ensure_skill_schema()

    # list[tuple[pack_name, result]] so _run() can build failed list by name, not path
    named_results: list[tuple[str, dict[str, Any]]] = []
    reembed_rc: int = 0
    try:
        # Deliberately NOT routed through /corpus/ingest-pack (#390): install-packs
        # is the bulk-bootstrap path and already writes the corpus with the service
        # up — this guard owns the stop→ingest→restart lifecycle. Unlike promote /
        # install-pack (which just hit the lock and failed, hence #390), install-packs
        # is not the pain point; routing it per-pack would add reembed-batching and
        # container-guard regression surface on the highest-stakes path (a bug here
        # breaks the whole corpus) to remove only a brief restart blip. See
        # docs/followups.md if that blip ever becomes a real complaint.
        for pack_name in selected:  # P10-R2: bounded by len(selected)
            pack_dir = packs_root / pack_name
            print(f"  → {pack_name}", file=sys.stderr, flush=True)
            # strict=False: the bundled corpus predates the lint gate and isn't
            # guaranteed --strict-clean; only the third-party install-pack path
            # (install_local_pack's own default) opts into strict lint.
            # run_reembed=False: this guard reembeds exactly once for the whole
            # run (below), not once per pack — install_local_pack's own default
            # (run_reembed=True) is for its standalone third-party caller.
            r = install_local_pack(
                pack_dir, root=root, no_restart=True, strict=False, run_reembed=False
            )
            named_results.append((pack_name, r))
        # no_restart=True: reembed does NOT restart — this guard owns the lifecycle.
        # allow_duplicates not passed: install-packs behavior is unchanged — a
        # hard cross-pack duplicate in the bundled corpus still fails the pass.
        reembed_rc = run_bulk_reembed(no_restart=True)
    finally:
        if container_stopped and not no_restart:
            ok: bool = restart_service_in_container()
            if not isinstance(ok, bool):
                raise TypeError(
                    f"restart_service_in_container must return bool, got {type(ok).__name__}"
                )
            if not ok:
                print(
                    "[agentalloy] WARNING: Failed to restart service after install-packs. "
                    "Restart the container manually "
                    "(`docker restart agentalloy` or `podman restart agentalloy`).",
                    file=sys.stderr,
                )
        if native_unit_stopped:
            _restart_native_service()

    return [r for _, r in named_results], named_results, reembed_rc


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers (e.g. simple_setup)."""
    return _run(args)
