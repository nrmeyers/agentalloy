"""ingest.embed_text — prefix, cap, and content-hash contract."""

from __future__ import annotations

from dataclasses import replace

from agentalloy.code_index.facade import ParsedSymbol
from agentalloy.code_index.ingest.embed_text import (
    DOCUMENT_PREFIX,
    MAX_EMBED_TEXT_CHARS,
    TRUNCATION_MARKER,
    compose_symbol_embed_text,
    content_hash,
    finalize_embed_text,
    is_embeddable,
    text_hash,
)


def make_symbol(**overrides: object) -> ParsedSymbol:
    base = ParsedSymbol(
        qualified_name="demo.pkg.util.helper",
        kind="Function",
        name="helper",
        file_path="pkg/util.py",
        start_line=4,
        end_line=6,
        docstring="Add one to x.",
        decorators=(),
        is_exported=False,
        is_async=False,
        is_generator=False,
        source_code="def helper(x):\n    return x + 1",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_document_prefix_applied() -> None:
    text = compose_symbol_embed_text(make_symbol())
    assert text.startswith(DOCUMENT_PREFIX)
    # The header + payload follow the prefix.
    assert "# Function: demo.pkg.util.helper" in text
    assert "# Module: demo.pkg.util" in text
    assert "Add one to x." in text
    assert "return x + 1" in text


def test_cap_enforced_and_truncation_recorded() -> None:
    huge = make_symbol(source_code="x = 1\n" * 5000)
    text = compose_symbol_embed_text(huge)
    assert len(text) <= MAX_EMBED_TEXT_CHARS
    assert text.endswith(TRUNCATION_MARKER)


def test_small_text_not_truncated() -> None:
    text = finalize_embed_text("short body")
    assert text == DOCUMENT_PREFIX + "short body"
    assert TRUNCATION_MARKER not in text


def test_finalize_never_exceeds_cap() -> None:
    assert len(finalize_embed_text("z" * 100_000)) <= MAX_EMBED_TEXT_CHARS


def test_content_hash_stable() -> None:
    assert content_hash(make_symbol()) == content_hash(make_symbol())


def test_content_hash_changes_on_source_change() -> None:
    a = content_hash(make_symbol())
    b = content_hash(make_symbol(source_code="def helper(x):\n    return x + 2"))
    assert a != b


def test_content_hash_changes_on_docstring_change() -> None:
    a = content_hash(make_symbol())
    b = content_hash(make_symbol(docstring="Add two to x."))
    assert a != b


def test_content_hash_ignores_line_shift() -> None:
    # Pure line-number movement (code added above) must not force a re-embed.
    a = content_hash(make_symbol())
    b = content_hash(make_symbol(start_line=40, end_line=42))
    assert a == b


def test_is_embeddable_kinds() -> None:
    assert is_embeddable(make_symbol())
    assert is_embeddable(make_symbol(kind="Method"))
    assert is_embeddable(make_symbol(kind="Class"))
    assert not is_embeddable(make_symbol(kind="Module"))


def test_text_hash_distinguishes_texts() -> None:
    assert text_hash("a") != text_hash("b")
    assert text_hash("a") == text_hash("a")
