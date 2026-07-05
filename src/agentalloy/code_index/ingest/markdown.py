"""Markdown discovery + section chunking for the ingest pipeline.

Slim adaptation of codebase-indexer's ``markdown_indexer`` / the markdown
strategy from ``chunk_strategies``: walk the repo for ``*.md`` files (simple
exclude set — the vendored engine's path rules are not exposed through the
facade), split each on H1/H2 headings, cap oversized sections, and hand the
chunks to the pipeline. This module is pure (filesystem reads only); the
pipeline owns hashing, embedding, and store writes.

Chunks land in the vector store as ``symbol_type="markdown"`` rows with
``qualified_name = f"{relpath}::{anchor}"`` — the ``::`` separator never
appears in code qualified names, which is how the pipeline tells markdown
rows apart from code rows in the stored content-hash map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Directory names whose subtrees are never scanned for markdown.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".agentalloy",
    }
)

# Keep individual chunks well inside the embedder's 2048-token window
# (~4 chars/token heuristic; the embed_text cap is the final backstop).
_MAX_CHUNK_CHARS = 3500

_HEADING_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$")


@dataclass(frozen=True)
class MarkdownChunk:
    """One heading-keyed slice of a markdown file."""

    qualified_name: str  # "{relpath}::{anchor}"
    file_path: str  # repo-relative POSIX path
    heading: str
    body: str
    start_line: int  # 1-indexed inclusive
    end_line: int  # 1-indexed inclusive


def discover_markdown_files(repo_root: Path) -> list[Path]:
    """Return eligible ``*.md`` files under ``repo_root``, sorted (absolute).

    Any file whose repo-relative path contains an :data:`EXCLUDED_DIRS`
    segment is skipped.
    """
    root = repo_root.resolve()
    out: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        out.append(path)
    out.sort()
    return out


def _slugify_heading(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "section"


def _chunk(
    *, rel_path: str, anchor: str, heading: str, body: str, start_line: int, end_line: int
) -> MarkdownChunk:
    return MarkdownChunk(
        qualified_name=f"{rel_path}::{anchor}",
        file_path=rel_path,
        heading=heading,
        body=body,
        start_line=start_line,
        end_line=end_line,
    )


def chunk_markdown(rel_path: str, content: str) -> list[MarkdownChunk]:
    """Split one markdown document into H1/H2-keyed chunks.

    A document with no headings collapses to a single chunk anchored on the
    filename stem; content before the first heading becomes a ``<stem>-preamble``
    chunk. Oversized sections are split into ``{anchor}-partN`` slices of at
    most ``_MAX_CHUNK_CHARS`` characters. Duplicate heading slugs within one
    file get a numeric suffix so qualified names stay unique.
    """
    lines = content.splitlines()
    if not lines:
        return []

    anchors: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            anchors.append((idx, m.group(2).strip()))

    stem = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    chunks: list[MarkdownChunk] = []

    if not anchors:
        body = content.strip()
        if body:
            chunks.extend(
                _split_capped(
                    rel_path=rel_path,
                    anchor=_slugify_heading(stem),
                    heading=stem,
                    body=body,
                    start_line=1,
                    end_line=len(lines),
                )
            )
        return chunks

    if anchors[0][0] > 0:
        preamble = "\n".join(lines[: anchors[0][0]]).strip()
        if preamble:
            chunks.extend(
                _split_capped(
                    rel_path=rel_path,
                    anchor=f"{_slugify_heading(stem)}-preamble",
                    heading=f"{stem} (preamble)",
                    body=preamble,
                    start_line=1,
                    end_line=anchors[0][0],
                )
            )

    seen: dict[str, int] = {}
    for i, (start_idx, heading) in enumerate(anchors):
        end_idx = anchors[i + 1][0] if i + 1 < len(anchors) else len(lines)
        body = "\n".join(lines[start_idx:end_idx]).strip()
        if not body:
            continue
        anchor = _slugify_heading(heading)
        seen[anchor] = seen.get(anchor, 0) + 1
        if seen[anchor] > 1:
            anchor = f"{anchor}-{seen[anchor]}"
        chunks.extend(
            _split_capped(
                rel_path=rel_path,
                anchor=anchor,
                heading=heading,
                body=body,
                start_line=start_idx + 1,
                end_line=end_idx,
            )
        )
    return chunks


def _split_capped(
    *, rel_path: str, anchor: str, heading: str, body: str, start_line: int, end_line: int
) -> list[MarkdownChunk]:
    """One chunk when the body fits; ``-partN`` slices otherwise."""
    if len(body) <= _MAX_CHUNK_CHARS:
        return [
            _chunk(
                rel_path=rel_path,
                anchor=anchor,
                heading=heading,
                body=body,
                start_line=start_line,
                end_line=end_line,
            )
        ]
    out: list[MarkdownChunk] = []
    pos = 0
    part = 0
    while pos < len(body):
        part += 1
        slice_text = body[pos : pos + _MAX_CHUNK_CHARS]
        lines_before = body[:pos].count("\n")
        sl = start_line + lines_before
        out.append(
            _chunk(
                rel_path=rel_path,
                anchor=f"{anchor}-part{part}",
                heading=heading,
                body=slice_text,
                start_line=sl,
                end_line=min(sl + slice_text.count("\n"), end_line),
            )
        )
        pos += _MAX_CHUNK_CHARS
    return out


def collect_markdown_chunks(repo_root: Path) -> list[MarkdownChunk]:
    """Discover, read, and chunk every eligible markdown file (unreadable or
    non-UTF-8-decodable files are skipped, never fatal)."""
    root = repo_root.resolve()
    chunks: list[MarkdownChunk] = []
    for path in discover_markdown_files(root):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.extend(chunk_markdown(path.relative_to(root).as_posix(), content))
    return chunks


def compose_markdown_embed_text(chunk: MarkdownChunk) -> str:
    """Embed-input body for one chunk (the pipeline applies prefix + cap).

    Mirrors the symbol header layout so raw embed inputs read consistently::

        # MarkdownDoc: <qualified_name>
        # File: <rel_path>
        # Heading: <heading>
        # ---
        <body>
    """
    return (
        f"# MarkdownDoc: {chunk.qualified_name}\n"
        f"# File: {chunk.file_path}\n"
        f"# Heading: {chunk.heading}\n"
        "# ---\n"
        f"{chunk.body}"
    )
