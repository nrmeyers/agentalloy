"""Contract artifact: parsing, validation, and file management.

A contract is a markdown file with YAML frontmatter written by the paid LLM
to state task intent and domain tags. It drives domain retrieval (Phase 2)
and gate evaluation (Phase 3).

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

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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
    path: Path
    phase: str
    task_slug: str
    domain_tags: list[str]
    scope: ContractScope
    success_criteria: list[str]
    related_contracts: list[Path]
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
    """Contract's phase field doesn't match .agentalloy/phase."""


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
    end_match = re.search(r"^---\s*$", rest, re.MULTILINE)
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
# Parsing
# ---------------------------------------------------------------------------


def parse_contract(path: Path) -> Contract:
    """Read and validate a contract file. Raises ContractMalformed on errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractMalformed(f"Cannot read contract file {path}: {exc}") from exc

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

    success_criteria = [str(c) for c in cast(list[Any], data.get("success_criteria") or [])]

    related_raw: list[Any] = data.get("related_contracts") or []
    related_contracts: list[Path] = []
    for r in related_raw:
        rp = Path(str(r))
        if not rp.is_absolute():
            candidate = path.parent / rp
            # Accept either convention: a path relative to THIS contract's directory
            # (e.g. ``../intake/x.md``) OR a repo-root-relative path (e.g.
            # ``.agentalloy/contracts/intake/x.md``). When the contract-dir join is
            # missing, retry against the repo root (the dir that contains ``.agentalloy``)
            # so neither form is a spurious "related contract not found".
            if not candidate.exists():
                for parent in path.resolve().parents:
                    if (parent / ".agentalloy").is_dir() and (parent / rp).exists():
                        candidate = parent / rp
                        break
            rp = candidate
        related_contracts.append(rp)

    # created_at — optional; fall back to file mtime
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
    if created_at is None:
        try:
            mtime = path.stat().st_mtime
            created_at = datetime.fromtimestamp(mtime, tz=UTC)
        except OSError:
            pass

    route = str(data.get("route") or "full").strip().lower()
    if route not in ("full", "fast", "add-skill"):
        raise ContractMalformed(
            f"Contract 'route' must be 'full', 'fast', or 'add-skill', got '{route}'"
        )

    return Contract(
        path=path.resolve(),
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


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


def safe_contract_path(
    path_str: str,
    project_root: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Validate a user-supplied contract path is contained under ``.agentalloy/contracts/``.

    Returns ``(resolved_path, project_root)`` on success, ``(None, None)`` on failure.
    Resolution failures, missing ``.agentalloy`` ancestor, or paths that escape the
    contracts directory all return ``(None, None)`` — callers should treat that as a
    400 / reject.

    When ``project_root`` is ``None``, the project root is derived from the path itself
    by walking up to the ``.agentalloy`` parent. This is the common case for the API:
    the caller supplies an absolute path and we verify it's a well-formed contract path
    living inside *some* project's ``.agentalloy/contracts/`` tree.
    """
    try:
        resolved = Path(path_str).resolve()
    except OSError:
        return None, None

    if not resolved.is_file():
        return None, None

    # Walk up until we find the .agentalloy parent (the project's agentalloy dir).
    contracts_root: Path | None = None
    for ancestor in resolved.parents:
        if ancestor.name == ".agentalloy":
            contracts_root = ancestor / "contracts"
            break
    if contracts_root is None:
        return None, None

    derived_root = contracts_root.parent.parent  # `.agentalloy/`.parent = project root

    # If caller pinned a project_root, the resolved path must also live under it.
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

    # And the path must live under derived_root/.agentalloy/contracts/
    try:
        resolved.relative_to(contracts_root.resolve())
    except (ValueError, OSError):
        return None, None

    return resolved, derived_root


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_contract(contract: Contract, project_root: Path) -> list[str]:
    """Return a list of issues (empty = valid). Does not raise."""
    issues: list[str] = []

    # Phase match check
    phase_file = project_root / ".agentalloy" / "phase"
    if phase_file.exists():
        try:
            raw_phase: Any = yaml.safe_load(phase_file.read_text(encoding="utf-8")) or {}
            if isinstance(raw_phase, dict):
                phase_data: dict[str, Any] = cast(dict[str, Any], raw_phase)
                active_phase = str(phase_data.get("phase", "")).strip()
            else:
                active_phase = ""
            if active_phase and active_phase != contract.phase:
                issues.append(
                    f"Contract phase '{contract.phase}' does not match active phase "
                    f"'{active_phase}' in .agentalloy/phase"
                )
        except Exception:
            pass

    # Related contracts existence
    for rp in contract.related_contracts:
        if not rp.exists():
            issues.append(f"Related contract not found: {rp}")

    # domain_tags is optional — empty is valid (compose falls back to body-text
    # retrieval). Tags only refine/boost retrieval when present.

    # scope.touches globs valid syntax
    for pattern in contract.scope.touches + contract.scope.avoids:
        try:
            fnmatch.translate(pattern)
        except Exception:
            issues.append(f"Invalid glob pattern in scope: {pattern!r}")

    return issues


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def list_contracts_for_phase(project_root: Path, phase: str) -> list[Path]:
    """Return all .agentalloy/contracts/<phase>/*.md sorted newest-first by mtime."""
    contracts_dir = project_root / ".agentalloy" / "contracts" / phase
    if not contracts_dir.is_dir():
        return []
    files = [f for f in contracts_dir.glob("*.md") if f.is_file()]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def ordered_contracts_for_phase(project_root: Path, phase: str) -> list[Path]:
    """Return contracts/<phase>/*.md in FILENAME order (``01-``, ``02-``, …).

    The design phase numbers build work-items by filename prefix to fix the
    worklist order, so this — not the mtime order of ``list_contracts_for_phase``
    — is the correct sequence for "which task is first/next". The single ordering
    definition shared by the ``task`` cursor commands and phase-entry seeding.
    """
    contracts_dir = project_root / ".agentalloy" / "contracts" / phase
    if not contracts_dir.is_dir():
        return []
    return sorted((f for f in contracts_dir.glob("*.md") if f.is_file()), key=lambda f: f.name)


def first_workitem_id(project_root: Path, phase: str) -> str | None:
    """The first work-item of ``phase`` (filename order) as a cursor id
    (``<phase>/<file>.md``), or ``None`` when the phase has no contracts.

    Used to seed ``.agentalloy/cursor`` on phase entry so the current work-item
    is reliably set without waiting for the agent's first ``agentalloy task next``.
    """
    contracts = ordered_contracts_for_phase(project_root, phase)
    if not contracts:
        return None
    return f"{phase}/{contracts[0].name}"


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


def code_index_query_params(contract: Contract, project_root: Path) -> CodeIndexQuery:
    """Build ``/code/search/*`` query parameters from a contract.

    Derives the repo slug with the same rule the code-index module uses to key
    its indexes (single github.com origin → ``{org}__{repo}``, else the
    slugified directory basename), so the resulting search query resolves
    instead of 404ing. See ``agentalloy.code_index.slug``.
    """
    from agentalloy.code_index.slug import repo_slug

    # The repo slug MUST byte-match the code-index module's own derivation —
    # it keys each index by this string, so any divergence 404s the lookup.
    repo = repo_slug(project_root)

    body = (contract.body or "").strip()
    first_line = body.split("\n")[0].lstrip("# ").strip() if body else ""
    semantic_q = first_line or contract.task_slug

    lexical_q = " ".join(contract.domain_tags) if contract.domain_tags else None
    path_globs = list(contract.scope.touches) if contract.scope and contract.scope.touches else []

    return CodeIndexQuery(
        repo=repo,
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
    digest = hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:16]  # noqa: S324 non-crypto id
    return f"cursor.{digest}"


def _read_cursor_value(project_root: Path, session_key: str | None = None) -> str | None:
    """Read the work-item cursor (a contracts-relative id).

    Reads the session-scoped file first (``cursor.<hash>``), then falls back to the
    shared ``cursor`` — so a session that never scoped, and every pre-scoping repo,
    resolve exactly as before. Mirrors ``skill_loader._read_cursor`` but lives here
    so this low-level module can resolve the current work-item without importing the
    signals/api layers. Returns ``None`` when absent or empty.
    """
    names = [cursor_state_name(session_key)]
    if names[0] != "cursor":
        names.append("cursor")  # shared fallback
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
    contracts-relative posix path (e.g. ``build/01-cache.md``) and ``abs_path`` is
    the file to use. Resolution order:

    1. An explicit ``.agentalloy/cursor`` that resolves to a file under
       ``.agentalloy/contracts/`` (set by phase-entry seeding — see
       ``first_workitem_id`` — or advanced by ``agentalloy task next``).
    2. Exactly one contract in ``contracts/<phase>/`` → that single work-item
       (the common single-item phase: spec/design/qa/ship).
    3. ≥2 with no cursor (an uncursored build fan-out) → ``(None, None)``: don't
       guess. This is the fail-safe floor, not the normal path — the cursor is
       seeded to the first work-item on phase entry (``_write_phase_atomic`` /
       ``run_phase_set``) and advanced by ``task next``, so a live phase almost
       always has a cursor. Both consumers depend on this strictness: the proxy
       composes nothing (rather than a mis-scoped guess) and the
       ``lessons_recorded`` gate fails open (UNKNOWN) rather than block against a
       guessed slug.
    4. Zero contracts → ``(None, None)``.

    The single source of truth is the cursor. This resolver never falls back to
    ``latest_contract`` (newest by mtime): mtime is fragile — git checkout/clone
    reset it — and, shared with a correctness gate, a newest-by-mtime guess could
    satisfy or block the gate against the wrong task.
    """
    contracts_root = (project_root / ".agentalloy" / "contracts").resolve()
    cursor = _read_cursor_value(project_root, session_key)
    if cursor:
        candidate = (contracts_root / cursor).resolve()
        # Containment guard: a stale/hostile cursor must not read outside the tree.
        if candidate.is_file() and candidate.is_relative_to(contracts_root):
            return candidate.relative_to(contracts_root).as_posix(), candidate
        # stale/invalid cursor → fall through to the phase default

    in_phase = list_contracts_for_phase(project_root, phase)
    if len(in_phase) != 1:
        # 0 → nothing current; ≥2 with no cursor → fan-out, don't guess.
        return None, None
    only = in_phase[0].resolve()
    return only.relative_to(contracts_root).as_posix(), only


def latest_contract(project_root: Path, phase: str | None = None) -> Path | None:
    """Most recently modified contract (optionally filtered by phase)."""
    if phase:
        files = list_contracts_for_phase(project_root, phase)
        return files[0] if files else None

    # No phase filter — scan all phases
    contracts_root = project_root / ".agentalloy" / "contracts"
    if not contracts_root.is_dir():
        return None

    all_files: list[Path] = []
    for phase_dir in contracts_root.iterdir():
        if phase_dir.is_dir():
            all_files.extend(f for f in phase_dir.glob("*.md") if f.is_file())

    if not all_files:
        return None

    return max(all_files, key=lambda f: f.stat().st_mtime)
