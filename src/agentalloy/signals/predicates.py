"""Deterministic predicate evaluators for phase gate evaluation.

Predicates are pure functions: (args: dict, ctx: PredicateContext) -> PredicateResult.
They never raise; they return UNKNOWN on any IO or context failure.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, cast


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
    contracts_root: Path | None = None  # .agentalloy/contracts/
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

    def __post_init__(self) -> None:
        if self.contracts_root is None:
            # Can't set on frozen dataclass directly; use object.__setattr__
            object.__setattr__(
                self, "contracts_root", self.project_root / ".agentalloy" / "contracts"
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


def _glob_files(root: Path, pattern: str) -> list[Path]:
    """Return files matching glob pattern under root (or absolute if pattern is absolute)."""
    try:
        if Path(pattern).is_absolute():
            p = Path(pattern)
            if p.exists():
                return [p]
            return []
        # Use rglob-style glob
        results = list(root.glob(pattern))
        return results
    except Exception:
        return []


def _read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def eval_artifact_exists(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
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
    UNKNOWN on IO failure.
    """
    pattern = args.get("path", "")
    if not pattern:
        return PredicateResult.UNKNOWN
    files = _glob_files(ctx.project_root, pattern)
    if not files:
        return PredicateResult.NOT_MET

    sections = args.get("sections")
    regex_pattern = args.get("pattern")

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

    File I/O is fully wrapped: a missing glob match or an unreadable file yields
    ``(0, total, required_sections)`` — i.e. no progress, every section "missing" —
    so the banner never raises and a not-yet-created artifact simply shows 0 present.
    Never raises.
    """
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
_ALWAYS_APPROVAL_PHASES = ("spec", "design")


def approval_required(phase: str | None) -> bool:
    """True when leaving *phase* requires a recorded human approval.

    spec/design: always. sdd-fast: behind SDD_FAST_REQUIRE_APPROVAL (default OFF).
    Everything else (intake, build, qa, ship): never.
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


def eval_approval_recorded(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET when leaving the current phase is permitted by the human-approval gate.

    The marker path is *derived from phase* (not a ``path`` arg) so the prefilter's
    gate-path walker never collects it and never emits a misleading "produce its
    exit artifact" advisory. ``since`` (the exit-artifact glob) makes the marker go
    stale when the artifact is edited after approval.
    """
    phase = args.get("phase") or ctx.current_phase
    if phase is None:
        return PredicateResult.UNKNOWN
    if not approval_required(phase):
        return PredicateResult.MET  # route is not approval-gated → satisfied
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


def eval_contract_exists(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    phase = args.get("phase", ctx.current_phase)
    count_min = args.get("count_min", 1)
    if phase is None or ctx.contracts_root is None:
        return PredicateResult.UNKNOWN
    contracts_dir = ctx.contracts_root / phase
    if not contracts_dir.exists():
        return PredicateResult.NOT_MET
    try:
        count = sum(1 for _ in contracts_dir.glob("*.md"))
        return PredicateResult.MET if count >= count_min else PredicateResult.NOT_MET
    except OSError:
        return PredicateResult.UNKNOWN


def eval_contract_has_tags(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """Check whether any contract in the phase directory has matching domain_tags.

    Semantics: ANY contract file with ANY matching tag → MET.
    Returns NOT_MET if no contract has any of the specified tags, UNKNOWN on IO failure.
    """
    import yaml as _yaml

    phase = args.get("phase", ctx.current_phase)
    any_of_tags = args.get("any_of", [])
    if phase is None or ctx.contracts_root is None:
        return PredicateResult.UNKNOWN
    contracts_dir = ctx.contracts_root / phase
    if not contracts_dir.exists():
        return PredicateResult.NOT_MET
    try:
        for contract_file in contracts_dir.glob("*.md"):
            content = _read_file(contract_file)
            if content is None:
                continue
            # Extract frontmatter
            if not content.startswith("---"):
                continue
            end = content.find("---", 3)
            if end == -1:
                continue
            try:
                fm: dict[str, Any] = _yaml.safe_load(content[3:end]) or {}
            except Exception:
                continue
            tags: list[Any] = fm.get("domain_tags") or []
            if any(t in tags for t in any_of_tags):
                return PredicateResult.MET
    except OSError:
        return PredicateResult.UNKNOWN
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


def eval_build_contracts_cover_tasks(
    args: dict[str, Any], ctx: PredicateContext
) -> PredicateResult:
    """MET when #build-contracts >= #tasks enumerated in tasks.md (floor 1).

    Deterministic and embed-free. Counts top-level list items under ``## Tasks``
    across the ``tasks`` glob, clamps the task count to a floor of 1 (so it never
    relaxes the existing >=1-contract gate and never blocks on an unparseable
    tasks.md), and compares that to the number of build-contract files. Returns
    UNKNOWN when no tasks.md exists or one is unreadable (a preceding
    artifact_exists/contains node handles the missing-file case in all_of).
    """
    tasks_glob = args.get("tasks", "docs/design/**/tasks.md")
    contracts_glob = args.get("contracts", ".agentalloy/contracts/build/*.md")
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
    contract_count = len([p for p in _glob_files(ctx.project_root, contracts_glob) if p.is_file()])
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
    tags = fm.get("domain_tags")
    return tags if isinstance(tags, list) else []


def eval_build_contract_tag_focus(args: dict[str, Any], ctx: PredicateContext) -> PredicateResult:
    """MET when every build contract carries <=2 domain_tags (one dominant surface).

    Tag-focus hard gate (#12b): with the fixed per-contract retrieval budget,
    fragments spread across many surfaces truncate and scores muddy, so each build
    contract must center ONE dominant tech surface. NOT_MET if ANY contract has
    more than ``max_tags`` (default 2) domain_tags. Embed-free and deterministic;
    UNKNOWN only when no contracts exist (a preceding artifact_exists node handles
    that in all_of).
    """
    contracts_glob = args.get("contracts", ".agentalloy/contracts/build/*.md")
    max_tags = args.get("max_tags", 2)
    files = [p for p in _glob_files(ctx.project_root, contracts_glob) if p.is_file()]
    if not files:
        return PredicateResult.UNKNOWN
    for f in files:
        content = _read_file(f)
        if content is None:
            continue
        tags = _contract_domain_tags(content)
        if tags is None:
            continue
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
