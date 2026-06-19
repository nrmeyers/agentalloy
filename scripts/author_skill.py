#!/usr/bin/env python3
"""Batch authoring helper: reads SKILL.md files, produces pending-review YAML."""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

_TYPE_HINTS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"when to use|prerequisites|requirements|installation|setup|getting started", re.I
        ),
        "setup",
    ),
    (
        re.compile(
            r"core concepts|overview|what is|key features|architecture|purpose|vs |comparison", re.I
        ),
        "rationale",
    ),
    (
        re.compile(
            r"quick start|pattern \d|template \d|implementation|step.by.step|workflow", re.I
        ),
        "execution",
    ),
    (re.compile(r"example|code sample|before.after|demo", re.I), "example"),
    (
        re.compile(
            r"best practice|do.s and don.ts|do.s|don.ts|anti.pattern|avoid|guardrail|security|troubleshoot",
            re.I,
        ),
        "guardrail",
    ),
    (re.compile(r"validation|verify|test|check", re.I), "verification"),
    (
        re.compile(
            r"decision|trade.off|why|principle|method|design principle|dashboard design", re.I
        ),
        "rationale",
    ),
    (re.compile(r"common pattern|advanced pattern|fundamental pattern", re.I), "execution"),
    (re.compile(r"reference|related|next step|debugging|compliance", re.I), "rationale"),
]


def guess_type(heading: str) -> str:
    for pattern, ftype in _TYPE_HINTS:
        if pattern.search(heading):
            return ftype
    return "execution"


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3 :].lstrip("\n")
    return text


def split_fragments(body: str) -> list[dict]:
    parts: list[dict] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in body.splitlines(keepends=True):
        if line.startswith("## ") and current_lines:
            content = "".join(current_lines).strip()
            if content and len(content.split()) >= 20:
                parts.append(
                    {
                        "heading": current_heading,
                        "content": content,
                        "type": guess_type(current_heading),
                    }
                )
            current_heading = line.strip()
            current_lines = [line]
        else:
            if not current_heading and line.startswith("## "):
                current_heading = line.strip()
            current_lines.append(line)
    if current_lines:
        content = "".join(current_lines).strip()
        if content and len(content.split()) >= 20:
            parts.append(
                {
                    "heading": current_heading,
                    "content": content,
                    "type": guess_type(current_heading),
                }
            )
    return parts


def emit_yaml(skill_id, canonical_name, category, tags, raw_prose, fragments, author="navistone"):
    tags_str = ", ".join(tags)
    indented_prose = textwrap.indent(raw_prose, "  ")
    lines = [
        f"skill_id: {skill_id}",
        f"canonical_name: {canonical_name}",
        f"category: {category}",
        "skill_class: domain",
        f"domain_tags: [{tags_str}]",
        "always_apply: false",
        "phase_scope: null",
        "category_scope: null",
        f"author: {author}",
        "change_summary: initial authoring from agents plugin source",
        "raw_prose: |",
        indented_prose,
        "fragments:",
    ]
    for i, frag in enumerate(fragments, 1):
        indented_content = textwrap.indent(frag["content"], "      ")
        lines.append(f"  - sequence: {i}")
        lines.append(f"    fragment_type: {frag['type']}")
        lines.append("    content: |")
        lines.append(indented_content)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_id")
    parser.add_argument("category")
    parser.add_argument("source")
    parser.add_argument("--tags", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--out-dir", default="skill-source/pending-review")
    args = parser.parse_args()
    source_path = Path(args.source)
    if not source_path.exists():
        print(f"Error: {source_path} not found", file=sys.stderr)
        sys.exit(1)
    raw = source_path.read_text()
    body = strip_frontmatter(raw)
    canonical_name = args.name
    if not canonical_name:
        m = re.search(r"^# (.+)$", body, re.MULTILINE)
        canonical_name = m.group(1).strip() if m else args.skill_id.replace("-", " ").title()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    fragments = split_fragments(body)
    yaml_content = emit_yaml(
        skill_id=args.skill_id,
        canonical_name=canonical_name,
        category=args.category,
        tags=tags,
        raw_prose=body,
        fragments=fragments,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.skill_id}.yaml"
    out_path.write_text(yaml_content)
    print(f"Wrote: {out_path}")
    print(f"  canonical_name : {canonical_name}")
    print(f"  skill_id       : {args.skill_id}")
    print(f"  category       : {args.category}")
    print(f"  fragments      : {len(fragments)} ({', '.join(f['type'] for f in fragments)})")


if __name__ == "__main__":
    main()
