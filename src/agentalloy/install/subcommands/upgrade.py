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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agentalloy.install import release_check, runtime_artifacts
from agentalloy.install import state as install_state
from agentalloy.install.output import (
    add_json_flag,
    print_rich,
    print_rich_stderr,
    progress_activity,
    should_output_human,
    write_result,
)

# All GitHub-API + version-parsing code lives in release_check (the single
# module that owns the service's only outbound call). Alias to the existing
# private names so the call sites below — and the tests that monkeypatch them —
# are unchanged.
from agentalloy.install.release_check import current_version as _current_version
from agentalloy.install.release_check import fetch_latest_tag as _latest_release_tag
from agentalloy.install.release_check import parse_semver as _parse_semver

SCHEMA_VERSION = 1
STEP_NAME = "upgrade"

_GIT_URL = "https://github.com/nrmeyers/agentalloy.git"
# Substrings that mark an embedding-dimension mismatch surfaced by install-packs
# or the startup guard — the signal that a full re-embed is required.
_DIM_MISMATCH_MARKERS = ("embedding_dim", "EmbeddingDimMismatch", "-dim embeddings", "dimension")
# Release-notes lines shown in the preflight card before linking out for the rest.
_NOTES_PREVIEW_LINES = 12


# ---------------------------------------------------------------------------
# Version helpers (defined in release_check; imported above)
# ---------------------------------------------------------------------------


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
    """Build the package re-install command for ``method`` at git ``ref``.

    Prefers ``uv tool install`` whenever ``uv`` is on PATH — this is safe even
    for plain-pip installs and avoids `_detect_install_method`'s uv-tool-list
    probe (which can misclassify a uv-tool install as "pip" on any hiccup,
    then shell `python -m pip` into a venv that ships no pip). Falls back to
    pip only when ``uv`` truly isn't available.
    """
    target = f"git+{_GIT_URL}@{ref}"
    if method != "source" and shutil.which("uv"):
        return ["uv", "tool", "install", "--force", "--from", target, "agentalloy"]
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
    """Stop the main API service before swapping the package. Returns the mode.

    ``uv tool install --force`` swaps the package but does not stop the running
    stack, so orphaned llama-servers keep running off the replaced/deleted files.
    Reap our own stale processes (best-effort, never raises, foreign processes
    untouched) before the swap; units and the shim are left in place.
    """
    if _is_systemd():
        _systemctl("stop", "agentalloy.service")
        mode = "systemd"
    else:
        _run_cli(["server-stop"], check=False)
        mode = "manual"
    runtime_artifacts.reap("processes")
    return mode


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
    """Comma-joined pack names to re-install, from the install-state registry.

    Entries are dicts (``{name, version, content_hash}``) and the registry keeps
    one per install, so the same name can appear across versions — extract the
    ``name`` and de-dupe, order-preserving. (A bare string entry is tolerated for
    forward/backward compatibility.) Falls back to ``"all"`` when the registry is
    empty or unusable so an upgrade never silently installs nothing.
    """
    names: list[str] = []
    seen: set[str] = set()
    for p in state.get("installed_packs") or []:
        name = p if isinstance(p, str) else (p.get("name") if isinstance(p, dict) else None)
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)
    return ",".join(names) if names else "all"


def _reset_pack_registry() -> bool:
    """Clear ``installed_packs`` in install-state so the version gate re-ingests.

    Used on an engine migration where the on-disk pack registry (written by the
    old version) no longer reflects the empty new skill store; without this,
    install-packs' content-hash gate skips every pack. install-packs rewrites the
    registry as it re-ingests. Returns True iff a non-empty registry was cleared.
    """
    try:
        st = install_state.load_state()
    except Exception:  # noqa: BLE001 — best-effort; a read failure just means no reset
        return False
    if not st.get("installed_packs"):
        return False
    st["installed_packs"] = []
    try:
        install_state.save_state(st)
    except OSError:
        return False
    return True


# v4 engine artifacts left beside the v5 corpus (Kuzu graph + DuckDB skill/vector
# store). Removed only after the v5 corpus is confirmed populated.
_LEGACY_CORPUS_FILES = ("skills.duck", "skills.duck.wal", "ladybug", "ladybug.wal")


