# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Deterministic predicate evaluators for phase gate evaluation.

Predicates are pure functions: (args: dict, ctx: PredicateContext) -> PredicateResult.
They never raise; they return UNKNOWN on any IO or context failure.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agentalloy.lessons_artifact import LESSON_NAME, LESSON_PHASE

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PredicateResult(Enum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PredicateContext:
    project_root: Path
    current_phase: str | None = None
    recent_prompt_text: str | None = None
    recent_tool_use: dict[str, Any] | None = None  # {tool, path, args}
    file_events_since: list[Path] = field(default_factory=lambda: cast(list[Path], []))
    # Store handle for querying contracts by phase/slug (in-process callers).
    # When None, predicates fall back to filesystem resolution.
    store: Any = None  # DuckDBStateStore | None (avoid runtime import)
    # Session id for cursor scoping — so the lessons_recorded gate resolves the
    # SAME work-item the proxy composed for this session (Bug C). None → shared cursor.
    session_key: str | None = None
    # mutable cache for git state (use dict so we can mutate from frozen dataclass)
    _git_cache: dict[str, str | None] = field(
        default_factory=lambda: cast(dict[str, str | None], {})
    )
    # Mutable diagnostics sink (same frozen-dataclass-safe pattern as _git_cache).
    # Semantic predicates record an embed-call failure here so the proxy can
    # surface a silently-degraded phase gate in telemetry: an UNKNOWN caused by
    # the embed server erroring is otherwise indistinguishable from an UNKNOWN
    # caused by "nothing to classify". See record_embed_failure / embed_failed.
    _diagnostics: dict[str, bool] = field(default_factory=lambda: cast(dict[str, bool], {}))

    # Per-phase cache of the resolved active work-item slug, so a whole gate
    # evaluation resolves it exactly once (#518). Same frozen-dataclass-safe
    # mutability trick as _git_cache / _diagnostics.
    _active_slug_cache: dict[str, str | None] = field(
        default_factory=lambda: cast(dict[str, str | None], {})
    )

    def record_embed_failure(self) -> None:
        """Flag that a semantic predicate's embed call failed this evaluation.

        The predicate still returns UNKNOWN (the gate fails open), but UNKNOWN
        alone can't tell an infra failure from "no text to score". This makes the
        embed failure observable so a silently-not-fired phase transition is
        queryable in telemetry instead of inferred from logs.
        """
        self._diagnostics["embed_failed"] = True

    @property
    def embed_failed(self) -> bool:
        """True if any semantic predicate hit an embed failure this evaluation."""
        return bool(self._diagnostics.get("embed_failed"))

    def resolve_active_slug(self, phase: str | None = None) -> str | None:
        """The active work-item slug for ``phase`` (default ``current_phase``).

        THE single resolution point (#518). Any predicate that must scope a
        store query to the active work item calls this — or, better, relies on
        the store-query helpers that default-scope to it — instead of
        hand-rolling slug resolution, so the rule "store queries are
        slug-scoped unless explicitly marked" has exactly one place to change.

        Store-first and cursor-aware (#514): queries the store for ``phase``'s
        active contracts, reads the work-item cursor (from the store when a
        store handle is bound, falling back to the pre-migration
        ``.agentalloy/cursor`` file), and returns the cursor'd contract's slug,
        else the sole contract in the phase, else ``None``.
        """
        phase = phase or self.current_phase
        if phase is None:
            return None
        cache = self._active_slug_cache
        if phase in cache:
            return cache[phase]
        slug = _resolve_workitem_slug(self, phase)
        cache[phase] = slug
        return slug


# Sentinel marking "scope to the active work item (the default)", so an
# explicit ``slug=None`` can still mean repo-global for the few predicates that
# genuinely evaluate cross-work-item invariants (#518: the default must be
# per-item; opt-out must be deliberate).
class _ScopeSentinel:
    __slots__ = ()


_ACTIVE = _ScopeSentinel()


def _glob_files(root: Path, pattern: str) -> list[Path]:
    r"""Return files matching glob pattern under root (or absolute if pattern is absolute).

    A bare trailing ``/**`` (e.g. ``"src/**"``) is meant to read "all files
    under ``src/``" — but pathlib's ``src/**`` matches the ``src`` *directory*
    itself (and subdirs), never the files within it, so an empty ``src/`` would
    vacuously satisfy an ``artifact_exists`` gate (#513). Normalize ``X/**`` to
    ``X/**/*`` and keep only real files, so the gate proves a file exists, not
    that the folder does.
    """
    try:
        if Path(pattern).is_absolute():
            p = Path(pattern)
            if p.is_file():
                return [p]
            return []
        glob_pattern = f"{pattern}/*" if pattern.endswith("/**") else pattern
        return [f for f in root.glob(glob_pattern) if f.is_file()]
    except Exception:
        return []


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _derive_phase_from_glob(glob_pattern: str) -> str | None:
    """Derive the phase name from a legacy contracts glob pattern.

    Patterns like ``.agentalloy/contracts/active/build/*.md`` yield ``"build"``.
    Returns ``None`` when the pattern doesn't match the expected shape.
    """
    parts = glob_pattern.split("/")
    # Expect: .../active/<phase>/*.md
    try:
        idx = parts.index("active")
        if idx + 1 < len(parts):
            phase_candidate = parts[idx + 1]
            # Strip glob chars from the phase component (shouldn't have any, but safe)
            if phase_candidate and not any(c in phase_candidate for c in "*?[]"):
                return phase_candidate
    except ValueError:
        pass
    return None


def _emit_legacy_glob_trace(glob_pattern: str) -> None:
    """Emit a deprecation trace for a legacy ``contracts`` glob arg.

    The trace confirms no corpus is still emitting globs before the tolerance
    branch is removed next release. Includes the caller stack so the offending
    gate YAML or caller is identifiable.
    """
    logger.warning(
        "DEPRECATION: legacy 'contracts' glob arg '%s' used in predicate/gate evaluation. "
        "Migrate gate args to 'phase'/'slug' keys. Caller:\n%s",
        glob_pattern,
        traceback.format_stack(limit=6)[-2].strip(),
    )


def _query_store_contracts(
    ctx: PredicateContext,
    *,
    phase: str | None = None,
    slug: str | None = None,
    work_item: str | None = None,
) -> list[dict[str, Any]] | None:
    """Query the store for contracts.

    Each dict carries ``slug``, ``domain_tags``, ``work_item``, ``phase``, etc. —
    the shape returned by ``DuckDBStateStore.list_contracts``.

    Three outcomes, deliberately distinguishable — there is **no** filesystem
    fallback (an earlier docstring claimed one):

    * ``[]``      — no store configured, or the store genuinely holds no match.
    * ``None``    — the store *errored*. Callers must fail open (UNKNOWN) rather
      than read an infra failure as "no contracts": ``eval_contract_exists``
      turns an empty result into NOT_MET, which would refuse a phase advance
      because the service blipped.
    * ``list``    — the matching rows.

    ``work_item`` is an explicit, opt-in filter (default ``None`` = repo-global
    within ``phase``). Contract work-item scoping is relationship-specific — a
    build contract's ``work_item`` is its *parent* design/plan slug, not its own
    slug — so ``_query_store_contracts`` does not auto-resolve it the way the
    artifact helper does; the cursor-based attribution lives in
    :func:`_item_build_contracts`. Artifact queries, by contrast, are
    default-scoped (see :func:`_list_store_artifacts`).
    """
    if ctx.store is None:
        return []
    try:
        return ctx.store.list_contracts(
            phase=phase, slug=slug, work_item=work_item, status="active"
        )
    except Exception:
        return None


def _resolve_workitem_slug_for(store: Any, project_root: Path, phase: str) -> str | None:
    """Store-side active work-item slug resolver — the one shared by the gate
    (via :meth:`PredicateContext.resolve_active_slug`) and by ``run_approve``, so
    the approval digest the CLI records and the digest the gate recomputes always
    cover the *same* artifact set (#501/#518).

    Store-first (#514): the cursor is read from the store when ``store`` is a
    handle that exposes ``read``, falling back to the pre-migration
    ``.agentalloy/cursor`` file; then the sole active contract in ``phase``;
    else ``None``.
    """
    in_phase: list[dict[str, Any]] = []
    if store is not None and hasattr(store, "list_contracts"):
        try:
            in_phase = store.list_contracts(phase=phase, status="active") or []
        except Exception:
            in_phase = []

    raw = ""
    if store is not None and hasattr(store, "read"):
        try:
            cursor_value = store.read("cursor")
            if cursor_value:
                raw = str(cursor_value).strip()
        except Exception:  # noqa: BLE001 — fall through to the legacy file
            pass
    if not raw:
        try:
            raw = (project_root / ".agentalloy" / "cursor").read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""

    if raw:
        cursor_slug = Path(raw).stem
        for c in in_phase:
            if c.get("slug") == cursor_slug or c.get("contract_id") == raw:
                return c.get("slug")

    if len(in_phase) == 1:
        return in_phase[0].get("slug")
    return None


def _list_store_artifacts(
    ctx: PredicateContext,
    *,
    phase: str,
    name_glob: str | None = None,
    slug: str | None | _ScopeSentinel = _ACTIVE,
) -> list[dict[str, Any]] | None:
    """Query the store for artifacts, scoped to the active work-item by default.

    ``None`` means "can't tell" — no store bound, or the bound store errored —
    callers must fail open (UNKNOWN), never treat it as "no artifacts" (NOT_MET
    would incorrectly block a gate whose predicates own the fail-open rule, not
    the caller; see ``test_no_store_handle_fails_open``). Only a store that is
    present AND answers (even with an empty list) yields a real result.

    Default scope (#518/#501): when ``slug`` is the ``_ACTIVE`` sentinel, resolves
    the active work-item slug for ``phase`` via
    :meth:`PredicateContext.resolve_active_slug` and filters to it — a prior
    work-item's artifacts can never affect this item's gate verdict. When no
    single work item resolves, it falls back to repo-global (``slug=None``), the
    pre-migration behavior; this is a documented boundary — the cursor is seeded
    on phase entry, so genuine multi-item ambiguity is rare, and failing open
    here would silently gut every store-backed completeness gate. Pass an
    explicit string to pin a specific slug, or ``None`` to deliberately evaluate
    repo-global.
    """
    if slug is _ACTIVE:
        slug = ctx.resolve_active_slug(phase)
    if ctx.store is None:
        return None
    try:
        return ctx.store.list_artifacts(phase, slug=slug, name_glob=name_glob)
    except Exception:
        return None


def eval_artifact_exists(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    phase = args.get("phase")
    if phase is not None:
        rows = _list_store_artifacts(ctx, phase=str(phase), name_glob=args.get("name"))
        if rows is None:
            return PredicateResult.UNKNOWN
        return PredicateResult.MET if rows else PredicateResult.NOT_MET
    pattern = args.get("path", "")
    if not pattern:
        return PredicateResult.UNKNOWN
    files = _glob_files(ctx.project_root, pattern)
    return PredicateResult.MET if files else PredicateResult.NOT_MET


def eval_artifact_absent(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    result = eval_artifact_exists(args, ctx)
    if result == PredicateResult.MET:
        return PredicateResult.NOT_MET
    if result == PredicateResult.NOT_MET:
        return PredicateResult.MET
    return PredicateResult.UNKNOWN


_TEST_EXCLUDE_DIRS = frozenset({"node_modules", "dist", ".venv", ".git", "__pycache__"})
_JS_TEST_EXTS = ("ts", "tsx", "js", "jsx", "mts", "cts")


def _path_in_excluded_dir(rel: Path) -> bool:
    """Whether any path segment is a vendored/output dir we never count tests from."""
    return any(part in _TEST_EXCLUDE_DIRS for part in rel.parts)


def eval_tests_present(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """Stack-aware test-presence gate: MET if any recognized test file exists.

    Replaces a hardcoded ``tests/**/*.py`` glob so a JS/TS repo with Vitest/Jest tests
    satisfies ``build -> qa`` without ``--force``. Detection:

    - always: ``tests/**/*.py``, ``**/test_*.py``, ``**/*_test.py`` (pytest)
    - when a root ``package.json`` exists: ``**/*.{test,spec}.{ts,tsx,js,jsx,mts,cts}``
    - ``args.extra_globs`` (list of repo-relative globs): a pack can add a stack (Go,
      Rust, ...) without a code change.

    Vendored/output dirs (``node_modules``, ``dist``, ``.venv``, ...) are excluded so their
    bundled tests never satisfy the gate. Returns MET/NOT_MET; never raises.
    """
    root = ctx.project_root
    patterns: list[str] = ["tests/**/*.py", "**/test_*.py", "**/*_test.py"]
    if (root / "package.json").is_file():
        for ext in _JS_TEST_EXTS:
            patterns.append(f"**/*.test.{ext}")
            patterns.append(f"**/*.spec.{ext}")
    extra = args.get("extra_globs")
    if isinstance(extra, list):
        patterns.extend(str(g) for g in cast(list[Any], extra))

    for pattern in patterns:
        for f in _glob_files(root, pattern):
            try:
                rel = f.relative_to(root)
            except ValueError:
                rel = f
            if not _path_in_excluded_dir(rel):
                return PredicateResult.MET
    return PredicateResult.NOT_MET


def _section_present(section: str, headings: list[str]) -> bool:
    """Whether a required ``section`` is present among markdown ``headings``,
    tolerating a trailing qualifier on the heading.

    A required section matches a heading when they are equal, or when the heading
    begins with the section name followed by a word boundary (a non-alphanumeric
    char). So ``## Out of Scope (this phase)``, ``## Tasks (8)``,
    ``## Review — notes`` and ``## Acceptance Criteria:`` all satisfy their bare
    section names, while ``Reviewer`` does not satisfy ``Review`` nor
    ``Subtasks`` satisfy ``Tasks``. Matching is case-insensitive.

    This keeps SDD phase gates from blocking an otherwise-complete exit artifact
    over a cosmetic heading suffix (a real footgun: authors naturally write
    ``## Out of Scope (this phase)``).
    """
    want = section.strip().casefold()
    if not want:
        return False
    for h in headings:
        hf = h.casefold()
        if hf == want or (hf.startswith(want) and not hf[len(want)].isalnum()):
            return True
    return False


def eval_artifact_contains(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """Check whether artifact files contain specified sections or regex patterns.

    Semantics: ALL files matching the pattern must pass ALL checks.
    - ``sections``: every listed section heading must appear in every file. A
      heading satisfies a section name even with a trailing qualifier
      (case-insensitive, word-boundary — see ``_section_present``).
    - ``pattern``: the regex must match in every file.
    Returns NOT_MET if any file fails any check, MET if all files pass all checks,
    UNKNOWN on IO failure (or on a store error, for the store-backed SDD path).
    """
    phase = args.get("phase")
    sections = args.get("sections")
    regex_pattern = args.get("pattern")

    if phase is not None:
        rows = _list_store_artifacts(ctx, phase=str(phase), name_glob=args.get("name"))
        if rows is None:
            return PredicateResult.UNKNOWN
        if not rows:
            return PredicateResult.NOT_MET
        for row in rows:
            content = row.get("content")
            if content is None:
                return PredicateResult.UNKNOWN
            if sections is not None:
                headings = _parse_markdown_headings(content)
                if not all(_section_present(s, headings) for s in sections):
                    return PredicateResult.NOT_MET
            if regex_pattern is not None:
                try:
                    if not re.search(regex_pattern, content, re.MULTILINE):
                        return PredicateResult.NOT_MET
                except re.error:
                    return PredicateResult.UNKNOWN
        return PredicateResult.MET

    pattern = args.get("path", "")
    if not pattern:
        return PredicateResult.UNKNOWN
    files = _glob_files(ctx.project_root, pattern)
    if not files:
        return PredicateResult.NOT_MET

    for f in files:
        content = _read_file(f)
        if content is None:
            return PredicateResult.UNKNOWN

        if sections is not None:
            # Parse markdown ATX headings (strip leading #'s and surrounding space).
            headings = _parse_markdown_headings(content)
            if not all(_section_present(s, headings) for s in sections):
                return PredicateResult.NOT_MET

        if regex_pattern is not None:
            try:
                if not re.search(regex_pattern, content, re.MULTILINE):
                    return PredicateResult.NOT_MET
            except re.error:
                return PredicateResult.UNKNOWN

    return PredicateResult.MET


def _parse_markdown_headings(content: str) -> list[str]:
    """Extract ATX markdown headings (leading ``#``s + surrounding space stripped).

    The same parse :func:`eval_artifact_contains` uses, factored out so the banner's
    section-completeness count scores against an identical view of the artifact.
    """
    return [line.lstrip("#").strip() for line in content.splitlines() if line.startswith("#")]


@lru_cache(maxsize=256)
def _section_completeness_cached(
    path_glob: str,
    sections_key: tuple[str, ...],
    project_root: Path,
    _mtime: float,
) -> tuple[int, int, tuple[str, ...]]:
    """Memoized core of :func:`section_completeness`, keyed including file mtime.

    ``_mtime`` is not read — it is the cache key that makes the entry self-invalidate
    when the artifact changes. ``evaluate_signal`` runs in-process, so this survives
    for the worker's lifetime and spares the re-parse on turns where the banner is
    built for a hash comparison and then suppressed (#587 §4).

    Nothing clears this on a phase transition, and it does not need to: a phase
    change alters ``path_glob``/``sections_key``, so the old phase's entries are
    simply never hit again and age out by LRU. ``project_root`` is in the key, so
    a worker serving several repos stays correct — they only compete for the 256
    slots. The one stale case is delete-then-recreate at a previously seen mtime,
    which requires a coarse-mtime filesystem or a checkout restoring old stamps.
    """
    present, total, missing = _section_completeness_uncached(
        path_glob, list(sections_key), project_root
    )
    return present, total, tuple(missing)


def _glob_first_mtime(project_root: Path, path_glob: str) -> float:
    """mtime of the first file matching ``path_glob``, or ``-1.0`` when absent.

    ``-1.0`` is a real cache key: it means "no artifact yet", and the entry is
    invalidated the moment one appears with a genuine mtime.
    """
    try:
        files = _glob_files(project_root, path_glob)
        if not files:
            return -1.0
        return files[0].stat().st_mtime
    except Exception:
        return -1.0


def section_completeness(
    path_glob: str,
    required_sections: list[str],
    project_root: Path,
) -> tuple[int, int, list[str]]:
    """How many ``required_sections`` are present in the artifact at ``path_glob``.

    Returns ``(present, total, missing)`` where ``total`` is ``len(required_sections)``,
    ``present`` is the count of required sections found as markdown headings in the
    FIRST file matching ``path_glob`` (relative to ``project_root``), and ``missing`` is
    the required sections not found, in declaration order. Section matching reuses
    :func:`_section_present` (case-insensitive, trailing-qualifier tolerant), the same
    rule the ``artifact_contains`` exit gate applies.

    Results are memoized on ``(glob, sections, root, artifact mtime)`` so repeated
    banner builds don't re-glob and re-parse an unchanged file; the mtime component
    invalidates the entry as soon as the artifact is edited.

    File I/O is fully wrapped: a missing glob match or an unreadable file yields
    ``(0, total, required_sections)`` — i.e. no progress, every section "missing" —
    so the banner never raises and a not-yet-created artifact simply shows 0 present.
    Never raises.
    """
    try:
        present, total, missing = _section_completeness_cached(
            path_glob,
            tuple(required_sections),
            project_root,
            _glob_first_mtime(project_root, path_glob),
        )
        return present, total, list(missing)
    except Exception:
        return _section_completeness_uncached(path_glob, required_sections, project_root)


def _section_completeness_uncached(
    path_glob: str,
    required_sections: list[str],
    project_root: Path,
) -> tuple[int, int, list[str]]:
    """The real scan. See :func:`section_completeness` for the contract."""
    total = len(required_sections)
    if total == 0:
        return 0, 0, []
    try:
        files = _glob_files(project_root, path_glob)
        if not files:
            return 0, total, list(required_sections)
        content = _read_file(files[0])
        if content is None:
            return 0, total, list(required_sections)
        headings = _parse_markdown_headings(content)
        present_count = 0
        missing: list[str] = []
        for section in required_sections:
            if _section_present(section, headings):
                present_count += 1
            else:
                missing.append(section)
        return present_count, total, missing
    except Exception:
        return 0, total, list(required_sections)


def store_section_completeness(
    store: Any,
    phase: str,
    name_glob: str,
    required_sections: list[str],
    slug: str | None = None,
) -> tuple[int, int, list[str]]:
    """``section_completeness`` for a STORE-backed artifact.

    Lifecycle artifacts (spec.artifact, approach.artifact, tasks.artifact, ...) live in the artifact
    store, not on disk, so the filesystem scorer above reports zero progress for
    them forever. This scores the same required headings against the recorded
    artifact bodies instead.

    Sections are scored against the UNION of headings across every row matching
    ``name_glob`` — plan's gate declares one section list covering ``tasks.md``
    *and* ``test-plan.md``, so scoring only the first row would permanently report
    the other file's sections missing.

    Returns ``(present, total, missing)``. Never raises: an unreachable store or a
    malformed row yields ``(0, total, required_sections)``, i.e. no progress, which
    the banner renders as "not yet recorded" rather than a false completion.
    """
    total = len(required_sections)
    if total == 0:
        return 0, 0, []
    if store is None:
        return 0, total, list(required_sections)
    try:
        rows = store.list_artifacts(phase, slug=slug, name_glob=name_glob)
    except Exception:
        return 0, total, list(required_sections)
    if not rows:
        return 0, total, list(required_sections)

    headings: list[str] = []
    for row in rows:
        body = row.get("content") if isinstance(row, dict) else None
        if isinstance(body, str):
            headings.extend(_parse_markdown_headings(body))
    if not headings:
        return 0, total, list(required_sections)

    present_count = 0
    missing: list[str] = []
    for section in required_sections:
        if _section_present(section, headings):
            present_count += 1
        else:
            missing.append(section)
    return present_count, total, missing


def eval_artifact_size_min(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    pattern = args.get("path", "")
    min_bytes = args.get("bytes", 0)
    if not pattern:
        return PredicateResult.UNKNOWN
    files = _glob_files(ctx.project_root, pattern)
    if not files:
        return PredicateResult.NOT_MET
    try:
        total = sum(f.stat().st_size for f in files if f.is_file())
        return PredicateResult.MET if total >= min_bytes else PredicateResult.NOT_MET
    except OSError:
        return PredicateResult.UNKNOWN


def eval_artifact_newer_than(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    pattern = args.get("path", "")
    since_pattern = args.get("since", "")
    if not pattern or not since_pattern:
        return PredicateResult.UNKNOWN
    files = _glob_files(ctx.project_root, pattern)
    markers = _glob_files(ctx.project_root, since_pattern)
    if not files or not markers:
        return PredicateResult.NOT_MET
    try:
        artifact_mtime = max(f.stat().st_mtime for f in files if f.is_file())
        marker_mtime = max(m.stat().st_mtime for m in markers if m.is_file())
        return PredicateResult.MET if artifact_mtime > marker_mtime else PredicateResult.NOT_MET
    except OSError:
        return PredicateResult.UNKNOWN


# --- approval gate -------------------------------------------------------

# Forward routes that always require a recorded human approval marker.
# add-skill is unconditional by design: installing a skill into the corpus
# changes what gets injected into every future session — never auto-approved.
_ALWAYS_APPROVAL_PHASES = ("spec", "design", "plan", "add-skill")


def approval_required(phase: str | None) -> bool:
    """True when leaving *phase* requires a recorded human approval.

    spec/design/add-skill: always. sdd-fast: behind SDD_FAST_REQUIRE_APPROVAL
    (default OFF). Everything else (intake, build, qa, ship): never.
    """
    if phase in _ALWAYS_APPROVAL_PHASES:
        return True
    if phase == "sdd-fast":
        try:
            from agentalloy.config import get_settings  # lazy, like gates.py

            return bool(get_settings().sdd_fast_require_approval)
        except Exception:
            return False
    return False


def approval_marker_path(project_root: Path, phase: str) -> Path:
    """Path of the human-approval marker for *phase* (``.agentalloy/approved/<phase>``)."""
    return project_root / ".agentalloy" / "approved" / phase


def _artifact_digest(rows: list[dict[str, Any]]) -> str:
    """Stable digest over artifact bodies, order-independent (sorted by name)."""
    import hashlib

    parts = [
        f"{r['name']}\0{r.get('content') or ''}" for r in sorted(rows, key=lambda r: r["name"])
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def eval_approval_recorded(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET when leaving the current phase is permitted by the human-approval gate.

    Store-backed, when the gate YAML declares ``since_name_glob`` (spec/design,
    post-migration): the marker is ``ctx.store.get_approval(phase)``, a JSON blob
    carrying the sha256 digest of the phase's artifact bodies at approval time.
    Staleness = the CURRENT digest (recomputed from ``since_name_glob``-matching
    artifacts) no longer matching the recorded one — the digest equivalent of the
    old marker-mtime-vs-artifact-mtime comparison, but immune to a
    touch-without-edit false staleness.

    The branch is keyed on the gate arg shape, NOT on ``ctx.store`` truthiness:
    phases that still declare the legacy ``since`` (a filesystem glob — sdd-fast,
    add-skill, pending their own migration) must keep working unchanged even
    when a store happens to be bound for this call.

    Disk-backed (gate YAML declares ``since``, a filesystem glob): the legacy
    ``.agentalloy/approved/<phase>`` marker file + mtime comparison against the
    exit-artifact glob, unchanged from before this migration.
    """
    phase = args.get("phase") or ctx.current_phase
    if phase is None:
        return PredicateResult.UNKNOWN
    if not approval_required(phase):
        return PredicateResult.MET  # route is not approval-gated → satisfied

    if "since_name_glob" in args:
        if ctx.store is None:
            return PredicateResult.UNKNOWN  # store required but unavailable → fail closed
        since_name_glob = args.get("since_name_glob")
        # Scope to the active work-item so a PRIOR item's artifact can neither
        # satisfy this item's approval nor poison its digest, and so the gate
        # and `run_approve` agree on the exact row set (#501/#518). Both fall
        # back to repo-global (slug=None) only when no single work item resolves
        # — a degenerate state where they still digest an identical set.
        slug = ctx.resolve_active_slug(str(phase))
        try:
            rows = ctx.store.list_artifacts(str(phase), slug=slug, name_glob=since_name_glob)
            approval = ctx.store.get_approval(str(phase))
        except Exception:
            return PredicateResult.UNKNOWN
        if approval is None:
            return PredicateResult.NOT_MET  # awaiting approval
        if not rows:
            return PredicateResult.NOT_MET  # nothing produced → nothing approvable
        current_digest = _artifact_digest(rows)
        recorded_digest = approval.get("artifact_digest")
        return PredicateResult.MET if current_digest == recorded_digest else PredicateResult.NOT_MET

    marker = approval_marker_path(ctx.project_root, str(phase))
    if not marker.is_file():
        return PredicateResult.NOT_MET  # awaiting approval
    since_pattern = args.get("since", "")
    if not since_pattern:
        return PredicateResult.MET  # existence-only marker
    artifacts = _glob_files(ctx.project_root, since_pattern)
    if not artifacts:
        return PredicateResult.NOT_MET  # nothing produced → nothing approvable
    try:
        marker_mtime = marker.stat().st_mtime
        artifact_mtime = max(f.stat().st_mtime for f in artifacts if f.is_file())
        # >= (not strict >) tolerates same-second granularity; staleness is only
        # when the exit artifact is edited *after* approval.
        return PredicateResult.MET if marker_mtime >= artifact_mtime else PredicateResult.NOT_MET
    except OSError:
        return PredicateResult.UNKNOWN


def eval_lessons_recorded(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET when the current task has recorded a compound-engineering lesson.

    Resolves the active work-item slug for the phase (``args['phase']`` or, by
    default, ``ctx.current_phase``) via the canonical
    :func:`agentalloy.contracts.resolve_current_contract` — cursor-first, then the
    sole contract for the phase, else no single work-item — and checks for
    ``docs/solutions/<slug>.md``.

    Slug-scoped on purpose. A bare ``artifact_exists: docs/solutions/*.md`` would
    be MET forever by the first lesson ever written (the stale-file no-op), so it
    could not force *this* task to codify. Tying the check to the active work-item
    slug makes it order-independent and per-task. Returns UNKNOWN (fail-open,
    never blocks) when no single work-item resolves. The cursor is seeded to the
    phase's first work-item on entry and advanced by ``task next``, so the gate
    normally resolves a concrete slug; only a genuinely uncursored fan-out (≥2
    contracts, no cursor — rare) fails open, by design, so the gate never blocks
    against a *guessed* task.

    Store-first, disk-fallback. The lesson is a store artifact
    (``phase='qa', name='solution'``, written by ``agentalloy contract
    artifact-set``); a repo predating the migration still satisfies the gate with
    ``docs/solutions/<slug>.md`` on disk, so no repo is stranded and the gate is
    never unsatisfiable just because no store is bound. The artifact is named
    ``solution`` WITHOUT a ``.md`` suffix on purpose: the qa exit gate globs
    ``name: "*.md"`` and ``artifact_contains`` requires EVERY matching row to
    carry ``## Checks``/``## Review``, so a ``.md``-suffixed lesson would make
    writing the lesson break the very gate it sits beside.
    """
    from agentalloy.contracts import (
        resolve_current_contract,  # lazy: keep signals free of import cost
    )

    phase = args.get("phase") or ctx.current_phase
    if phase is None:
        return PredicateResult.UNKNOWN
    _cid, contract_path = resolve_current_contract(ctx.project_root, str(phase), ctx.session_key)
    if contract_path is None:
        return PredicateResult.UNKNOWN
    slug = contract_path.stem
    rows = _list_store_artifacts(ctx, phase=LESSON_PHASE, name_glob=LESSON_NAME, slug=slug)
    if rows:
        return PredicateResult.MET
    lesson = ctx.project_root / "docs" / "solutions" / f"{slug}.md"
    return PredicateResult.MET if lesson.is_file() else PredicateResult.NOT_MET


def eval_phase_in(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    if ctx.current_phase is None:
        return PredicateResult.UNKNOWN
    phases = args.get("phases", [])
    return PredicateResult.MET if ctx.current_phase in phases else PredicateResult.NOT_MET


def eval_phase_not_in(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    result = eval_phase_in(args, ctx)
    if result == PredicateResult.MET:
        return PredicateResult.NOT_MET
    if result == PredicateResult.NOT_MET:
        return PredicateResult.MET
    return PredicateResult.UNKNOWN


def eval_tool_use_about_to_fire(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    if ctx.recent_tool_use is None:
        return PredicateResult.UNKNOWN
    tools = args.get("tools", [])
    tool_name = ctx.recent_tool_use.get("tool", "")
    return PredicateResult.MET if any(t in tool_name for t in tools) else PredicateResult.NOT_MET


def eval_tool_use_just_completed(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    return eval_tool_use_about_to_fire(args, ctx)


def _get_git_state(ctx: PredicateContext) -> str | None:
    """Run git status once and cache in ctx._git_cache."""
    cache = ctx._git_cache  # type: ignore[attr-defined]
    if "output" not in cache:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=ctx.project_root,
            )
            cache["output"] = result.stdout
        except Exception:
            cache["output"] = None
    return cache["output"]  # type: ignore[return-value]


def eval_git_state(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    output = _get_git_state(ctx)
    if output is None:
        return PredicateResult.UNKNOWN

    lines = output.splitlines()
    staged = any(line[:2][0] in "MADRCU" for line in lines if len(line) >= 2)
    uncommitted = any(line[:2][1] in "MADRCU?" for line in lines if len(line) >= 2)

    has_staged = args.get("has_staged")
    has_uncommitted = args.get("has_uncommitted")
    branch_pattern = args.get("branch_matches")

    if has_staged is not None and bool(has_staged) != staged:
        return PredicateResult.NOT_MET
    if has_uncommitted is not None and bool(has_uncommitted) != uncommitted:
        return PredicateResult.NOT_MET
    if branch_pattern is not None:
        try:
            br = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=ctx.project_root,
            )
            if not re.search(branch_pattern, br.stdout.strip()):
                return PredicateResult.NOT_MET
        except Exception:
            return PredicateResult.UNKNOWN

    return PredicateResult.MET


def _read_phase_start_ref(project_root: Path, store: Any | None = None) -> str | None:
    """The phase-entry HEAD SHA stamped on the last real phase transition.

    Absent (no marker yet, or git was unavailable at transition time) → None,
    which callers treat as UNKNOWN (fail-open) per the absent-vs-unavailable
    rule: a missing marker must not read as NOT_MET and block a legitimate
    advance. Written by ``skill_loader._record_phase_start_ref`` on both the
    CLI and proxy auto-advance transition paths.

    Reads from the store (``phase_start_ref`` blob field) when a ``store``
    handle is provided (tests pass their temp-store here); otherwise falls
    back to the store's global process handle.  The stamp lives in the phase
    blob, not on disk.
    """
    try:
        if store is not None:
            state = store.read_phase()
            return state.phase_start_ref if state else None
        # Fallback: global process handle (production path, not used in tests)
        from agentalloy.signals.skill_loader import (
            _phase_view,
        )

        view = _phase_view(project_root)
        if view is None:
            return None
        state = view.read_phase()
        return state.phase_start_ref if state else None
    except Exception:  # noqa: BLE001 — fail-soft by design
        return None


def _changed_paths_since(project_root: Path, base_ref: str) -> list[str] | None:
    """Repo-relative paths changed since *base_ref* (committed + working tree).

    Combines ``git diff --name-only <ref>..HEAD`` (committed during the phase)
    with ``git status --porcelain`` (staged/uncommitted/untracked) so a
    commit-per-concern workflow that leaves the tree clean at advance time is
    still caught. Returns ``None`` on any total git failure (callers fail open).
    A shallow clone where ``<ref>..HEAD`` is unreachable degrades to the
    working-tree signal alone rather than failing outright.
    """
    paths: set[str] = set()
    diff_ok = True
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}..HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
        diff_ok = diff.returncode == 0
        if diff_ok:
            paths.update(line.strip() for line in diff.stdout.splitlines() if line.strip())
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_root,
        )
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if len(line) > 3:
                    paths.add(line[3:].strip())
        if not diff_ok and st.returncode != 0:
            return None
    except Exception:
        return None
    return sorted(paths)


def _path_in_scope(path: str, scope_patterns: list[str]) -> bool:
    """Whether a repo-relative *path* is covered by any ``scope.touches`` pattern.

    Matches exact paths, directory prefixes (``src/auth`` covers
    ``src/auth/x.py``), and glob patterns (``src/**/*.py``). Pure; unit-tested.
    """
    p = path.strip()
    for s in scope_patterns:
        pat = s.strip().rstrip("/")
        if not pat:
            continue
        if p == pat or p.startswith(pat + "/"):
            return True
        if fnmatch.fnmatch(p, pat):
            return True
    return False


def eval_scope_touched_in_diff(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET iff the current phase changed a path inside the work-item's scope.touches.

    Replaces the vacuous ``artifact_exists{path: "src/**"}`` build→qa gate
    (#513): that node matched any repo with a ``src/`` dir, even empty. This
    diffs ``<phase-entry SHA>..HEAD`` (+ working tree) against the cursor'd
    contract's ``scope.touches``, so the gate proves the build actually touched
    the declared scope — not that the repo merely has source.

    Fail-open (UNKNOWN) on any infra gap — no phase-start marker, no store, no
    contract, an undeclared scope, or a git failure — never NOT_MET: a degraded
    read must not return the shape of success (the absent-vs-unavailable
    convention, #531/#530/#526).
    """
    phase = args.get("phase") or ctx.current_phase
    if phase is None:
        return PredicateResult.UNKNOWN

    base_ref = _read_phase_start_ref(ctx.project_root, store=ctx.store)
    if not base_ref:
        return PredicateResult.UNKNOWN

    contracts = _query_store_contracts(ctx, phase=str(phase))
    if not contracts:
        # None (store errored) or [] (no store / no contract) — can't tell either way.
        return PredicateResult.UNKNOWN
    slug = _resolve_workitem_slug(ctx, str(phase))
    chosen = next((c for c in contracts if c.get("slug") == slug), None) or contracts[0]
    touches = chosen.get("scope_touches") or []
    if not touches:
        return PredicateResult.UNKNOWN  # undeclared scope → don't block

    changed = _changed_paths_since(ctx.project_root, base_ref)
    if changed is None:
        return PredicateResult.UNKNOWN  # git failed → infra
    if not changed:
        return PredicateResult.NOT_MET  # nothing changed this phase — a no-op build
    if any(_path_in_scope(c, list(touches)) for c in changed):
        return PredicateResult.MET
    return PredicateResult.NOT_MET  # changed things, but nothing in the declared scope


def eval_contract_exists(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    count_min = args.get("count_min", 1)

    # Legacy glob tolerance: if 'contracts' key present, derive phase and trace
    if "contracts" in args:
        glob_pattern = str(args["contracts"])
        _emit_legacy_glob_trace(glob_pattern)
        phase = _derive_phase_from_glob(glob_pattern)
        if phase is None:
            return PredicateResult.UNKNOWN
    else:
        phase = args.get("phase", ctx.current_phase)

    if phase is None:
        return PredicateResult.UNKNOWN

    # Query store
    contracts = _query_store_contracts(ctx, phase=str(phase))
    if contracts is None:
        return PredicateResult.UNKNOWN  # store errored → fail open, not NOT_MET
    if not contracts and ctx.store is not None:
        # Store exists but returned nothing → no contracts for this phase
        return PredicateResult.NOT_MET
    return PredicateResult.MET if len(contracts) >= count_min else PredicateResult.NOT_MET


def eval_contract_has_tags(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """Check whether any contract in the phase has matching domain_tags.

    Semantics: ANY contract with ANY matching tag → MET.
    Returns NOT_MET if no contract has any of the specified tags, UNKNOWN on failure.
    """
    any_of_tags = args.get("any_of", [])

    # Legacy glob tolerance
    if "contracts" in args:
        glob_pattern = str(args["contracts"])
        _emit_legacy_glob_trace(glob_pattern)
        phase = _derive_phase_from_glob(glob_pattern)
        if phase is None:
            return PredicateResult.UNKNOWN
    else:
        phase = args.get("phase", ctx.current_phase)

    if phase is None:
        return PredicateResult.UNKNOWN

    # Query store
    contracts = _query_store_contracts(ctx, phase=str(phase))
    if contracts is None:
        return PredicateResult.UNKNOWN  # store errored → fail open, not NOT_MET
    if not contracts and ctx.store is not None:
        return PredicateResult.NOT_MET

    for contract in contracts:
        tags: list[Any] = contract.get("domain_tags") or []
        if any(t in tags for t in any_of_tags):
            return PredicateResult.MET

    return PredicateResult.NOT_MET


def eval_file_type_active(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    extensions = args.get("extensions", [])
    if not ctx.file_events_since and ctx.recent_tool_use is None:
        return PredicateResult.UNKNOWN
    # Check file_events_since
    for path in ctx.file_events_since:
        if any(str(path).endswith(ext) for ext in extensions):
            return PredicateResult.MET
    # Check recent_tool_use path
    if ctx.recent_tool_use:
        tool_path = ctx.recent_tool_use.get("path", "")
        if tool_path and any(str(tool_path).endswith(ext) for ext in extensions):
            return PredicateResult.MET
    return PredicateResult.NOT_MET


# --- build-contract density + tag-focus (#12 / #12b) ---------------------


def _count_task_items(content: str) -> int:
    """Count top-level task entries under any ``## Tasks`` heading.

    A task entry is a top-level (<=3 leading spaces) markdown list item — a bullet
    (``-``/``*``/``+``) or an ordered item (``1.``/``1)``). Counting is scoped to the
    ``## Tasks`` section (any heading level) and stops at the next heading. Returns 0
    when there is no ``## Tasks`` section or it carries no list items.
    """
    item_re = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s+\S")
    heading_re = re.compile(r"^#{1,6}\s")
    count = 0
    in_tasks = False
    for line in content.splitlines():
        if heading_re.match(line):
            in_tasks = _section_present("Tasks", [line.lstrip("#").strip()])
            continue
        if in_tasks and item_re.match(line):
            count += 1
    return count


def _resolve_workitem_slug(ctx: PredicateContext, phase: str) -> str | None:
    """The cursor'd work-item slug for ``phase`` — phase-strict.

    Delegates to :func:`_resolve_workitem_slug_for` (store-first cursor read,
    sole-contract fallback) so the gate and ``run_approve`` share one resolution
    path (#518). Queries the store for active contracts in ``phase``; if a
    cursor is set and points to a contract in this phase, returns that
    contract's slug; falls back to the sole contract in the phase, else
    ``None``. So a design→build gate always scopes to the design work-item,
    never a sibling the cursor drifted to.
    """
    return _resolve_workitem_slug_for(ctx.store, ctx.project_root, phase)


def _contract_work_item(content: str) -> str | None:
    """The ``work_item`` frontmatter field (the parent design-item slug that
    ``contract init --phase build`` stamps from the active design cursor), or
    ``None`` when the field is absent or the file has no parseable frontmatter."""
    import yaml as _yaml

    if not content.startswith("---"):
        return None
    end = content.find("---", 3)
    if end == -1:
        return None
    try:
        fm: dict[str, Any] = _yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None
    wi = fm.get("work_item")
    return wi if isinstance(wi, str) and wi else None


def _item_build_contracts(
    ctx: PredicateContext, slug: str, *, contracts_glob: str | None = None
) -> list[dict[str, Any]]:
    """The build contracts attributed to design work-item ``slug``.

    Attribution is the contract's ``work_item`` field. Build contracts are keyed
    by contract_id, so the field is the only per-item signal. Migration bridge: a
    repo whose build contracts predate the field carries ``work_item`` on *none* of
    them; to avoid a spurious block there, attribution falls back to **all** build
    contracts when NONE declares a ``work_item`` (the old repo-global behavior).
    Once any contract is stamped, only contracts stamped to *this* item count.

    ``contracts_glob`` is retained for legacy gate YAML that still carries a
    ``contracts`` key; when present it triggers a deprecation trace but is not
    used for resolution (the store is authoritative).
    """
    # Legacy glob tolerance: emit trace if a glob arg is present
    if contracts_glob is not None:
        _emit_legacy_glob_trace(contracts_glob)

    # Query store for build contracts
    build_contracts = _query_store_contracts(ctx, phase="build") or []

    any_tagged = False
    mine: list[dict[str, Any]] = []
    for c in build_contracts:
        wi = c.get("work_item")
        if wi is not None:
            any_tagged = True
            if wi == slug:
                mine.append(c)
    return mine if any_tagged else build_contracts


def eval_build_contracts_cover_tasks(
    args: dict[str, Any], ctx: PredicateContext
) -> PredicateResult:
    """MET when #build-contracts >= #tasks for the CURSOR'D work-item (floor 1).

    Deterministic and embed-free, and **cursor-scoped** (#378): both sides judge
    the active design work-item, not the repo aggregate — so a fully-decomposed
    item advances regardless of sibling items still mid-design. Resolves the design
    slug (:func:`_resolve_workitem_slug`), counts top-level ``## Tasks`` items in
    that item's ``docs/design/<slug>/tasks.md``, floor-clamps to 1 (never relaxes
    the >=1-contract floor, never blocks on an unparseable tasks.md), and compares
    to the item's own build contracts (:func:`_item_build_contracts`). Returns
    UNKNOWN (fail-open) when no single work-item resolves, no tasks.md exists, or
    one is unreadable — a preceding artifact node owns the missing-file case.

    ``tasks_from_store: true`` (set by the design pack post-migration) reads
    ``tasks.md`` from the artifact store instead of disk. This is a distinct
    switch from ``ctx.store`` being set: ``ctx.store`` is also bound to resolve
    the build-contract count below, independent of where ``tasks.md`` lives, so
    branching on its mere presence would silently stop reading a
    still-on-disk ``tasks.md`` for any caller that happens to pass a store.
    """
    phase = str(args.get("phase") or "design")
    slug = _resolve_workitem_slug(ctx, phase)
    if slug is None:
        return PredicateResult.UNKNOWN

    # Legacy glob tolerance: pass through if present (traces deprecation)
    contracts_glob: str | None = args.get("contracts")

    if args.get("tasks_from_store") and ctx.store is not None:
        try:
            artifact = ctx.store.get_artifact(phase, slug, "tasks.md")
        except Exception:
            return PredicateResult.UNKNOWN  # store error → fail closed, never MET
        if artifact is None or artifact.get("content") is None:
            return PredicateResult.UNKNOWN
        task_count = _count_task_items(artifact["content"])
    else:
        tasks_glob = args.get("tasks", "docs/design/{slug}/tasks.md").replace("{slug}", slug)
        task_files = _glob_files(ctx.project_root, tasks_glob)
        if not task_files:
            return PredicateResult.UNKNOWN
        task_count = 0
        for f in task_files:
            content = _read_file(f)
            if content is None:
                return PredicateResult.UNKNOWN
            task_count += _count_task_items(content)
    task_count = max(1, task_count)
    contract_count = len(_item_build_contracts(ctx, slug, contracts_glob=contracts_glob))
    return PredicateResult.MET if contract_count >= task_count else PredicateResult.NOT_MET


def _contract_domain_tags(content: str) -> list[Any] | None:
    """Parse the ``domain_tags`` list from a contract's YAML frontmatter.

    Returns the tag list (``[]`` when the field is absent or non-list), or ``None``
    when there is no parseable frontmatter — so a malformed/headerless file is
    skipped rather than flagged.
    """
    import yaml as _yaml

    if not content.startswith("---"):
        return None
    end = content.find("---", 3)
    if end == -1:
        return None
    try:
        fm: dict[str, Any] = _yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None
    tags: list[Any] | None = fm.get("domain_tags")
    return tags if isinstance(tags, list) else []


def eval_build_contract_tag_focus(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET when every build contract carries <=2 domain_tags (one dominant surface).

    Tag-focus hard gate (#12b): with the fixed per-contract retrieval budget,
    fragments spread across many surfaces truncate and scores muddy, so each build
    contract must center ONE dominant tech surface. NOT_MET if ANY contract has
    more than ``max_tags`` (default 2) domain_tags. Embed-free and deterministic;
    UNKNOWN only when no contracts exist (a preceding artifact_exists node handles
    that in all_of).

    Cursor-scoped (#378): judges only the active design work-item's own build
    contracts (:func:`_item_build_contracts`), so a sibling item's wide-tag
    contract never blocks this item's design→build. UNKNOWN (fail-open) when no
    single work-item resolves, mirroring :func:`eval_build_contracts_cover_tasks`.
    """
    # Legacy glob tolerance: pass through if present (traces deprecation)
    contracts_glob: str | None = args.get("contracts")
    max_tags = args.get("max_tags", 2)
    slug = _resolve_workitem_slug(ctx, str(args.get("phase") or "design"))
    if slug is None:
        return PredicateResult.UNKNOWN
    contracts = _item_build_contracts(ctx, slug, contracts_glob=contracts_glob)
    if not contracts:
        return PredicateResult.UNKNOWN
    for c in contracts:
        tags: list[Any] = c.get("domain_tags") or []
        if len(tags) > max_tags:
            return PredicateResult.NOT_MET
    return PredicateResult.MET


PREDICATES: dict[str, Callable[[dict[str, Any], PredicateContext], PredicateResult]] = {
    "artifact_exists": eval_artifact_exists,
    "artifact_absent": eval_artifact_absent,
    "tests_present": eval_tests_present,
    "artifact_contains": eval_artifact_contains,
    "artifact_size_min": eval_artifact_size_min,
    "artifact_newer_than": eval_artifact_newer_than,
    "approval_recorded": eval_approval_recorded,
    "lessons_recorded": eval_lessons_recorded,
    "phase_in": eval_phase_in,
    "phase_not_in": eval_phase_not_in,
    "tool_use_about_to_fire": eval_tool_use_about_to_fire,
    "tool_use_just_completed": eval_tool_use_just_completed,
    "git_state": eval_git_state,
    "contract_exists": eval_contract_exists,
    "contract_has_tags": eval_contract_has_tags,
    "file_type_active": eval_file_type_active,
    "build_contracts_cover_tasks": eval_build_contracts_cover_tasks,
    "build_contract_tag_focus": eval_build_contract_tag_focus,
    "scope_touched_in_diff": eval_scope_touched_in_diff,
}


def evaluate_predicate(
    predicate_name: str,
    args: dict[str, Any],
    ctx: PredicateContext,
) -> PredicateResult:
    """Evaluate a named deterministic predicate. Raises ValueError for unknown names."""
    if predicate_name not in PREDICATES:
        raise ValueError(f"Unknown predicate '{predicate_name}'. Available: {sorted(PREDICATES)}")
    try:
        return PREDICATES[predicate_name](args, ctx)
    except Exception:
        return PredicateResult.UNKNOWN


# ---------------------------------------------------------------------------
# Gate feedback — AC completeness check for phase gates
# ---------------------------------------------------------------------------


def _gate_trigger_enabled() -> bool:
    """Whether the gate-trigger (AC feedback) feature is enabled.

    Reads ``AGENTALLOY_GATE_TRIGGER_ENABLED`` env var. Defaults to ``True``.
    """
    try:
        val = os.environ.get("AGENTALLOY_GATE_TRIGGER_ENABLED", "1")
        return val.lower() not in ("0", "false", "no", "")
    except Exception:
        return True


def _ac_is_met(
    store: Any,
    phase: str,
    slug: str,
    ac_id: str,
    ac_text: str,
) -> bool:
    """Whether a single acceptance criterion is met in the phase's artifacts.

    Lists artifacts for ``phase``/``slug``, reads each artifact's content,
    checks if ``ac_id`` or ``ac_text`` appears as a substring.
    """
    try:
        rows = _list_store_artifacts(
            PredicateContext(project_root=Path.cwd(), store=store),
            phase=phase,
            slug=slug,
        )
        if rows is None:
            return False
        for row in rows:
            content = row.get("content") or ""
            if ac_id in content or ac_text in content:
                return True
        return False
    except Exception:
        return False


def _evaluate_ac_feedback(
    store: Any,
    contract: dict[str, Any],
) -> str | None:
    """Evaluate AC feedback for a contract's success criteria.

    For each structured AC in ``success_criteria``, checks if the AC ID or
    text appears in any artifact for the phase (simple substring match).
    Returns ``[agentalloy-gate-feedback] Unmet criteria: AC-X, AC-Y`` if any
    are unmet, or ``None`` if all met.
    """
    if not _gate_trigger_enabled():
        return None

    success_criteria = contract.get("success_criteria") or []
    phase = str(contract.get("phase", ""))
    slug = str(contract.get("slug", ""))

    if not phase or not slug:
        return None

    unmet: list[str] = []
    for criterion in success_criteria:
        if isinstance(criterion, dict):
            ac_id = str(criterion.get("id", ""))
            ac_text = str(criterion.get("text", ""))
        else:
            # String criterion: treat the whole string as both id and text
            ac_id = str(criterion)
            ac_text = str(criterion)

        if not ac_id or not ac_text:
            continue

        if not _ac_is_met(store, phase, slug, ac_id, ac_text):
            unmet.append(ac_id)

    if unmet:
        return f"[agentalloy-gate-feedback] Unmet criteria: {', '.join(unmet)}"
    return None
