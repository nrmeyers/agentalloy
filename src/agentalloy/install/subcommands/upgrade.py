"""``upgrade`` subcommand — one command to the latest release.

Operator-tier. Where ``update`` is a *diagnostic* (corpus schema migrations +
model-drift report, no wheel/image swap), ``upgrade`` is the orchestrator that
actually moves an install to the newest tagged release:

  1. Resolve the current version (``agentalloy.__version__``) and the latest
     release tag (GitHub releases API). No-op if already current (``--force``
     overrides; ``--ref`` pins a specific tag).
  2. Branch on ``deployment`` recorded in ``install-state.json``:
       * **native**  — stop the service, re-install the package at the tag
         (``uv tool install --force git+…@<tag>``; pip fallback; a source/editable
         checkout is left to ``git pull``), re-ingest changed packs, re-embed
         only if the embedding dimension changed (prompted), then restart + verify.
       * **container** — bump the CLI in lock-step, pull the new image, and
         recreate the container; the image entrypoint self-heals the corpus by
         re-seeding when its ``corpus-stamp.json`` differs from the volume's.

Heavy lifting reuses existing idempotent subcommands (``install-packs``,
``reembed``, ``update``, ``verify``) by shelling the freshly-installed
``agentalloy`` binary — so the post-swap steps run the *new* code without an
in-process re-exec.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1
STEP_NAME = "upgrade"

_REPO = "nrmeyers/agentalloy"
_GIT_URL = "https://github.com/nrmeyers/agentalloy.git"
_RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
# Substrings that mark an embedding-dimension mismatch surfaced by install-packs
# or the startup guard — the signal that a full re-embed is required.
_DIM_MISMATCH_MARKERS = ("embedding_dim", "EmbeddingDimMismatch", "-dim embeddings", "dimension")


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def _parse_semver(value: str) -> tuple[int, int, int]:
    """Parse ``vX.Y.Z`` / ``X.Y.Z`` into a comparable tuple (extras ignored)."""
    core = value.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    nums: list[int] = []
    for part in core.split(".")[:3]:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _current_version() -> str:
    from agentalloy import __version__

    return __version__


def _installed_version_via_cli() -> str | None:
    """Read the version from the freshly-installed binary (out-of-process).

    After a package swap the in-process ``agentalloy.__version__`` is still the
    *old* version — the running interpreter imported it before the swap and
    Python caches the module — so reading it in-process reports the pre-upgrade
    version. Shell the new binary to learn what actually landed.
    """
    try:
        proc = _run_cli(["--version"], capture=True)
    except (OSError, subprocess.SubprocessError):
        return None
    # Output format: "agentalloy X.Y.Z"
    parts = (proc.stdout or "").strip().split()
    return parts[-1] if parts else None


def _latest_release_tag(timeout: float = 10.0) -> str | None:
    """Return the newest release tag (e.g. ``v2.2.1``), or ``None`` if unreachable."""
    req = urllib.request.Request(
        _RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "agentalloy-upgrade"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) and tag else None


# ---------------------------------------------------------------------------
# Install-method detection + package swap (native)
# ---------------------------------------------------------------------------


def _detect_install_method() -> str:
    """Classify how the running ``agentalloy`` was installed.

    Returns ``"source"`` (dev/editable checkout — don't touch it), ``"uv-tool"``
    (the documented native install), or ``"pip"`` (plain pip env).
    """
    if _current_version() == "0.0.0+unknown":
        return "source"
    import agentalloy

    pkg_dir = Path(agentalloy.__file__).resolve().parent
    probe = pkg_dir
    for _ in range(6):
        if (probe / ".git").exists():
            return "source"
        probe = probe.parent
    try:
        out = subprocess.run(
            ["uv", "tool", "list"], capture_output=True, text=True, timeout=15, check=False
        )
        if out.returncode == 0 and "agentalloy" in out.stdout:
            return "uv-tool"
    except (OSError, subprocess.SubprocessError):
        pass
    return "pip"


def _swap_command(method: str, ref: str) -> list[str]:
    """Build the package re-install command for ``method`` at git ``ref``."""
    target = f"git+{_GIT_URL}@{ref}"
    if method == "uv-tool":
        return ["uv", "tool", "install", "--force", target]
    return [sys.executable, "-m", "pip", "install", "--upgrade", target]


# ---------------------------------------------------------------------------
# Service control (native)
# ---------------------------------------------------------------------------


def _systemd_unit(name: str) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "systemd" / "user" / name


def _is_systemd() -> bool:
    return _systemd_unit("agentalloy.service").exists()


def _systemctl(*args: str) -> int:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def _stop_service() -> str:
    """Stop the main API service before swapping the package. Returns the mode."""
    if _is_systemd():
        _systemctl("stop", "agentalloy.service")
        return "systemd"
    _run_cli(["server-stop"], check=False)
    return "manual"


def _start_inference_servers() -> None:
    """Ensure the embed/rerank llama-servers are up (needed for ingest/re-embed)."""
    if _is_systemd():
        _systemctl("start", "agentalloy-embed.service", "agentalloy-rerank.service")


def _start_service() -> None:
    """Start the main API service after the corpus is consistent."""
    if _is_systemd():
        _systemctl("restart", "agentalloy.service")
    else:
        _run_cli(["server-start"], check=False)


# ---------------------------------------------------------------------------
# CLI shelling (post-swap steps run the *new* binary)
# ---------------------------------------------------------------------------


def _run_cli(
    args: list[str], *, check: bool = False, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Invoke ``agentalloy <args>`` as a subprocess (resolves the new binary)."""
    return subprocess.run(
        ["agentalloy", *args],
        capture_output=capture,
        text=True,
        timeout=3600,
        check=check,
    )


def _is_dim_mismatch(proc: subprocess.CompletedProcess[str]) -> bool:
    """True when a captured subprocess failed due to an embedding-dim mismatch."""
    if proc.returncode == 0:
        return False
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    return any(marker in blob for marker in _DIM_MISMATCH_MARKERS)


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [Y/n]: ").strip().lower() not in ("n", "no")
    except (EOFError, KeyboardInterrupt):
        return False


# ---------------------------------------------------------------------------
# Native upgrade
# ---------------------------------------------------------------------------


def _installed_packs(state: dict[str, Any]) -> str:
    packs = state.get("installed_packs") or []
    names = [p for p in packs if isinstance(p, str)]
    return ",".join(names) if names else "all"


def _upgrade_native(
    ref: str, state: dict[str, Any], *, assume_yes: bool
) -> tuple[list[str], list[str]]:
    """Run the native upgrade. Returns (actions, warnings)."""
    actions: list[str] = []
    warnings: list[str] = []

    method = _detect_install_method()
    if method == "source":
        warnings.append(
            "Running from a source/editable checkout — not swapping the package. "
            "Update with `git pull` (then `uv sync`) instead."
        )
        return actions, warnings

    mode = _stop_service()
    actions.append(f"stopped service ({mode})")

    swap = _swap_command(method, ref)
    print_rich(f"  [dim]-> {' '.join(swap)}[/dim]")
    try:
        subprocess.run(swap, check=True, timeout=1800, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()[-3:]
        detail = f": {' / '.join(line.strip() for line in tail)}" if tail else ""
        warnings.append(
            f"package install failed (exit {exc.returncode}); service left stopped{detail}"
        )
        return actions, warnings
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"package install failed: {exc}")
        return actions, warnings
    actions.append(f"installed {ref} via {method}")

    # Inference servers must be up for pack ingest / re-embed.
    _start_inference_servers()

    packs = _installed_packs(state)
    ingest = _run_cli(["install-packs", "--packs", packs, "--no-restart"], capture=True)
    if _is_dim_mismatch(ingest):
        warnings.append("embedding dimension changed — a full re-embed is required")
        if _confirm(
            "  Re-embed the whole corpus now? This can take 30–40 min on CPU",
            assume_yes=assume_yes,
        ):
            _run_cli(["reembed", "--force", "--no-restart"], check=False)
            _run_cli(["install-packs", "--packs", packs, "--no-restart"], check=False)
            actions.append("re-embedded corpus (--force)")
        else:
            warnings.append("re-embed skipped — the service may refuse to start until you run it")
    else:
        actions.append("re-ingested packs")

    # corpus schema migrations + model-drift report. Capture the JSON output so it
    # does not spill to the terminal; surface only its warnings, cleanly.
    upd = _run_cli(["update"], check=False, capture=True)
    actions.append("ran corpus migrations")
    try:
        upd_payload: dict[str, Any] = json.loads(upd.stdout or "{}")
        upd_warnings = upd_payload.get("warnings")
        if isinstance(upd_warnings, list):
            warnings.extend(w for w in upd_warnings if isinstance(w, str))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    except (json.JSONDecodeError, ValueError):
        pass

    _start_service()
    actions.append("restarted service")
    return actions, warnings


# ---------------------------------------------------------------------------
# Container upgrade
# ---------------------------------------------------------------------------


def _target_image(current_tag: str | None, version: str) -> str:
    """Pin the image to the release ``version``, preserving registry + -full variant."""
    from agentalloy.install.subcommands.container_runtime import _DEFAULT_IMAGE

    base_ref = current_tag or _DEFAULT_IMAGE
    repo = base_ref.rsplit(":", 1)[0]
    suffix = "-full" if base_ref.endswith("-full") else ""
    return f"{repo}:{version.lstrip('v')}{suffix}"


def _upgrade_container(
    ref: str, state: dict[str, Any], *, assume_yes: bool
) -> tuple[list[str], list[str]]:
    """Run the container upgrade. Returns (actions, warnings)."""
    from agentalloy.install.subcommands import container_runtime as cr

    actions: list[str] = []
    warnings: list[str] = []

    # Keep the orchestrating CLI in lock-step with the image so the recreate
    # uses the new entrypoint (with stamp-compare re-seed). Best-effort.
    if _detect_install_method() != "source":
        try:
            subprocess.run(_swap_command(_detect_install_method(), ref), check=True, timeout=1800)
            actions.append(f"upgraded CLI to {ref}")
        except (subprocess.SubprocessError, OSError) as exc:
            warnings.append(f"CLI upgrade failed ({exc}); continuing with image pull")

    runtime = state.get("runtime_binary") or cr._detect_runtime_binary()
    if not runtime:
        warnings.append("no container runtime (podman/docker) found on PATH")
        return actions, warnings
    image = _target_image(state.get("image_tag"), ref)

    if cr._pull_image(runtime, image) != 0:
        warnings.append(f"failed to pull {image}")
        return actions, warnings
    actions.append(f"pulled {image}")

    packs = _installed_packs(state)
    entrypoint = cr._generate_entrypoint(packs)
    if cr._run_container(runtime, entrypoint, packs, image_ref=image) != 0:
        warnings.append("container recreate failed")
        return actions, warnings
    actions.append("recreated container")
    actions.append("corpus self-heals on the new entrypoint (stamp-compare re-seed)")
    return actions, warnings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def upgrade(
    *,
    ref: str | None = None,
    check: bool = False,
    force: bool = False,
    assume_yes: bool = False,
) -> dict[str, Any]:
    """Resolve the latest release and apply it for the recorded deployment."""
    t0 = time.monotonic()
    current = _current_version()
    latest = _latest_release_tag()

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "current_version": current,
        "latest_release": latest,
        "target_ref": None,
        "deployment": None,
        "update_available": False,
        "actions": [],
        "warnings": [],
    }

    if ref is None and latest is None:
        summary["warnings"].append(
            "Could not reach the GitHub releases API to resolve the latest version. "
            "Retry later, or pass `--ref vX.Y.Z` to target a specific release."
        )
        summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return summary

    target_ref = ref or (latest if isinstance(latest, str) else "")
    summary["target_ref"] = target_ref
    available = bool(latest) and _parse_semver(latest) > _parse_semver(current)  # type: ignore[arg-type]
    summary["update_available"] = available

    if check:
        summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return summary

    if ref is None and not available and not force:
        summary["actions"].append(f"already on the latest release ({current})")
        summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return summary

    state = install_state.load_state()
    deployment = state.get("deployment") or "native"
    summary["deployment"] = deployment

    if deployment == "container":
        actions, warnings = _upgrade_container(target_ref, state, assume_yes=assume_yes)
    else:
        actions, warnings = _upgrade_native(target_ref, state, assume_yes=assume_yes)

    summary["actions"].extend(actions)
    summary["warnings"].extend(warnings)
    # Read the post-swap version from the new binary; the in-process __version__ is
    # frozen at the pre-upgrade value (module imported before the swap).
    summary["new_version"] = _installed_version_via_cli() or _current_version()
    summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return summary


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "upgrade",
        help="Upgrade an existing install to the latest tagged release (native or container).",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Report current vs latest release and exit without changing anything.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive: skip confirmation and auto-approve a required re-embed.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-install even if already on the latest release.",
    )
    p.add_argument(
        "--ref",
        default=None,
        help="Target a specific release tag (e.g. v2.2.0) instead of the latest.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    print_rich("\n  [bold]Upgrade[/bold]\n")
    print_rich(f"  Current:  {result.get('current_version')}")
    print_rich(f"  Latest:   {result.get('latest_release') or 'unknown'}")
    if result.get("target_ref") and result.get("target_ref") != result.get("latest_release"):
        print_rich(f"  Target:   {result.get('target_ref')}")
    if result.get("deployment"):
        print_rich(f"  Mode:     {result.get('deployment')}")

    actions = result.get("actions") or []
    if actions:
        print_rich("\n  [bold]Actions[/bold]")
        for a in actions:
            print_rich(f"  [green]✓[/green] {a}")

    warnings = result.get("warnings") or []
    if warnings:
        print_rich("\n  [bold]Warnings[/bold]")
        for w in warnings:
            print_rich(f"  [yellow]![/yellow] {w}")

    if result.get("new_version"):
        print_rich(f"\n  Now on: [bold]{result.get('new_version')}[/bold]")
    elif result.get("update_available"):
        print_rich("\n  [dim]Run without --check to apply.[/dim]")
    print_rich()


def _run(args: argparse.Namespace) -> int:
    result = upgrade(
        ref=getattr(args, "ref", None),
        check=getattr(args, "check", False),
        force=getattr(args, "force", False),
        assume_yes=getattr(args, "yes", False),
    )
    write_result(result, args, human_fn=_render_human)
    # Non-zero only when an action was attempted and something went wrong.
    if result.get("warnings") and result.get("deployment") is not None:
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers."""
    return _run(args)
