# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Per-repo harness block for the code-index module.

A SECOND sentinel pair — independent of the main ``<!-- BEGIN agentalloy
install -->`` block — telling coding agents that this repo has a code index
and which commands/endpoints to use:

    <!-- BEGIN agentalloy code-index -->
    ...
    <!-- END agentalloy code-index -->

Written by ``wire``/``add``/``wrap`` only when the code-index module is enabled
AND the local service reports ``modules.code_index == "enabled"``. Also migrates the
OLD standalone codebase-indexer block (``<!-- BEGIN codebase-indexer -->``)
in place: replaced by the new block when the module is enabled, removed when
it is not. ``unwire`` / ``uninstall`` sweep both marker pairs.

Target-file resolution mirrors codebase-indexer's ``app/cli/wiring.py`` (which
itself mirrored agentalloy's wire style): tool-specific markers outrank the
shared CLAUDE.md, and CLAUDE.md is the default when nothing is detected.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agentalloy.code_index.slug import repo_slug
from agentalloy.install.sentinel_utils import remove_sentinel_block, replace_marked_block

SENTINEL_BEGIN = "<!-- BEGIN agentalloy code-index -->"
SENTINEL_END = "<!-- END agentalloy code-index -->"

# Markers written by the OLD standalone codebase-indexer CLI (app/cli/wiring.py).
LEGACY_SENTINEL_BEGIN = "<!-- BEGIN codebase-indexer -->"
LEGACY_SENTINEL_END = "<!-- END codebase-indexer -->"

# Every file (relative to the repo root) the block may live in — ours or the
# legacy tool's. Order is the detection priority for NEW writes; the sweep
# (legacy migration, unwire) always scans all of them. The `.mdc` files are
# dedicated (entirely ours / the legacy tool's) and are deleted when emptied.
_CANDIDATE_TARGETS: tuple[str, ...] = (
    ".cursor/rules/agentalloy-code-index.mdc",
    ".cursor/rules/codebase-indexer.mdc",  # legacy dedicated file
    "GEMINI.md",
    ".clinerules",
    "CLAUDE.md",
    "AGENTS.md",
    # Windsurf — dedicated file (when .windsurf/ exists), shared fallback
    ".windsurf/rules/agentalloy.md",
    ".windsurfrules",
    # GitHub Copilot
    ".github/copilot-instructions.md",
    # Aider
    ".agentalloy-aider-instructions.md",
    # OpenCode
    ".opencode/system-prompt.md",
)

_DEDICATED_TARGETS = frozenset(
    {".cursor/rules/agentalloy-code-index.mdc", ".cursor/rules/codebase-indexer.mdc"},
)

# Inner block template (without sentinels). Kept small — it loads into every
# agent session for this repo.
_BLOCK_TEMPLATE = """\
## agentalloy code-index — code intelligence for this repo

This repo has a code index (slug `{slug}`) served by the agentalloy service at
`http://127.0.0.1:{port}/code`. Prefer it over grep/file-reading to find code
by intent, trace call graphs, or assemble cross-file context:

- `agentalloy code search "<intent>" -k 10` — hybrid semantic search
- `agentalloy code callers <fqn>` (or `callees`) — call-graph tracing
- `agentalloy code bundle "<task>"` — budgeted multi-file context (includes governing design decisions)

Design decisions behind the code:

- `agentalloy knowledge why <fqn>` — design rationale governing a symbol
- `agentalloy knowledge related "<query>"` — find related decisions by topic

Re-run `agentalloy code index` after large changes. This block is managed by
agentalloy (`agentalloy unwire` removes it); edit outside the markers."""


def build_block(slug: str, port: int) -> str:
    """The inner markdown block (without sentinels) injected per repo."""
    return _BLOCK_TEMPLATE.format(slug=slug, port=port)


# Mapping of harness registry keys to their dedicated carrier file in the repo.
# Home-scoped harnesses (openclaw, continue-closed, continue-local) are
# intentionally omitted: their config lives in the user's home directory,
# not the repo root, so detect_target() returns None for them.
#
# Proxy-wired harnesses (claude-code, qwen-code, codex) are excluded: they
# receive context per-turn via the AgentAlloy proxy — no markdown block is
# written. hermes-agent is dual: proxy wiring + markdown injection into
# AGENTS.md (repo-scoped); .hermes/config.yaml is a YAML config file that
# must not receive prose.
_PROXY_ONLY_NO_MARKDOWN: frozenset[str] = frozenset(
    {"claude-code", "qwen-code", "codex"},
)

