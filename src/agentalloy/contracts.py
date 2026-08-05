"""Contract artifact: in-memory model and markdown+frontmatter serializer.

A contract is stored in the DuckDB state store (``sdd_contract`` table) and
represented in memory by :class:`Contract`.  The markdown+frontmatter
serializer is retained for ``contract show`` and the web edit round-trip.

Format::

    ---
    phase: build
    task_slug: add-auth-middleware
    domain_tags:
      - NestJS
      - JWT validation
    scope:
      touches:
        - "src/auth/**"
      avoids:
        - "src/billing/**"
    success_criteria:
      - "Existing auth tests still pass"
    related_contracts: []
    created_at: 2026-05-21T14:32:11Z
    ---

    # Add Auth Middleware

    <task description prose>
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractScope:
    touches: list[str]  # globs; may be empty
    avoids: list[str]  # globs; may be empty


@dataclass(frozen=True)
class Contract:
    contract_id: str
    phase: str
    task_slug: str
    domain_tags: list[str]
    scope: ContractScope
    success_criteria: list[dict]
    related_contracts: list[str]
    created_at: datetime | None
    body: str
    # Workflow route chosen at intake: "full" (spec→design→build→qa→ship) or
    # "fast" (sdd-fast→qa→ship). Authoritative routing signal: the intake→next
    # transition reads this field (via _intake_route_hint) to branch the phase
    # graph, falling back to contract-folder presence only when no intake
    # contract is readable.
    route: str = "full"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContractError(Exception):
    """Base for contract problems."""


class ContractMalformed(ContractError):
    """Frontmatter missing, schema invalid, etc."""


class ContractPhaseMismatch(ContractError):
    """Contract's phase field doesn't match the current store phase."""


# ---------------------------------------------------------------------------
# Frontmatter parser (inline — no python-frontmatter dependency required)
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split markdown+frontmatter into (metadata_dict, body_str).

    Raises ContractMalformed if the frontmatter delimiter is missing or the
    YAML cannot be parsed.
    """
    if not text.startswith("---"):
        raise ContractMalformed("Contract must begin with '---' YAML frontmatter delimiter")

    # Find closing delimiter
    rest = text[3:].lstrip("\n")
    end_match = __import__("re").search(r"^---\s*$", rest, __import__("re").MULTILINE)
    if not end_match:
        raise ContractMalformed("Contract frontmatter is not closed with a '---' delimiter")

    fm_text = rest[: end_match.start()]
    body = rest[end_match.end() :].lstrip("\n")

    try:
        raw: Any = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise ContractMalformed(f"Contract frontmatter YAML is invalid: {exc}") from exc

    if not isinstance(raw, dict):
        raise ContractMalformed("Contract frontmatter must be a YAML mapping")

    data: dict[str, Any] = cast(dict[str, Any], raw)
    return data, body


# ---------------------------------------------------------------------------
# Parsing — from markdown text
# ---------------------------------------------------------------------------


def parse_contract_text(
    text: str,
    *,
    contract_id: str,
) -> Contract:
    """Parse a contract from markdown+frontmatter text.

    Raises ContractMalformed on errors.  ``contract_id`` is the store key
    (e.g. ``01-add-auth``) — it is not derived from the text.
    """
    data, body = _split_frontmatter(text)

    # Required fields
    phase = data.get("phase")
    if not phase or not isinstance(phase, str):
        raise ContractMalformed("Contract 'phase' field is required and must be a non-empty string")

    task_slug = data.get("task_slug")
    if not task_slug or not isinstance(task_slug, str):
        raise ContractMalformed(
            "Contract 'task_slug' field is required and must be a non-empty string"
        )

    # domain_tags is optional: when empty/absent the compose engine retrieves
    # from the contract body text (tags are only a soft BM25 boost, not a hard
    # filter). A present value must still be a list, not e.g. a bare string.
    domain_tags_raw = data.get("domain_tags") or []
    if not isinstance(domain_tags_raw, list):
        raise ContractMalformed("Contract 'domain_tags' must be a list when present")
    domain_tags = [str(t) for t in cast(list[Any], domain_tags_raw)]

    # Optional scope
    scope_raw: dict[str, Any] = data.get("scope") or {}
    scope = ContractScope(
        touches=[str(g) for g in cast(list[Any], scope_raw.get("touches") or [])],
        avoids=[str(g) for g in cast(list[Any], scope_raw.get("avoids") or [])],
    )

    success_criteria_raw = cast(list[Any], data.get("success_criteria") or [])
    success_criteria = _normalize_success_criteria(success_criteria_raw)

    # related_contracts — list of contract_ids (strings)
    related_raw: list[Any] = data.get("related_contracts") or []
    related_contracts = [str(r) for r in related_raw]

    # created_at — optional
    created_at: datetime | None = None
    raw_ts = data.get("created_at")
    if raw_ts:
        try:
            if isinstance(raw_ts, str):
                created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            elif isinstance(raw_ts, datetime):
                created_at = raw_ts
        except (ValueError, TypeError):
            created_at = None

    route = str(data.get("route") or "full").strip().lower()
    if route not in ("full", "fast", "add-skill"):
        raise ContractMalformed(
            f"Contract 'route' must be 'full', 'fast', or 'add-skill', got '{route}'"
        )

    return Contract(
        contract_id=contract_id,
        phase=phase,
        task_slug=task_slug,
        domain_tags=domain_tags,
        scope=scope,
        success_criteria=success_criteria,
        related_contracts=related_contracts,
        created_at=created_at,
        body=body,
        route=route,
    )


