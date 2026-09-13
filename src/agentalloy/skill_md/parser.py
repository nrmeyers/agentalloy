"""Markdown parser for the atomic system skill bootstrap format.

Expected input::

    # Canonical Skill Name

    **skill_id:** sys-xxx
    **category:** governance
    **always_apply:** true
    **phase_scope:** design,build
    **category_scope:**
    **author:** name
    **change_summary:** description

    Raw prose content...

Rules
-----
- The first H1 (``# ...``) becomes ``canonical_name``.
- Lines matching ``**key:** value`` (leading/trailing whitespace stripped) are
  parsed as metadata fields until the first line that breaks the pattern (blank
  line between heading and fields is allowed; the block ends at the first
  non-field, non-blank line after fields have started).
- Everything after the metadata block is ``raw_prose`` (stripped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FIELD_RE = re.compile(r"^\*\*([^*:]+?):\*\*\s*(.*)")


@dataclass
class ParsedSystemSkill:
    canonical_name: str
    skill_id: str
    category: str
    always_apply: bool
    phase_scope: list[str]
    category_scope: list[str]
    author: str
    change_summary: str
    raw_prose: str
    deprecated: bool = False
    superseded_by: str = ""
    # Carry raw fields for error reporting / future extension
    extra_fields: dict[str, str] = field(default_factory=lambda: {})


class ParseError(ValueError):
    pass


def parse_file(path: Path) -> ParsedSystemSkill:
    return parse_text(path.read_text(encoding="utf-8"), source=str(path))


def parse_text(text: str, *, source: str = "<string>") -> ParsedSystemSkill:
    lines = text.splitlines()

    # --- H1 heading ---
    canonical_name: str | None = None
    heading_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            canonical_name = stripped[2:].strip()
            heading_idx = i
            break

    if canonical_name is None:
        raise ParseError(f"{source}: no H1 heading found")

    # --- field block ---
    raw_fields: dict[str, str] = {}
    field_started = False
    prose_start = heading_idx + 1

    for i in range(heading_idx + 1, len(lines)):
        line = lines[i]
        m = _FIELD_RE.match(line.strip())
        if m:
            field_started = True
            raw_fields[m.group(1).strip().lower()] = m.group(2).strip()
            prose_start = i + 1
        elif line.strip() == "":
            # blank lines are allowed within the field block
            continue
        elif field_started:
            # first non-blank, non-field line after fields started = end of block
            prose_start = i
            break

    raw_prose = "\n".join(lines[prose_start:]).strip()

    # --- extract known fields ---
    def _require(key: str) -> str:
        if key not in raw_fields:
            raise ParseError(f"{source}: missing required field '**{key}:**'")
        return raw_fields.pop(key)

    def _optional(key: str, default: str = "") -> str:
        return raw_fields.pop(key, default)

    def _parse_bool(key: str, value: str) -> bool:
        v = value.lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0", ""):
            return False
        raise ParseError(f"{source}: field '**{key}:**' must be a boolean, got '{value}'")

    def _parse_list(value: str) -> list[str]:
        if not value.strip():
            return []
        return [s.strip() for s in value.split(",") if s.strip()]

    skill_id = _require("skill_id")
    category = _require("category")
    always_apply = _parse_bool("always_apply", _optional("always_apply", "false"))
    phase_scope = _parse_list(_optional("phase_scope"))
    category_scope = _parse_list(_optional("category_scope"))
    author = _optional("author", "bootstrap")
    change_summary = _optional("change_summary", "bootstrap load")
    deprecated = _parse_bool("deprecated", _optional("deprecated", "false"))
    superseded_by = _optional("superseded_by", "")

    # remaining fields treated as extra (not an error — forward-compatible)
    extra = dict(raw_fields)

    return ParsedSystemSkill(
        canonical_name=canonical_name,
        skill_id=skill_id,
        category=category,
        always_apply=always_apply,
        phase_scope=phase_scope,
        category_scope=category_scope,
        author=author,
        change_summary=change_summary,
        raw_prose=raw_prose,
        deprecated=deprecated,
        superseded_by=superseded_by,
        extra_fields=extra,
    )
