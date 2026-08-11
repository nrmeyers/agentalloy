# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
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
import json
from dataclasses import dataclass
from datetime import datetime
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
    success_criteria: list[str | dict[str, Any]]
    related_contracts: list[str]
    created_at: datetime | None
    body: str
    # Workflow route chosen at intake (for backward compatibility with older
    # tooling). Routing is now determined by the downstream contract's phase
    # (see ``_intake_route_hint`` in skill_loader.py), not this field.
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
            "Contract 'task_slug' field is required and must be a non-empty string",
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
            f"Contract 'route' must be 'full', 'fast', or 'add-skill', got '{route}'",
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


def _normalize_success_criteria(raw: list[Any]) -> list[str | dict[str, Any]]:
    """Normalize success_criteria to list[str | dict[str, Any]] with id/text keys.

    Handles both legacy format (list[str]) and new format (list[dict]).

    Legacy: ["AC-1: feature works", "AC-2: another feature"]
    New: [{"id": "AC-1", "text": "feature works"}, ...]

    Legacy entries are preserved as-is with auto-generated IDs to maintain
    backward compatibility. New entries must have both id and text keys.
    """
    if not raw:
        return []

    normalized: list[str | dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            # New format: already has id/text
            normalized.append(
                {
                    "id": str(item.get("id", "")),
                    "text": str(item.get("text", "")),
                },
            )
        else:
            # Legacy format: string, preserve as-is
            normalized.append({"id": str(item), "text": str(item)})

    return normalized


def parse_ac_headings(markdown: str) -> list[dict[str, Any]]:
    """Extract AC IDs and text from markdown headings.

    Matches ## AC-N: text or ### AC-N: text patterns.
    Returns list of {id: f"AC-{n}", text: stripped_text}.
    """
    import re

    pattern = r"^(?:#{2,3})\s+AC-(\d+)[\s:]+\s*(.+)$"
    results: list[dict[str, Any]] = []
    for line in markdown.split("\n"):
        m = re.match(pattern, line.strip())
        if m:
            results.append({"id": f"AC-{m.group(1)}", "text": m.group(2).strip()})
    return results


def _json_load_list(val: Any) -> list[str]:
    """Parse a JSON string or Python list into a list of strings."""
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return [str(t) for t in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return [str(t) for t in (val or [])]


def _json_load_any(val: Any) -> Any:
    """Parse a JSON string into its Python value, or pass through."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


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

    domain_tags = _json_load_list(row.get("domain_tags"))
    scope_touches = _json_load_list(row.get("scope_touches"))
    scope_avoids = _json_load_list(row.get("scope_avoids"))
    success_criteria_raw = _json_load_any(row.get("success_criteria"))
    if not isinstance(success_criteria_raw, list):
        success_criteria_raw = []

    route = str(row.get("route") or "full").strip().lower()
    if route not in ("full", "fast", "add-skill"):
        raise ContractMalformed(
            f"Contract 'route' must be 'full', 'fast', or 'add-skill', got '{route}'",
        )

    success_criteria = _normalize_success_criteria(success_criteria_raw)

    return Contract(
        contract_id=str(row["contract_id"]),
        phase=str(row["phase"]),
        task_slug=str(row.get("slug", "")),
        domain_tags=domain_tags,
        scope=ContractScope(
            touches=scope_touches,
            avoids=scope_avoids,
        ),
        success_criteria=success_criteria,
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
    files stay in the repo tree where both sides see them.
    """
    if not session_key:
        return "cursor"
    digest = hashlib.sha1(session_key.encode()).hexdigest()[:16]
    return f"cursor.{digest}"