def _normalize_success_criteria(raw: list[Any]) -> list[dict]:
    """Normalize success_criteria to list[dict] with id/text keys.

    Handles both legacy format (list[str]) and new format (list[dict]).

    Legacy: ["AC-1: feature works", "AC-2: another feature"]
    New: [{"id": "AC-1", "text": "feature works"}, ...]

    Legacy entries are preserved as-is with auto-generated IDs to maintain
    backward compatibility. New entries must have both id and text keys.
    """
    if not raw:
        return []

    normalized: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            # New format: already has id/text
            normalized.append(
                {
                    "id": str(item.get("id", "")),
                    "text": str(item.get("text", "")),
                }
            )
        else:
            # Legacy format: string, preserve as-is
            normalized.append({"id": str(item), "text": str(item)})

    return normalized


def parse_ac_headings(markdown: str) -> list[dict]:
    """Extract AC IDs and text from markdown headings.

    Matches ## AC-N: text or ### AC-N: text patterns.
    Returns list of {id: f"AC-{n}", text: stripped_text}.
    """
    import re

    pattern = r"^(?:#{2,3})\s+AC-(\d+)[\s:]+\s*(.+)$"
    results: list[dict] = []
    for line in markdown.split("\n"):
        m = re.match(pattern, line.strip())
        if m:
            results.append({"id": f"AC-{m.group(1)}", "text": m.group(2).strip()})
    return results