_HARNESS_CARRIERS: dict[str, str] = {
    "cursor": ".cursor/rules/agentalloy.mdc",
    "cline": ".clinerules",
    "windsurf": ".windsurf/rules/agentalloy.md",
    "github-copilot": ".github/copilot-instructions.md",
    "copilot-cli": ".github/copilot-instructions.md",
    "aider": ".agentalloy-aider-instructions.md",
    "opencode": ".opencode/system-prompt.md",
    "hermes-agent": "AGENTS.md",
}


def detect_target(root: Path, harness: str | None = None) -> Path | None:
    """The file the NEW block goes to — tool markers outrank shared CLAUDE.md.

    For harnesses with a known dedicated carrier file, returns that file if it
    exists in the repo root (the caller will create it if missing).  Falls back
    to shared targets (GEMINI.md, CLAUDE.md, etc.) when the dedicated carrier
    is absent.  Returns ``None`` for harnesses without a carrier (home-scoped
    tools like openclaw, continue-closed, continue-local).

    When no harness is specified, also checks for ``.cursor`` and ``.windsurf``
    directories (the original behaviour) to detect tool-specific carriers.
    """
    # 0a. Proxy-only harnesses receive context per-turn via the AgentAlloy
    #      proxy — they never get a code-index markdown block, even if a
    #      CLAUDE.md / AGENTS.md already exists in the repo.
    if harness in _PROXY_ONLY_NO_MARKDOWN:
        return None

    # 0. Harness-agnostic directory checks (when no harness is specified).
    #     .cursor/.cursorrules → dedicated .mdc file.
    #     .windsurf → dedicated .md file (not the shared fallback).
    if harness is None:
        if (root / ".cursor").is_dir() or (root / ".cursorrules").exists():
            return root / ".cursor/rules/agentalloy-code-index.mdc"
        if (root / ".windsurf").is_dir():
            return root / ".windsurf/rules/agentalloy.md"

    # 1. Dedicated carrier for this harness (repo-local config file)
    if harness and harness in _HARNESS_CARRIERS:
        dedicated = root / _HARNESS_CARRIERS[harness]
        if dedicated.exists():
            return dedicated

    # 2. Shared targets (in detection priority order)
    for rel in (
        "GEMINI.md",
        ".clinerules",
        ".windsurfrules",
        ".github/copilot-instructions.md",
        ".agentalloy-aider-instructions.md",
        ".opencode/system-prompt.md",
        "CLAUDE.md",
        "AGENTS.md",
    ):
        if (root / rel).exists():
            return root / rel

    # 3. Only default to CLAUDE.md when the harness is claude-code (or unknown).
    # For other harnesses, don't create a Claude Code carrier file.
    if harness is not None and harness != "claude-code":
        return None
    return root / "CLAUDE.md"


def service_base_url(port: int) -> str:
    """Base URL of the local service, honouring ``STATE_SERVICE_URL``.

    The code-index endpoints live on the *same* service and the *same* port as
    the state API, so they take the same override — one knob for "where is my
    agentalloy", not two.

    This is what keeps the test suite off a developer's live service. XDG
    redirection isolates every other write, but these calls leave the process:
    a wiring test would reach the real :47950, whose own environment resolves
    the real data dir, and the service would index the test's ``tmp_path`` into
    ``~/.local/share/agentalloy/code_index``. That is how 32 pytest temp dirs
    ended up in the registry (2026-07-28 cleanup); ``tests/conftest.py`` pins
    this env var to a dead port autouse.
    """
    return os.environ.get("STATE_SERVICE_URL") or f"http://127.0.0.1:{port}"