def _drop_legacy_corpus_files() -> list[str]:
    """Delete v4 corpus artifacts (``ladybug``, ``skills.duck``) beside the v5
    corpus after a successful engine migration. Returns the names removed.

    Best-effort: a file that can't be removed is skipped, never fatal — a stale
    v4 file is unused dead weight, not a correctness problem. Handles both file
    and directory forms (``ladybug`` has shipped as either).
    """
    from agentalloy.config import get_settings

    try:
        corpus_dir = Path(get_settings().duckdb_path).parent
    except Exception:  # noqa: BLE001 — path resolution failure just means no cleanup
        return []
    removed: list[str] = []
    for name in _LEGACY_CORPUS_FILES:
        target = corpus_dir / name
        if not target.exists():
            continue
        try:
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def _upgrade_native(
    ref: str, state: dict[str, Any], *, assume_yes: bool, show_progress: bool = False
) -> tuple[list[str], list[str]]:
    """Run the native upgrade. Returns (actions, warnings).

    ``show_progress`` drives a live spinner around the long, output-silent
    steps (package swap, pack ingest, corpus migration); each runs with its
    own output captured, so without it the terminal sits blank for minutes and
    looks wedged. Defaults off for programmatic/`--json` callers.
    """
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
        with progress_activity(f"installing {ref} via {method}", enabled=show_progress):
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

    # Freshly imported (not at module top) so a native upgrade runs the NEW
    # post-swap code, not this still-loaded pre-swap module.
    from agentalloy.install.subcommands import seed_corpus

    # Engine-migration guard: a v4 install recorded its packs (with content
    # hashes) in install-state, but v5's skill store (agentalloy.duck) starts
    # empty. install-packs' version gate keys off that record, so it reports every
    # pack "already installed" and skips ingest — leaving the new engine empty.
    # When the skill store is empty yet the registry still claims packs, clear the
    # registry so ingest actually re-populates the new store (install-packs then
    # rewrites the registry as it goes).
    if seed_corpus.corpus_skill_count() == 0 and _reset_pack_registry():
        actions.append("cleared stale pack registry (engine migration) — forcing full re-ingest")

    packs = _installed_packs(state)
    with progress_activity("ingesting skill packs", enabled=show_progress):
        ingest = _run_cli(["install-packs", "--packs", packs, "--no-restart"], capture=True)

    dim_changed = _is_dim_mismatch(ingest)
    # A same-dim engine migration (v4 stored vectors in DuckDB, v5 in
    # fragments.lance) is invisible to the dim-mismatch check: install-packs
    # writes fragment METADATA to agentalloy.duck but the vector index is built
    # by reembed, so the Lance dataset lands empty and retrieval silently dies.
    lance_empty = seed_corpus.corpus_embedding_count() == 0

    if dim_changed or lance_empty:
        if dim_changed:
            warnings.append("embedding dimension changed — a full re-embed is required")
        else:
            actions.append("vector index empty (engine migration) — rebuilding embeddings")
        # A dim change is long and needs a human OK; an empty index is
        # non-negotiable (the service cannot retrieve without it), so don't gate
        # that case behind a prompt — always rebuild.
        proceed = lance_empty or _confirm(
            "  Re-embed the whole corpus now? This can take 30–40 min on CPU",
            assume_yes=assume_yes,
        )
        if proceed:
            # Stream reembed (capture=False): it prints its own live `embedded
            # N/M` counter to stderr, which beats a blind spinner. The trailing
            # re-ingest is the silent one, so wrap only that.
            _run_cli(["reembed", "--force", "--no-restart"], check=False)
            with progress_activity("re-ingesting skill packs", enabled=show_progress):
                _run_cli(["install-packs", "--packs", packs, "--no-restart"], check=False)
            actions.append("re-embedded corpus (--force)")
        else:
            warnings.append("re-embed skipped — the service may refuse to start until you run it")
    else:
        actions.append("re-ingested packs")

    # Re-validate user customizations against the freshly-installed shipped skills.
    # A version bump can change a skill's load-bearing mechanics (gates, contract
    # paths, phase-advance commands); an override whose prose no longer carries
    # them is disabled so the shipped version serves, surfaced as a warning. Runs
    # as a child so it sees the NEW _packs (this process is still pre-swap code).
    with progress_activity("re-validating customizations", enabled=show_progress):
        rev = _run_cli(["customize", "revalidate", "--json"], check=False, capture=True)
    actions.append("re-validated overrides")
    try:
        rev_payload: dict[str, Any] = json.loads(rev.stdout or "{}")
        rev_warnings = rev_payload.get("warnings")
        if isinstance(rev_warnings, list):
            warnings.extend(w for w in rev_warnings if isinstance(w, str))
    except (json.JSONDecodeError, ValueError):
        pass

    # Guard: ingest/re-embed reported done — verify the corpus actually populated.
    # install-packs only re-embeds on a dimension mismatch, so a silently empty
    # corpus would otherwise restart the service on a half-upgrade. A warning here
    # makes `_run` return a non-clean status (mirrors setup's #261 guard).
    skill_count = seed_corpus.corpus_skill_count()
    if skill_count < seed_corpus.MIN_SKILL_COUNT:
        warnings.append(
            f"corpus is missing or empty after upgrade ({skill_count} skills "
            f"embedded, expected >= {seed_corpus.MIN_SKILL_COUNT}) — run "
            f"`agentalloy reembed --force` then `agentalloy doctor`"
        )
    else:
        # New corpus is confirmed healthy — safe to reclaim the v4 engine files
        # (Kuzu ladybug + skills.duck). Only now, never on a half-populated corpus.
        dropped = _drop_legacy_corpus_files()
        if dropped:
            actions.append(f"removed legacy v4 corpus files ({', '.join(dropped)})")

    # corpus schema migrations + model-drift report. Capture the JSON output so it
    # does not spill to the terminal; surface only its warnings, cleanly.
    with progress_activity("running corpus migrations", enabled=show_progress):
        upd = _run_cli(["update", "--json"], check=False, capture=True)
    actions.append("ran corpus migrations")
    try:
        upd_payload: dict[str, Any] = json.loads(upd.stdout or "{}")
        upd_warnings = upd_payload.get("warnings")
        if isinstance(upd_warnings, list):
            warnings.extend(w for w in upd_warnings if isinstance(w, str))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    except (json.JSONDecodeError, ValueError):
        pass

    # Web UI bundle is version-matched to the server; fetch the new tag's
    # asset via the freshly-installed binary (its current_version() is the new
    # version). Non-fatal: the API works without it and / serves a hint.
    with progress_activity("downloading web UI bundle", enabled=show_progress):
        web = _run_cli(["pull-web"], check=False, capture=True)
    if web.returncode == 0:
        actions.append("refreshed web UI bundle")
    else:
        warnings.append("web UI bundle download failed — run `agentalloy pull-web` and restart")

    _start_service()
    actions.append("restarted service")
    return actions, warnings


