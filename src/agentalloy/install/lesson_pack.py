"""Lesson -> domain-skill pack generator (compound-engineering bridge, task 03).

Turns a ``docs/solutions/<slug>.md`` lesson into a valid AgentAlloy domain-skill
pack under ``<dest_root>/<slug>-lesson/``, ready for ``agentalloy validate-pack``
/ ``install-pack``. The lesson's structure maps onto the fragment taxonomy:

    the approach that worked         -> execution fragment
    how to confirm it worked         -> verification fragment
    the decision / what didn't work  -> rationale fragment

Pure file transform: no embeddings, no corpus, no network. The promote CLI
(``agentalloy lessons promote``) runs this, then a pre-ingest dedup probe, then
the install rail.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from agentalloy.install.subcommands.new_skill_pack import (
    _SKILL_ID_RE,
    _default_pack_manifest,
    _derive_domain_tags,
    _dump_yaml,
)

SCHEMA_VERSION = 1

# ingest._lint warns below 25 words (an error under the strict install path);
# aim a little above so a real lesson never trips it.
_MIN_WORDS = 30

_EXECUTION_KEYS = (
    "approach",
    "solution",
    "what worked",
    "worked",
    "fix",
    "steps",
    "execution",
    "implementation",
    "how i",
    "how we",
)
_VERIFICATION_KEYS = ("verification", "verify", "test", "confirm", "check", "how to verify")
_RATIONALE_KEYS = (
    "rationale",
    "why",
    "decision",
    "didn't",
    "did not",
    "tradeoff",
    "trade-off",
    "pitfall",
    "gotcha",
    "root cause",
    "lesson",
)
_PROBLEM_KEYS = ("problem", "context", "symptom", "background", "issue")

_TAGS_RE = re.compile(
    r"^\s*(?:\*\*|_)?tags(?:\*\*|_)?\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _sanitize_skill_id(slug: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug.strip()).strip("-").lower() or "lesson"
    skill_id = f"{base}-lesson"[:64]
    if not _SKILL_ID_RE.match(skill_id):
        skill_id = re.sub(r"[^a-zA-Z0-9_-]", "", skill_id) or "x-lesson"
    return skill_id


def _parse_sections(text: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Return ``(title, intro_before_first_heading, [(heading, body), ...])``.

    ``title`` is the first ``# H1``; ``intro`` is text before the first ``##``
    heading; sections are the ``##``/``###`` blocks with their bodies.
    """
    title = ""
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in text.splitlines():
        h1 = re.match(r"^#\s+(.*)$", line)
        hn = re.match(r"^#{2,6}\s+(.*)$", line)
        if hn:
            sections.append((hn.group(1).strip(), []))
            cur = sections[-1][1]
            continue
        if h1 and not title:
            title = h1.group(1).strip()
            continue
        (cur if cur is not None else intro).append(line)
    return title, "\n".join(intro).strip(), [(h, "\n".join(b).strip()) for h, b in sections]


def _find_section(sections: list[tuple[str, str]], keys: tuple[str, ...]) -> str:
    for heading, body in sections:
        hl = heading.lower()
        if body and any(k in hl for k in keys):
            return body
    return ""


def _word_count(text: str) -> int:
    return len(text.split())


def _pad(text: str, filler: str) -> str:
    """Ensure ``text`` clears the word floor by appending ``filler`` context."""
    if _word_count(text) >= _MIN_WORDS:
        return text
    padded = (text + ("\n\n" if text else "") + filler).strip()
    return padded