def contract_from_row(row: dict[str, Any]) -> Contract:
    """Construct a :class:`Contract` from a store row dict.

    ``row`` is the dict returned by ``DuckDBStateStore.get_contract()`` or
    ``DuckDBStateStore.list_contracts()``.
    """
    created_at: datetime | None = None
    raw_ts = row.get("created_at")
    if isinstance(raw_ts, str):
        with contextlib.suppress(ValueError):
            created_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    elif isinstance(raw_ts, datetime):
        created_at = raw_ts

    domain_tags = row.get("domain_tags") or []
    scope_touches = row.get("scope_touches") or []
    scope_avoids = row.get("scope_avoids") or []
    success_criteria_raw = row.get("success_criteria") or []

    route = str(row.get("route") or "full").strip().lower()
    if route not in ("full", "fast", "add-skill"):
        route = "full"

    return Contract(
        contract_id=str(row["contract_id"]),
        phase=str(row["phase"]),
        task_slug=str(row.get("slug", "")),
        domain_tags=[str(t) for t in domain_tags],
        scope=ContractScope(
            touches=[str(g) for g in scope_touches],
            avoids=[str(g) for g in scope_avoids],
        ),
        success_criteria=_normalize_success_criteria(success_criteria_raw),
        related_contracts=[],
        created_at=created_at,
        body=str(row.get("body") or ""),
        route=route,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_contract(contract: Contract, project_root: Any) -> list[str]:
    """Return a list of issues (empty = valid). Does not raise.

    Note: ``project_root`` is retained for backward compatibility with
    existing callers but is no longer used for phase-file checks.
    """
    issues: list[str] = []

    # scope.touches globs valid syntax
    for pattern in contract.scope.touches + contract.scope.avoids:
        try:
            fnmatch.translate(pattern)
        except Exception:
            issues.append(f"Invalid glob pattern in scope: {pattern!r}")

    return issues


# ---------------------------------------------------------------------------
# Validation — from store row dict
# ---------------------------------------------------------------------------


def validate_contract_from_dict(row: dict[str, Any]) -> list[str]:
    """Validate a contract from a store row dict. Returns list of issues (empty = valid).

    Converts the row to a :class:`Contract` via :func:`contract_from_row`, then
    delegates to :func:`validate_contract`.
    """
    try:
        contract = contract_from_row(row)
    except (KeyError, TypeError, ValueError) as exc:
        return [f"Cannot parse contract from row: {exc}"]

    # Required field checks
    issues: list[str] = []
    if not contract.phase:
        issues.append("Contract 'phase' is empty")
    if not contract.task_slug:
        issues.append("Contract 'task_slug' is empty")

    issues.extend(validate_contract(contract, None))
    return issues


# ---------------------------------------------------------------------------
# Code index query construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeIndexQuery:
    """Parameters for an in-process code-index search derived from a contract.

    ``repo`` + ``semantic_q`` map onto ``GET /code/search/semantic?repo=&q=``
    and ``repo`` + ``lexical_q`` onto ``GET /code/search/lexical?repo=&q=`` on
    the local service port.
    """

    repo: str
    semantic_q: str
    lexical_q: str | None
    path_globs: list[str]


def code_index_query_params(contract: Contract) -> CodeIndexQuery:
    """Build ``/code/search/*`` query parameters from a contract.

    Derives the repo slug from the contract's ``slug`` field.
    """
    body = (contract.body or "").strip()
    first_line = body.split("\n")[0].lstrip("# ").strip() if body else ""
    semantic_q = first_line or contract.task_slug

    lexical_q = " ".join(contract.domain_tags) if contract.domain_tags else None
    path_globs = list(contract.scope.touches) if contract.scope.touches else []

    return CodeIndexQuery(
        repo=contract.task_slug,
        semantic_q=semantic_q,
        lexical_q=lexical_q,
        path_globs=path_globs,
    )


def cursor_state_name(session_key: str | None) -> str:
    """Backing filename for the work-item cursor, session-scoped when possible.

    A repo has ONE ``.agentalloy/contracts`` tree but may be driven by several
    concurrent sessions; a single shared ``.agentalloy/cursor`` lets one session's
    ``task start`` clobber another's current work-item (Bug C). Scoping the cursor
    file by the session key isolates them: ``cursor.<sha1(key)[:16]>`` when a key is
    known, else the shared ``cursor`` (single-session, non-Claude-Code harnesses,
    and every pre-scoping repo — the back-compat floor). The key is the same value
    on both sides: the proxy's ``x-claude-code-session-id`` header and the CLI's
    ``CLAUDE_CODE_SESSION_ID`` env var are the one session UUID, so a scoped write
    by the CLI is read back by the proxy (and vice versa) across the container bind
    mount. The cursor is deliberately NOT a relocated runtime-state key, so scoped
    files stay in the repo tree where both sides see them."""
    if not session_key:
        return "cursor"
    digest = hashlib.sha1(session_key.encode()).hexdigest()[:16]
    return f"cursor.{digest}"


# ---------------------------------------------------------------------------
# Backward-compat filesystem helpers
#
# Retained for consumers outside the compose path (predicates, CLI, skill_loader)
# that will be migrated in later tasks. The compose/proxy reader path uses the
# store exclusively (contract_id-based).
# ---------------------------------------------------------------------------


def safe_contract_path(
    path_str: str,
    project_root: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Validate a user-supplied contract path is contained under ``.agentalloy/contracts/``.

    Returns ``(resolved_path, project_root)`` on success, ``(None, None)`` on failure.
    """
    try:
        resolved = Path(path_str).resolve()
    except OSError:
        return None, None

    if not resolved.is_file():
        return None, None

    contracts_root: Path | None = None
    for ancestor in resolved.parents:
        if ancestor.name == ".agentalloy":
            contracts_root = ancestor / "contracts"
            break
    if contracts_root is None:
        return None, None

    derived_root = contracts_root.parent.parent

    if project_root is not None:
        try:
            project_resolved = project_root.resolve()
        except OSError:
            return None, None
        try:
            resolved.relative_to(project_resolved)
        except ValueError:
            return None, None
        derived_root = project_resolved

    try:
        resolved.relative_to(contracts_root.resolve())
    except (ValueError, OSError):
        return None, None

    return resolved, derived_root


def contracts_root(project_root: Path) -> Path:
    """The ``.agentalloy/contracts`` directory for *project_root*."""
    return project_root / ".agentalloy" / "contracts"


def active_dir(project_root: Path, phase: str) -> Path:
    """Where live contracts for *phase* belong: ``contracts/active/<phase>/``."""
    return contracts_root(project_root) / "active" / phase


def archive_dir(project_root: Path, phase: str) -> Path:
    """Where completed contracts for *phase* belong: ``contracts/archive/<phase>/``."""
    return contracts_root(project_root) / "archive" / phase


def list_contracts_for_phase(project_root: Path, phase: str) -> list[Path]:
    """Return all .agentalloy/contracts/active/<phase>/*.md sorted newest-first by mtime."""
    contracts_dir = active_dir(project_root, phase)
    if not contracts_dir.is_dir():
        return []
    files = [f for f in contracts_dir.glob("*.md") if f.is_file()]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def ordered_contracts_for_phase(project_root: Path, phase: str) -> list[Path]:
    """Return contracts/<phase>/*.md in FILENAME order (``01-``, ``02-``, …)."""
    contracts_dir = active_dir(project_root, phase)
    if not contracts_dir.is_dir():
        return []
    return sorted((f for f in contracts_dir.glob("*.md") if f.is_file()), key=lambda f: f.name)


def first_workitem_id(project_root: Path, phase: str) -> str | None:
    """The first work-item of ``phase`` (filename order) as a cursor id."""
    contract_files = ordered_contracts_for_phase(project_root, phase)
    if not contract_files:
        return None
    return f"active/{phase}/{contract_files[0].name}"


def _read_contract_phase(path: Path) -> str | None:
    """Best-effort read of a contract's ``phase`` frontmatter field."""
    try:
        data, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ContractMalformed):
        return None
    phase = data.get("phase")
    if isinstance(phase, str) and phase.strip():
        return phase.strip()
    return None


@dataclass(frozen=True)
class ContractMove:
    """A single planned relocation of a contract file into the tree."""

    src: Path
    dst: Path
    archived: bool


@dataclass(frozen=True)
class MigrationPlan:
    """The side-effect-free result of planning a legacy → tree migration."""

    moves: list[ContractMove]
    collisions: list[tuple[Path, Path]]
    unreadable: list[Path]

    @property
    def is_empty(self) -> bool:
        return not (self.moves or self.collisions or self.unreadable)


def plan_contracts_migration(project_root: Path) -> MigrationPlan:
    """Plan the move of a repo's legacy-layout contracts into the tree."""
    root = contracts_root(project_root)
    moves: list[ContractMove] = []
    collisions: list[tuple[Path, Path]] = []
    unreadable: list[Path] = []
    claimed: dict[Path, Path] = {}

    if not root.is_dir():
        return MigrationPlan(moves, collisions, unreadable)

    for src in sorted(root.rglob("*.md")):
        if not src.is_file():
            continue
        parts = src.relative_to(root).parts
        top = parts[0]

        if top == "active":
            continue
        if top == "archive" and len(parts) >= 3:
            continue

        phase = _read_contract_phase(src)
        if phase is None:
            unreadable.append(src)
            continue

        archived = top in ("archive", "_superseded")
        base = archive_dir(project_root, phase) if archived else active_dir(project_root, phase)
        dst = base / src.name

        if dst == src:
            continue

        if dst in claimed or (dst.exists() and dst.resolve() != src.resolve()):
            collisions.append((src, dst))
            continue

        claimed[dst] = src
        moves.append(ContractMove(src=src, dst=dst, archived=archived))

    return MigrationPlan(moves=moves, collisions=collisions, unreadable=unreadable)


def apply_contracts_migration(plan: MigrationPlan) -> list[ContractMove]:
    """Execute a plan's ``moves`` on disk, returning the moves performed."""
    done: list[ContractMove] = []
    for mv in plan.moves:
        if not mv.src.exists():
            continue
        mv.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mv.src), str(mv.dst))
        done.append(mv)
    return done


def cursor_after_migration(cursor: str | None, moves: list[ContractMove], root: Path) -> str | None:
    """Return the cursor value rewritten to follow a migration, or unchanged."""
    if not cursor:
        return cursor
    for mv in moves:
        try:
            old_rel = mv.src.relative_to(root).as_posix()
        except ValueError:
            continue
        if old_rel == cursor:
            return mv.dst.relative_to(root).as_posix()
    return cursor


def plan_archive(
    project_root: Path, *, phase: str | None = None, slug: str | None = None
) -> MigrationPlan:
    """Plan moving live contracts from ``active/<phase>/`` to ``archive/<phase>/``."""
    root = contracts_root(project_root)
    active_root = root / "active"
    moves: list[ContractMove] = []
    collisions: list[tuple[Path, Path]] = []
    claimed: dict[Path, Path] = {}

    if not active_root.is_dir():
        return MigrationPlan(moves, collisions, [])

    phase_dirs = (
        [active_root / phase] if phase else [d for d in active_root.iterdir() if d.is_dir()]
    )
    for pdir in sorted(phase_dirs):
        if not pdir.is_dir():
            continue
        ph = pdir.name
        for src in sorted(pdir.glob("*.md")):
            if not src.is_file():
                continue
            if slug is not None and src.stem != slug:
                continue
            dst = archive_dir(project_root, ph) / src.name
            if dst in claimed or (dst.exists() and dst.resolve() != src.resolve()):
                collisions.append((src, dst))
                continue
            claimed[dst] = src
            moves.append(ContractMove(src=src, dst=dst, archived=True))

    return MigrationPlan(moves=moves, collisions=collisions, unreadable=[])


def _read_cursor_value(project_root: Path, session_key: str | None = None) -> str | None:
    """Read the work-item cursor (a contracts-relative id)."""
    names = [cursor_state_name(session_key)]
    if names[0] != "cursor":
        names.append("cursor")
    for name in names:
        try:
            raw = (project_root / ".agentalloy" / name).read_text(encoding="utf-8")
        except OSError:
            continue
        value = raw.strip()
        if value:
            return value
    return None


def resolve_current_contract(
    project_root: Path, phase: str, session_key: str | None = None
) -> tuple[str | None, Path | None]:
    """Resolve the current work-item contract for ``phase``.

    Returns ``(contract_id, abs_path)`` where ``contract_id`` is the
    contracts-relative posix path and ``abs_path`` is the file to use.
    """
    cr = (project_root / ".agentalloy" / "contracts").resolve()
    cursor = _read_cursor_value(project_root, session_key)
    if cursor:
        candidate = (cr / cursor).resolve()
        if candidate.is_file() and candidate.is_relative_to(cr):
            return candidate.relative_to(cr).as_posix(), candidate

    in_phase = list_contracts_for_phase(project_root, phase)
    if len(in_phase) != 1:
        return None, None
    only = in_phase[0].resolve()
    return only.relative_to(cr).as_posix(), only


def latest_contract(project_root: Path, phase: str | None = None) -> Path | None:
    """Most recently modified contract (optionally filtered by phase)."""
    if phase:
        files = list_contracts_for_phase(project_root, phase)
        return files[0] if files else None

    active_root = contracts_root(project_root) / "active"
    if not active_root.is_dir():
        return None

    all_files: list[Path] = []
    for phase_dir in active_root.iterdir():
        if phase_dir.is_dir():
            all_files.extend(f for f in phase_dir.glob("*.md") if f.is_file())

    if not all_files:
        return None

    return max(all_files, key=lambda f: f.stat().st_mtime)


def parse_contract(path: Path) -> Contract:
    """Read and validate a contract file from the filesystem.

    Backward-compat wrapper: derives ``contract_id`` from the filename stem.
    Raises ContractMalformed on errors.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractMalformed(f"Cannot read contract file {path}: {exc}") from exc

    # Derive contract_id from filename stem (without .md extension)
    contract_id = path.stem

    return parse_contract_text(text, contract_id=contract_id)