# ---------------------------------------------------------------------------
# Container upgrade
# ---------------------------------------------------------------------------


def _target_image(current_tag: str | None, version: str | None) -> str:
    """Pin the image to the release ``version``, preserving registry + -full variant.

    When ``version`` is falsy (no ``--ref`` and the releases API was unreachable,
    or a recreate-only pass with nothing to pin), fall back to the base ref's own
    tag rather than building ``repo:`` — an invalid image that aborts the run.
    """
    from agentalloy.install.subcommands.container_runtime import _DEFAULT_IMAGE

    base_ref = current_tag or _DEFAULT_IMAGE
    if not version:
        return base_ref
    repo = base_ref.rsplit(":", 1)[0]
    suffix = "-full" if base_ref.endswith("-full") else ""
    return f"{repo}:{version.lstrip('v')}{suffix}"


def _upgrade_container(
    ref: str, state: dict[str, Any], *, assume_yes: bool
) -> tuple[list[str], list[str]]:
    """Run the container upgrade. Returns (actions, warnings).

    No activity spinner here: the long container steps already stream their
    own progress — ``podman pull`` renders per-layer download bars and the CLI
    swap runs uncaptured — so the terminal never goes silent the way the
    native path's captured steps do.
    """
    from agentalloy.install.subcommands import container_runtime as cr

    actions: list[str] = []
    warnings: list[str] = []

    runtime = state.get("runtime_binary") or cr._detect_runtime_binary()
    if not runtime:
        warnings.append("no container runtime (podman/docker) found on PATH")
        return actions, warnings
    image = _target_image(state.get("image_tag"), ref)

    # Pull the image FIRST, before touching the CLI. A freshly-tagged release can
    # take a few minutes to publish its container image, so the release/tag can be
    # visible while the image is not. If we swapped the CLI first and the pull then
    # failed, we'd strand a newer CLI orchestrating the old container. Pulling first
    # means a not-yet-published image aborts with everything on the current version.
    if cr._pull_image(runtime, image) != 0:
        warnings.append(
            f"image {image} isn't available yet — a new release's container image can "
            "take a few minutes to publish after the tag. Nothing was changed (CLI and "
            "container are both unchanged); re-run `agentalloy upgrade` shortly."
        )
        return actions, warnings
    actions.append(f"pulled {image}")

    # Image is present — now keep the orchestrating CLI in lock-step with it so the
    # recreate uses the new entrypoint (with stamp-compare re-seed). Best-effort.
    method = _detect_install_method()
    swapped = False
    if method != "source":
        try:
            subprocess.run(_swap_command(method, ref), check=True, timeout=1800)
            actions.append(f"upgraded CLI to {ref}")
            swapped = True
        except (subprocess.SubprocessError, OSError) as exc:
            warnings.append(f"CLI upgrade failed ({exc}); continuing with image recreate")

    # Recreate under the *new* code, never this stale in-process module. The
    # container spec (mounts, env, entrypoint) is baked at `podman run` time, so a
    # recreate run by the pre-swap module bakes the OLD spec — the exact failure
    # that shipped a 3.0.5 CLI orchestrating a mountless container. After a
    # successful swap, shell the freshly-installed binary to do the recreate
    # (mirroring how every other post-swap step already runs the new code). For a
    # source checkout nothing was swapped, so this process *is* the new code.
    if swapped:
        rec = _run_cli(["upgrade", "--recreate-only", "--ref", ref], check=False, capture=True)
        actions.append("recreated container (post-swap CLI)")
        if rec.returncode != 0:
            # Surface the child's own warning lines (recreate failure *or* a spec
            # post-condition warning) rather than a generic blanket message.
            detail = (rec.stdout or rec.stderr or "").strip().splitlines()
            warnings.append(
                "post-swap recreate reported issues: "
                + (detail[-1] if detail else "re-run `agentalloy upgrade` and check the container")
            )
        else:
            actions.append("corpus self-heals on the new entrypoint (stamp-compare re-seed)")
        # Re-validate host-mounted customizations under the new CLI (same child
        # call as the native path). Only after a successful swap, so it runs the
        # new _packs; on the not-swapped fallback we'd re-derive against stale code.
        rev = _run_cli(["customize", "revalidate", "--json"], check=False, capture=True)
        actions.append("re-validated overrides")
        try:
            rev_payload: dict[str, Any] = json.loads(rev.stdout or "{}")
            rev_warnings = rev_payload.get("warnings")
            if isinstance(rev_warnings, list):
                warnings.extend(w for w in rev_warnings if isinstance(w, str))
        except (json.JSONDecodeError, ValueError):
            pass
    else:
        a, w = _recreate_container(image, state)
        actions.extend(a)
        warnings.extend(w)
    return actions, warnings


