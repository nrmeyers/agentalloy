"""Per-symbol embed-text composition + content hashing for the ingest pipeline.

Adapts codebase-indexer's ``embed_driver.compose_function_method_embed_text``
header layout: a small ``#``-comment header (kind, qualified name, module)
followed by the docstring and the source range. The composed string is BOTH
the embed input and the incremental-skip fingerprint input, so a symbol
re-embeds exactly when the text the model would see changes.

nomic-embed-text-v1.5 constraints enforced here (the single choke point):

- Documents carry a literal ``search_document: `` prefix (queries — a later
  PR — use ``search_query: ``). Omitting it silently degrades recall.
- The embed server (llama-server, no RoPE scaling) has a HARD 2048-token
  context ceiling. Inputs are truncated CLIENT-SIDE with a conservative
  ~4 chars/token heuristic: the final text (prefix included) never exceeds
  :data:`MAX_EMBED_TEXT_CHARS`, and truncation is recorded in the stored text.
"""

from __future__ import annotations

import hashlib

from agentalloy.code_index.facade import ParsedSymbol

DOCUMENT_PREFIX = "search_document: "
"""nomic-embed-text-v1.5 document-side task prefix (query side: ``search_query: ``)."""

MAX_EMBED_TEXT_CHARS = 6000
"""Hard cap on the final embed input, prefix included. ~1500 tokens at the
conservative 4 chars/token heuristic — well under nomic's 2048-token ceiling."""

TRUNCATION_MARKER = "\n# [truncated]"
"""Appended to a capped text so the stored copy records that truncation happened."""

#: Symbol kinds that get their own vector row. Structural nodes (File / Folder
#: / Package) and Module rows carry no useful embed payload and are skipped.
EMBEDDABLE_KINDS: frozenset[str] = frozenset({"Function", "Method", "Class"})


def is_embeddable(symbol: ParsedSymbol) -> bool:
    """True when ``symbol`` should get a vector row (functions/methods/classes)."""
    return symbol.kind in EMBEDDABLE_KINDS


def finalize_embed_text(text: str) -> str:
    """Apply the document prefix and the client-side length cap to ``text``.

    The returned string — prefix, body, and (when capped) the truncation
    marker — is at most :data:`MAX_EMBED_TEXT_CHARS` characters and is exactly
    what gets embedded AND stored in the vector row's ``text`` column.
    """
    budget = MAX_EMBED_TEXT_CHARS - len(DOCUMENT_PREFIX)
    if len(text) > budget:
        text = text[: budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return DOCUMENT_PREFIX + text


def compose_symbol_embed_text(symbol: ParsedSymbol) -> str:
    """Build the embed input for one symbol (prefixed + capped).

    Layout (mirrors codebase-indexer's function/method pass so raw embed
    inputs read consistently across both systems)::

        search_document: # Function: pkg.mod.helper
        # Module: pkg.mod
        # ---
        <docstring>
        <source range>
    """
    parts = [f"# {symbol.kind}: {symbol.qualified_name}"]
    module_path = ".".join(symbol.qualified_name.split(".")[:-1])
    if module_path:
        parts.append(f"# Module: {module_path}")
    parts.append("# ---")
    docstring = (symbol.docstring or "").strip()
    if docstring:
        parts.append(docstring)
    if symbol.source_code:
        parts.append(symbol.source_code)
    return finalize_embed_text("\n".join(parts))


def content_hash(symbol: ParsedSymbol) -> str:
    """Stable SHA-1 fingerprint over the symbol's identity-bearing fields.

    Drives the incremental-embedding skip: when the stored hash on the graph
    row matches the freshly computed one, the symbol is unchanged since the
    last index and both the graph write and the embed call are skipped.
    SHA-1 is a content fingerprint here, not a cryptographic guarantee.
    """
    fields = (
        symbol.qualified_name,
        symbol.kind,
        symbol.name,
        symbol.file_path or "",
        symbol.docstring or "",
        "|".join(symbol.decorators),
        str(symbol.is_exported),
        str(symbol.is_async),
        str(symbol.is_generator),
        symbol.source_code or "",
    )
    return hashlib.sha1("\x00".join(fields).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    """SHA-1 fingerprint of an arbitrary embed input (markdown chunks)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()