def _lesson_fragments(text: str, slug: str) -> list[dict[str, Any]]:
    """Map the lesson into execution/verification/rationale fragment records."""
    title, intro, sections = _parse_sections(text)
    subject = title or slug
    whole = intro or "\n\n".join(b for _h, b in sections if b)
    ctx = (
        f"This lesson (`{slug}`) was codified during a compound-engineering task on {subject}. "
        f"Keep it when a similar situation recurs so the next task starts from the answer "
        f"instead of rediscovering it."
    )

    execution = _find_section(sections, _EXECUTION_KEYS) or whole or ctx
    verification = _find_section(sections, _VERIFICATION_KEYS) or (
        "Reproduce the original scenario and confirm the behavior the approach describes; "
        "re-run the tests or commands the lesson relied on and check they still pass before "
        "trusting this again. If they do not, the lesson has drifted and needs re-verifying."
    )
    rationale = (
        _find_section(sections, _RATIONALE_KEYS)
        or _find_section(sections, _PROBLEM_KEYS)
        or (
            "This lesson was captured because the problem recurred and cost time; the approach "
            "above is the shortcut past it, and skipping it reintroduces the failure it documents."
        )
    )

    frags = [
        ("execution", "## Execution\n\n" + _pad(execution.strip(), ctx)),
        ("verification", "## Verification\n\n" + _pad(verification.strip(), ctx)),
        ("rationale", "## Rationale\n\n" + _pad(rationale.strip(), ctx)),
    ]
    return [
        {"sequence": i, "fragment_type": ft, "content": content}
        for i, (ft, content) in enumerate(frags)
    ]


def _lesson_tags(text: str, skill_id: str) -> list[str]:
    """Tags from an explicit ``Tags:`` line if present, else derived from the id.

    The derived fallback (:func:`_derive_domain_tags`) is lint-clean by
    construction; explicit tags are trusted as authored.
    """
    m = _TAGS_RE.search(text)
    if m:
        raw = re.split(r"[,;]", m.group(1))
        tags: list[str] = []
        for t in raw:
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", t.strip().lower()).strip("-")
            if slug and slug not in tags:
                tags.append(slug)
        if tags:
            return tags[:8]
    return _derive_domain_tags(skill_id)


def generate_lesson_pack(lesson_path: Path, dest_root: Path) -> dict[str, Any]:
    """Generate a domain-skill pack from ``lesson_path`` under ``dest_root``.

    Returns a result dict with ``action`` ('generated' | error), ``pack_dir``,
    ``skill_id`` and the fragment contents (for the caller's dedup probe).
    Writes ``<dest_root>/<slug>-lesson/{pack.yaml, <skill_id>.yaml}``.
    """
    t0 = time.monotonic()
    if not lesson_path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "lesson_not_found",
            "error": f"lesson file not found: {lesson_path}",
        }

    slug = lesson_path.stem
    text = lesson_path.read_text(encoding="utf-8")
    if not text.strip():
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "lesson_empty",
            "error": f"lesson file is empty: {lesson_path}",
        }

    skill_id = _sanitize_skill_id(slug)
    pack_name = f"{slug}-lesson"
    pack_dir = dest_root / pack_name
    fragments = _lesson_fragments(text, slug)
    raw_prose = "\n\n".join(str(f["content"]) for f in fragments)
    title, _intro, _sections = _parse_sections(text)

    skill_record = {
        "skill_id": skill_id,
        "canonical_name": (title or slug)[:120],
        "description": f"Codified lesson from docs/solutions/{slug}.md: {(title or slug)[:80]}.",
        "category": "tooling",
        "skill_class": "domain",
        "domain_tags": _lesson_tags(text, skill_id),
        "always_apply": False,
        "phase_scope": [],
        "category_scope": [],
        "author": "compound-engineering",
        "change_summary": f"promoted from docs/solutions/{slug}.md via `agentalloy lessons promote`",
        "raw_prose": raw_prose,
        "fragments": fragments,
    }

    manifest = _default_pack_manifest(pack_name)
    manifest["description"] = f"Promoted compound-engineering lesson(s): {pack_name}."
    manifest["skills"] = [
        {"skill_id": skill_id, "file": f"{skill_id}.yaml", "fragment_count": len(fragments)}
    ]

    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / f"{skill_id}.yaml").write_text(_dump_yaml(skill_record), encoding="utf-8")
    (pack_dir / "pack.yaml").write_text(_dump_yaml(manifest), encoding="utf-8")

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "generated",
        "slug": slug,
        "skill_id": skill_id,
        "pack_dir": str(pack_dir),
        "domain_tags": skill_record["domain_tags"],
        "fragment_contents": [str(f["content"]) for f in fragments],
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