def _recreate_container(image: str | None, state: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Recreate the container with *this* code's spec, then verify it took.

    Skips image pull and CLI swap — it is the recreate half only, invoked either
    in-process (source checkout) or via ``upgrade --recreate-only`` as a post-swap
    CLI step so the spec is always baked by the running code, never a stale module.
    """
    from agentalloy.install.subcommands import container_runtime as cr

    actions: list[str] = []
    warnings: list[str] = []

    runtime = state.get("runtime_binary") or cr._detect_runtime_binary()
    if not runtime:
        warnings.append("no container runtime (podman/docker) found on PATH")
        return actions, warnings
    image = image or _target_image(state.get("image_tag"), None)

    packs = _installed_packs(state)
    port = install_state.validate_port(state.get("port", 47950))
    # Runs the image's baked /app/entrypoint.sh with AGENTALLOY_PACKS — no
    # host-generated entrypoint bind-mount. This both makes the container
    # survive `start`/reboot and fixes the prior temp-file leak: the old path
    # called _generate_entrypoint (a NamedTemporaryFile) but never cleaned it up.
    if cr._run_container(runtime, packs, image_ref=image, port=port) != 0:
        warnings.append("container recreate failed")
        return actions, warnings
    actions.append("recreated container")
    warnings.extend(_verify_container_spec(runtime, cr))
    # Record the image we actually ran. doctor's container check and the next
    # upgrade's _target_image() base both read state["image_tag"]; leaving it at
    # ":latest" after pinning to a versioned tag is misleading and loses the
    # -full variant across upgrades.
    if image and state.get("image_tag") != image:
        state["image_tag"] = image
        try:
            install_state.save_state(state)
            actions.append(f"pinned image_tag -> {image}")
        except OSError as exc:  # noqa: BLE001
            warnings.append(f"could not persist image_tag: {exc}")
    return actions, warnings


def _verify_container_spec(runtime: str, cr: Any) -> list[str]:
    """Post-condition: the live container must carry the spec this code intends.

    Catches the stale-spec class of bug — a recreate that silently kept an old
    mount, or a ``restart`` that reused the original create spec. A missing
    projects-root mount is the specific silent killer: the proxy then can't read
    ``.agentalloy/`` phase state and phase injection no-ops with no error.
    """
    root = str(cr.resolve_projects_root())
    if root == "/":
        return []  # the run path already refused to mount '/'; nothing to assert
    # Only warn on a *confirmed* missing mount. If inspect can't run (no runtime,
    # transient error, container absent) we can't confirm a problem, so stay quiet
    # rather than emit a false positive — the recreate's own exit code is the
    # primary success signal.
    try:
        out = subprocess.run(
            [
                runtime,
                "inspect",
                "agentalloy",
                "--format",
                "{{range .Mounts}}{{.Destination}} {{end}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            # cwd="/" — this runs right after the container recreate, the
            # exact window where rootless podman's bind-mount teardown can
            # transiently break getcwd() for callers under the bind-mounted
            # projects root (issue #303).
            cwd="/",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    if root not in (out.stdout or "").split():
        return [
            f"container is missing the projects-root mount ({root}) — the proxy "
            "cannot read .agentalloy/ phase state, so phase injection silently no-ops. "
            "A plain `podman restart` reuses the old spec; re-run `agentalloy upgrade`."
        ]
    return []


# ---------------------------------------------------------------------------
# Preflight (interactive)
# ---------------------------------------------------------------------------


def _customized_skill_count() -> int:
    """How many skills the user has overridden (profile or project layer).

    Best-effort and quiet: shells ``customize list --json`` (a bare list of rows
    each carrying a ``layer`` of project/profile/default) and counts the
    non-default rows. Any failure → 0, so the preflight never blocks on it.
    """
    try:
        proc = _run_cli(["customize", "list", "--json"], capture=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0:
        return 0
    try:
        rows: Any = json.loads(proc.stdout or "null")
    except (json.JSONDecodeError, ValueError):
        return 0
    if isinstance(rows, dict):  # tolerate a wrapped {"skills": [...]} shape
        rows = rows.get("skills")
    if not isinstance(rows, list):
        return 0
    return sum(
        1 for r in rows if isinstance(r, dict) and r.get("layer") not in (None, "", "default")
    )


def _preflight_confirm(current: str, target: str, ref: str | None, deployment: str) -> bool:
    """Show a release card on stderr and gate the swap on an interactive yes.

    Fetches the release detail fresh — the one place that wants the notes/URL,
    pulled at the moment of intent — and surfaces the v3.7.0 consequence: user
    customizations are re-validated on upgrade. Returns True to proceed, False
    to abort with no swap. Routed to stderr so ``--json`` stdout stays clean.
    """
    bump = release_check.bump_type(current, target)
    suffix = f"  ({bump})" if bump else ""
    target_disp = target.lstrip("v") or target
    print_rich_stderr(f"\n  [bold]Upgrade {current} → {target_disp}[/bold]{suffix}")
    print_rich_stderr(f"  [dim]deployment: {deployment}[/dim]")

    info = release_check.fetch_release_info(ref=ref)
    if info:
        if info["name"]:
            print_rich_stderr(f"  {info['name']}")
        if info["published_at"]:
            print_rich_stderr(f"  [dim]published {info['published_at'][:10]}[/dim]")
        if info["html_url"]:
            print_rich_stderr(f"  [dim]{info['html_url']}[/dim]")
        content = [ln for ln in info["body"].splitlines() if ln.strip()]
        shown = content[:_NOTES_PREVIEW_LINES]
        if shown:
            print_rich_stderr("")
            for ln in shown:
                print_rich_stderr(f"  │ {ln}")
            if len(content) > _NOTES_PREVIEW_LINES and info["html_url"]:
                print_rich_stderr(f"  [dim]… full notes: {info['html_url']}[/dim]")
    else:
        print_rich_stderr("  [dim]release notes unavailable (offline?)[/dim]")

    customized = _customized_skill_count()
    if customized:
        print_rich_stderr(
            f"\n  [yellow]![/yellow] {customized} customized skill(s) will be re-validated "
            "against the new release; stale overrides are disabled (preserved) with a warning."
        )
    print_rich_stderr("")
    return _confirm(f"  Proceed with upgrade to {target}?", assume_yes=False)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def upgrade(
    *,
    ref: str | None = None,
    check: bool = False,
    force: bool = False,
    assume_yes: bool = False,
    interactive: bool = False,
) -> dict[str, Any]:
    """Resolve the latest release and apply it for the recorded deployment.

    ``interactive`` defaults to False so programmatic callers never block on
    stdin or hit the network for release notes; the CLI (``_run``) opts in via
    ``should_output_human``. When interactive and not ``assume_yes``, a preflight
    card + confirm gate runs before any swap; declining returns with no changes.
    """
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

    # Interactive preflight: show what's changing + a confirm gate before any
    # swap. Skipped under --json/--quiet (interactive=False) and --yes
    # (assume_yes); short-circuits so the card only renders when it will prompt.
    declined = (
        interactive
        and not assume_yes
        and not _preflight_confirm(current, target_ref, ref, deployment)
    )
    if declined:
        summary["actions"].append("upgrade declined by user")
        summary["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return summary

    if deployment == "container":
        actions, warnings = _upgrade_container(target_ref, state, assume_yes=assume_yes)
    else:
        actions, warnings = _upgrade_native(
            target_ref, state, assume_yes=assume_yes, show_progress=interactive
        )

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
    p.add_argument(
        "--dismiss",
        action="store_true",
        help="Silence the new-release notice for the current latest until a newer one lands.",
    )
    p.add_argument(
        "--recreate-only",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: post-swap recreate run by the new CLI
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

    if result.get("dismissed_version") is not None:
        print_rich(
            f"  Dismissed: [bold]{result['dismissed_version']}[/bold] (until a newer release)"
        )
    elif result.get("new_version"):
        print_rich(f"\n  Now on: [bold]{result.get('new_version')}[/bold]")
    elif result.get("update_available"):
        print_rich("\n  [dim]Run without --check to apply.[/dim]")
    print_rich()


def _dismiss(args: argparse.Namespace) -> int:
    """Mark the latest known release as dismissed so the badge stops nagging."""
    latest = release_check.read_cache().get("latest_tag") or _latest_release_tag()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "current_version": _current_version(),
        "latest_release": latest,
        "dismissed_version": None,
        "actions": [],
        "warnings": [],
    }
    if isinstance(latest, str) and latest:
        release_check.dismiss(latest)
        result["dismissed_version"] = latest
        result["actions"].append(f"dismissed {latest}")
    else:
        result["warnings"].append("no known release to dismiss")
    write_result(result, args, human_fn=_render_human)
    return 0


def _run(args: argparse.Namespace) -> int:
    if getattr(args, "dismiss", False):
        return _dismiss(args)

    # Internal post-swap entry: recreate the container only (no pull, no CLI swap).
    # This is what `_upgrade_container` shells after the swap so the spec is baked
    # by the freshly-installed code rather than the stale orchestrating process.
    if getattr(args, "recreate_only", False):
        state = install_state.load_state()
        ref = getattr(args, "ref", None)
        image = _target_image(state.get("image_tag"), ref or _current_version())
        actions, warnings = _recreate_container(image, state)
        result = {
            "schema_version": SCHEMA_VERSION,
            "deployment": state.get("deployment") or "container",
            "actions": actions,
            "warnings": warnings,
        }
        write_result(result, args, human_fn=_render_human)
        return 1 if warnings else 0

    result = upgrade(
        ref=getattr(args, "ref", None),
        check=getattr(args, "check", False),
        force=getattr(args, "force", False),
        assume_yes=getattr(args, "yes", False),
        interactive=should_output_human(args),
    )
    write_result(result, args, human_fn=_render_human)
    # Non-zero only when an action was attempted and something went wrong.
    if result.get("warnings") and result.get("deployment") is not None:
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    """Public entry point for non-argparse callers."""
    return _run(args)