def service_module_status(port: int) -> str | None:
    """The running service's ``modules.code_index`` state, or None (unreachable)."""
    try:
        req = urllib.request.Request(f"{service_base_url(port)}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    modules = body.get("modules")
    if isinstance(modules, dict):
        state = modules.get("code_index")
        return state if isinstance(state, str) else None
    return None


def registry_slugs(port: int) -> list[str] | None:
    """Slugs in the service's indexed-repos registry, or None (unreachable)."""
    try:
        req = urllib.request.Request(f"{service_base_url(port)}/code/repos", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            raw = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    return [str(r["slug"]) for r in raw if isinstance(r, dict) and "slug" in r]


def active_job_for(slug: str, port: int) -> dict[str, Any] | None:
    """Newest queued/running index job for *slug*, or None (none/unreachable).

    The registry row lands only once the ingest pipeline starts, so a freshly
    submitted job is invisible to :func:`registry_slugs` for a moment. The job
    list is visible immediately — this closes that window so a second wiring
    run does not prompt for a repo whose index is already in flight.
    """
    url = (
        f"{service_base_url(port)}/code/index/jobs?slug={urllib.parse.quote(slug, safe='')}&limit=5"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            raw = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    for job in raw:  # newest first
        if isinstance(job, dict) and job.get("state") in ("queued", "running"):
            return {"id": job.get("id"), "slug": job.get("slug")}
    return None


def submit_index_job(port: int, repo_path: Path) -> dict[str, Any] | None:
    """POST /code/index for *repo_path*; the job snapshot, or None on failure.

    A 409 means an index job for this repo is *already active* — the endpoint
    rejects the duplicate but the running job is unaffected. That is not a
    failure, so the result carries ``already_active`` (plus the in-flight job's
    id, parsed from the 409 detail) rather than ``None``; the caller prints an
    "already active" pointer instead of the misleading "could not start".
    """
    payload = json.dumps({"repo_path": str(repo_path), "force": False}).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{service_base_url(port)}/code/index",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 409:
            job_id: str | None = None
            try:
                detail = json.loads(err.read()).get("detail", "")
            except (OSError, json.JSONDecodeError, AttributeError):
                detail = ""
            if isinstance(detail, str):
                # "an index job for slug 'x' is already active: <job_id>"
                job_id = detail.rsplit(":", 1)[-1].strip() or None
            return {"already_active": True, "job_id": job_id}
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def offer_index(root: Path, port: int, *, assume_yes: bool = False) -> dict[str, Any] | None:
    """Offer to index *root* when it isn't in the indexed-repos registry.

    Wire is an explicit enrollment act and the index is what makes the block
    useful, so the default answer is yes: ``assume_yes`` and non-TTY runs
    submit without prompting. Fire-and-forget — the job id is printed with an
    ``agentalloy code status`` pointer, never awaited. Best-effort: an
    unreachable service prints a hint and wiring proceeds untouched.

    If an index job for the repo is already active (submitted moments ago —
    the registry row lags the job list), no prompt: the user already opted in
    when that job was started, so the in-flight job is simply pointed at.
    """
    slug = repo_slug(root)
    slugs = registry_slugs(port)
    if slugs is None:
        print(
            "  code-index: service unreachable; index later with `agentalloy code index`",
            file=sys.stderr,
        )
        return None
    if slug in slugs:
        return None
    active = active_job_for(slug, port)
    if active is not None:
        print(
            f"  code-index: an index job is already active (id={active.get('id')}); "
            "follow it with `agentalloy code status`",
            file=sys.stderr,
        )
        return {"already_active": True, "job_id": active.get("id")}
    if not (assume_yes or not sys.stdin.isatty()):
        try:
            answer = input("Index this repo now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            answer = ""
        if answer not in ("", "y", "yes"):
            return None
    job = submit_index_job(port, root)
    if job is None:
        print(
            "  code-index: could not start the index job; run `agentalloy code index` manually",
            file=sys.stderr,
        )
        return None
    if job.get("already_active"):
        print(
            f"  code-index: an index job is already active (id={job.get('job_id')}); "
            "follow it with `agentalloy code status`",
            file=sys.stderr,
        )
        return job
    print(
        f"  code-index: index job started (id={job.get('id')}); "
        "follow it with `agentalloy code status`",
        file=sys.stderr,
    )
    return job


def _strip_block(path: Path, begin: str, end: str, *, rel: str) -> dict[str, Any] | None:
    """Remove one marker pair from *path*; delete a dedicated file left empty."""
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    if begin not in content or end not in content:
        return None
    cleaned = remove_sentinel_block(content, begin, end)
    if not cleaned.strip() and rel in _DEDICATED_TARGETS:
        path.unlink()
        return {"path": str(path), "action": "deleted_dedicated_file"}
    path.write_text(cleaned, encoding="utf-8")
    return {"path": str(path), "action": "removed_block"}


def remove_code_index_blocks(root: Path) -> list[dict[str, Any]]:
    """Sweep every candidate target, removing our block AND the legacy one.

    Idempotent and surgical: only the bytes between the markers are touched;
    files without markers are left alone. Used by unwire/uninstall and by a
    re-wire when the module is disabled.
    """
    actions: list[dict[str, Any]] = []
    for rel in _CANDIDATE_TARGETS:
        path = root / rel
        for begin, end in (
            (SENTINEL_BEGIN, SENTINEL_END),
            (LEGACY_SENTINEL_BEGIN, LEGACY_SENTINEL_END),
        ):
            rec = _strip_block(path, begin, end, rel=rel)
            if rec is not None:
                actions.append(rec)
            if rec is not None and rec["action"] == "deleted_dedicated_file":
                break  # file is gone; don't probe the second pair
    return actions


def wire_code_index_block(
    root: Path,
    port: int,
    *,
    harness: str | None = None,
) -> list[dict[str, Any]]:
    """Write/refresh the code-index block, migrating any legacy block in place.

    A legacy codebase-indexer block found in a candidate file is replaced by
    the new block at that location (the user chose that file once already);
    otherwise the new block goes to the detected target. Idempotent: an
    existing new block is updated between its markers.

    Args:
        root: Repository root.
        port: Proxy port (used for the block content).
        harness: Harness registry key (e.g. ``"qwen-code"``). Used for
            harness-aware target detection — prevents creating a CLAUDE.md
            for non-claude-code harnesses.

    """
    root = Path(root)
    slug = repo_slug(root)
    block = build_block(slug, port)
    actions: list[dict[str, Any]] = []

    # 1. Migrate the legacy block: remove it everywhere; remember where it was.
    legacy_home: Path | None = None
    for rel in _CANDIDATE_TARGETS:
        path = root / rel
        rec = _strip_block(path, LEGACY_SENTINEL_BEGIN, LEGACY_SENTINEL_END, rel=rel)
        if rec is None:
            continue
        if rec["action"] == "removed_block":
            rec["action"] = "replaced_legacy_codebase_indexer_block"
            if legacy_home is None and rel not in _DEDICATED_TARGETS:
                legacy_home = path
        actions.append(rec)

    # 2. Pick the target: an existing new block wins (update in place), then
    # the file the legacy block lived in, then detection.
    target: Path | None = None
    for rel in _CANDIDATE_TARGETS:
        path = root / rel
        if path.exists() and SENTINEL_BEGIN in path.read_text(encoding="utf-8"):
            target = path
            break
    if target is None:
        target = legacy_home if legacy_home is not None else detect_target(root, harness)

    # If no target was found (harness-aware detection returned None), skip
    # writing — the code-index block doesn't belong in a file we'd create.
    if target is None:
        return actions

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    updated = replace_marked_block(existing, block, SENTINEL_BEGIN, SENTINEL_END)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    action = (
        "updated_block"
        if SENTINEL_BEGIN in existing
        else ("injected_block" if existing else "wrote_new_file")
    )
    actions.append({"path": str(target), "action": action, "slug": slug})
    return actions


def maybe_wire(
    root: Path,
    port: int,
    *,
    quiet: bool = False,
    assume_yes: bool = False,
    harness: str | None = None,
) -> list[dict[str, Any]]:
    """Wire (or clean up) the code-index block based on live module state.

    - module ``enabled``  → write/refresh the block (migrating a legacy one),
      then offer to index the repo if it isn't in the registry yet (see
      :func:`offer_index`; ``assume_yes`` skips the TTY prompt).
    - anything else       → remove our block AND a legacy block if present,
      but only when one exists (a repo that never had one stays untouched).

    Args:
        root: Repository root.
        port: Proxy port.
        quiet: Suppress status output.
        assume_yes: Skip the TTY prompt for indexing.
        harness: Harness registry key for harness-aware target detection.

    Best-effort: wiring already succeeded when this runs, so failures are
    reported as warnings, never raised.

    The subcommand layer (``add``/``wire``/``wrap``) is the single owner of
    this step — provider install writers do not call it, so a wiring run
    prompts and submits at most once.
    """
    try:
        status = service_module_status(port)
        if status == "enabled":
            actions = wire_code_index_block(root, port, harness=harness)
        else:
            actions = remove_code_index_blocks(root)
        if not quiet:
            for a in actions:
                print(f"  code-index: {a['action']} {a['path']}", file=sys.stderr)
        if status == "enabled":
            offer_index(root, port, assume_yes=assume_yes)
        return actions
    except (OSError, ValueError) as exc:
        if not quiet:
            print(f"  code-index: wiring skipped ({exc})", file=sys.stderr)
        return []
