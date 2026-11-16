"""Tests for the Tantivy BM25 sidecar's query handling.

The load-bearing case: task text containing a colon ("...approach: git-
commit-standards?") must not lose hits. Tantivy's query parser treats ':'
as a field separator, so the raw text would parse as ``commit: messages``
→ field query on a nonexistent field → parse failure → silently []. The
fix sanitizes colons to spaces before parsing (the tokenizer splits on
whitespace anyway, so no terms are lost).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.storage.tantivy_bm25 import TantivyBM25Index

PROSE = "Use conventional commit messages: type, scope, subject"

SECOND_PROSE = "Deploy the service with the release workflow"


@pytest.fixture()
def index(tmp_path: Path) -> TantivyBM25Index:
    idx = TantivyBM25Index(tmp_path / "test.bm25")
    idx.upsert(
        fragment_id="frag-1",
        skill_id="skill-a",
        category="git",
        fragment_type="execution",
        prose=PROSE,
    )
    idx.upsert(
        fragment_id="frag-2",
        skill_id="skill-b",
        category="deploy",
        fragment_type="execution",
        prose=SECOND_PROSE,
    )
    idx.commit()
    yield idx
    idx.close()


def test_query_with_colon_returns_hits(index: TantivyBM25Index) -> None:
    hits = index.search("commit: messages")
    assert [h.fragment_id for h in hits] == ["frag-1"]


def test_colon_query_matches_its_sanitized_equivalent(index: TantivyBM25Index) -> None:
    """Sanitization must be lossless: colon form and space form behave the same."""
    colon = index.search("commit: messages")
    plain = index.search("commit messages")
    assert [h.fragment_id for h in colon] == [h.fragment_id for h in plain]
    assert [round(h.score, 6) for h in colon] == [round(h.score, 6) for h in plain]


def test_plain_query_still_works(index: TantivyBM25Index) -> None:
    hits = index.search("conventional commit messages")
    assert [h.fragment_id for h in hits] == ["frag-1"]


def test_filtered_query_with_colon(index: TantivyBM25Index) -> None:
    """Filters (category, deprecated) compose with colon queries."""
    hits = index.search("commit: messages", categories=["git"])
    assert [h.fragment_id for h in hits] == ["frag-1"]
    assert index.search("commit: messages", categories=["deploy"]) == []
    assert index.search("commit: messages", deprecated_skill_ids=["skill-a"]) == []


def test_multiple_colons(index: TantivyBM25Index) -> None:
    assert [h.fragment_id for h in index.search("scope: subject: type")] == ["frag-1"]


def test_empty_query_returns_nothing(index: TantivyBM25Index) -> None:
    assert index.search("") == []
    assert index.search("   ") == []


def test_query_on_other_doc_returns_other_doc(index: TantivyBM25Index) -> None:
    hits = index.search("release workflow: deploy")
    assert [h.fragment_id for h in hits] == ["frag-2"]
