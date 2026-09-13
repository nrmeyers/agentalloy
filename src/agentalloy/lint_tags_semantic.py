"""Semantic tag linting — data-plumbing only (Phase B).

Builds the prompt block appended to the QA critic's user prompt and provides
a defensive parser for the critic's JSON-array response.  No LLM calls are
made from this module; that happens in Phase C.
"""

from __future__ import annotations

import json


def build_semantic_lint_block(tags: list[str], canonical_name: str, raw_prose: str) -> str:
    """Return a markdown string to append to the QA critic's user prompt."""
    tags_str = ", ".join(tags) if tags else "(none)"
    excerpt = raw_prose[:800]
    ellipsis = "..." if len(raw_prose) > 800 else ""

    return (
        "## Tag Quality Check\n\n"
        "Please evaluate the following tags for Rules 1, 3-syn, and 4.\n\n"
        f"**Skill:** {canonical_name}\n\n"
        f"**Tags to evaluate:** {tags_str}\n\n"
        "**Skill content (excerpt):**\n"
        f"{excerpt}{ellipsis}\n\n"
        "For each tag, respond with a JSON array of verdicts:\n"
        "[\n"
        '  {"tag": "<tag>", "rule": "R1|R3-syn|R4", '
        '"verdict": "pass|not_queryable|synonym_of:<other>|off_intent", '
        '"detail": "<explanation>"}\n'
        "]\n"
        "Only emit verdicts for tags that FAIL. If all tags pass, return []."
    )


def parse_semantic_verdicts(raw: str) -> list[dict[str, str]]:
    """Defensively parse an LLM response containing a JSON verdict array.

    Tolerates:
    - Extra text before/after the JSON array, including stray ``[``/``]`` in
      that prose — the first ``[`` that begins a valid JSON list wins
    - Truncated/partial JSON (returns [] if no complete list is found)
    - Non-list top-level values
    - Verdict dicts missing fields (kept if they have at minimum a "tag" key)
    """
    if not raw:
        return []

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "[":
            continue
        try:
            result, _ = decoder.raw_decode(raw[idx:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict) and "tag" in item]  # type: ignore[return-value]
    return []
