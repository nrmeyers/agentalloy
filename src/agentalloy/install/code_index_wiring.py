"""Per-repo harness block for the code-index module.

A SECOND sentinel pair — independent of the main ``<!-- BEGIN agentalloy
install -->`` block — telling coding agents that this repo has a code index
and which commands/endpoints to use:

    <!-- BEGIN agentalloy code-index -->
    ...
    <!-- END agentalloy code-index -->

Written by ``wire``/``add`` only when the code-index module is enabled AND the
local service reports ``modules.code_index == "enabled"``. Also migrates the
OLD standalone codebase-indexer block (``<!-- BEGIN codebase-indexer -->``)
in place: replaced by the new block when the module is enabled, removed when
it is not. ``unwire`` / ``uninstall`` sweep both marker pairs.

Target-file resolution mirrors codebase-indexer's ``app/cli/wiring.py`` (which
itself mirrored agentalloy's wire style): tool-specific markers outrank the
shared CLAUDE.md, and CLAUDE.md is the default when nothing is detected.
"""

from __future__ import annotations

import json
import sys
import urllib.error
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
)

_DEDICATED_TARGETS = frozenset(
    {".cursor/rules/agentalloy-code-index.mdc", ".cursor/rules/codebase-indexer.mdc"}
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
- `agentalloy code bundle "<task>"` — budgeted multi-file context

Re-run `agentalloy code index` after large changes. This block is managed by
agentalloy (`agentalloy unwire` removes it); edit outside the markers."""


def build_block(slug: str, port: int) -> str:
    """The inner markdown block (without sentinels) injected per repo."""
    return _BLOCK_TEMPLATE.format(slug=slug, port=port)


def detect_target(root: Path) -> Path:
    """The file the NEW block goes to — tool markers outrank shared CLAUDE.md."""
    if (root / ".cursor").is_dir() or (root / ".cursorrules").exists():
        return root / ".cursor/rules/agentalloy-code-index.mdc"
    for rel in ("GEMINI.md", ".clinerules", "CLAUDE.md", "AGENTS.md"):
        if (root / rel).exists():
            return root / rel
    return root / "CLAUDE.md"


def service_module_status(port: int) -> str | None:
    """The running service's ``modules.code_index`` state, or None (unreachable)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    modules = body.get("modules") if isinstance(body, dict) else None
    if isinstance(modules, dict):
        state = modules.get("code_index")
        return state if isinstance(state, str) else None
    return None


def registry_slugs(port: int) -> list[str] | None:
    """Slugs in the service's indexed-repos registry, or None (unreachable)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/code/repos", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, list):
        return None
    return [str(r["slug"]) for r in body if isinstance(r, dict) and "slug" in r]


def submit_index_job(port: int, repo_path: Path) -> dict[str, Any] | None:
    """POST /code/index for *repo_path*; the job snapshot, or None on failure."""
    payload = json.dumps({"repo_path": str(repo_path), "force": False}).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/code/index",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def offer_index(root: Path, port: int, *, assume_yes: bool = False) -> dict[str, Any] | None:
    """Offer to index *root* when it isn't in the indexed-repos registry.

    Wire is an explicit enrollment act and the index is what makes the block
    useful, so the default answer is yes: ``assume_yes`` and non-TTY runs
    submit without prompting. Fire-and-forget — the job id is printed with an
    ``agentalloy code status`` pointer, never awaited. Best-effort: an
    unreachable service prints a hint and wiring proceeds untouched.
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


def wire_code_index_block(root: Path, port: int) -> list[dict[str, Any]]:
    """Write/refresh the code-index block, migrating any legacy block in place.

    A legacy codebase-indexer block found in a candidate file is replaced by
    the new block at that location (the user chose that file once already);
    otherwise the new block goes to the detected target. Idempotent: an
    existing new block is updated between its markers.
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
        target = legacy_home if legacy_home is not None else detect_target(root)

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
    root: Path, port: int, *, quiet: bool = False, assume_yes: bool = False
) -> list[dict[str, Any]]:
    """Wire (or clean up) the code-index block based on live module state.

    - module ``enabled``  → write/refresh the block (migrating a legacy one),
      then offer to index the repo if it isn't in the registry yet (see
      :func:`offer_index`; ``assume_yes`` skips the TTY prompt).
    - anything else       → remove our block AND a legacy block if present,
      but only when one exists (a repo that never had one stays untouched).

    Best-effort: wiring already succeeded when this runs, so failures are
    reported as warnings, never raised.
    """
    try:
        status = service_module_status(port)
        if status == "enabled":
            actions = wire_code_index_block(root, port)
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
