"""Tests for contract-driven domain retrieval (Phase 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_contract(path: Path, phase: str = "build", domain_tags: list[str] | None = None) -> Path:
    fm: dict[str, Any] = {
        "phase": phase,
        "task_slug": "test-task",
        "domain_tags": domain_tags or ["NestJS", "JWT"],
        "scope": {"touches": [], "avoids": []},
        "success_criteria": [],
        "related_contracts": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.dump(fm)}---\n\nTest task.\n")
    return path


# ---------------------------------------------------------------------------
# retrieve_domain_candidates: BM25 source selection
# ---------------------------------------------------------------------------


def _make_mock_retrieval_env():
    """Return minimal mocks for retrieve_domain_candidates."""
    source = MagicMock()
    source.get_active_fragments.return_value = []
    lm = MagicMock()
    lm.embed.return_value = [[0.1] * 512]
    vector_store = MagicMock()
    vector_store.search_similar.return_value = []
    vector_store.search_bm25.return_value = []
    return source, lm, vector_store


def test_retrieval_uses_contract_tags_as_bm25(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agentalloy.retrieval.domain import retrieve_domain_candidates

    source, lm, vector_store = _make_mock_retrieval_env()
    bm25_calls: list[str] = []

    def capture_bm25(query: str, **kwargs: Any) -> list[Any]:
        bm25_calls.append(query)
        return []

    vector_store.search_bm25.side_effect = capture_bm25

    result = retrieve_domain_candidates(
        source,
        lm,
        vector_store,
        task="add auth middleware",
        phase="build",
        domain_tags=None,
        k=4,
        embedding_model="test-model",
        contract_tags=["NestJS", "JWT validation"],
    )

    assert result.bm25_source == "contract"
    assert len(bm25_calls) == 1
    assert "NestJS" in bm25_calls[0]
    assert "JWT validation" in bm25_calls[0]


def test_retrieval_falls_back_to_rules_when_no_contract():
    from agentalloy.retrieval.domain import retrieve_domain_candidates

    source, lm, vector_store = _make_mock_retrieval_env()
    bm25_calls: list[str] = []

    def capture_bm25(query: str, **kwargs: Any) -> list[Any]:
        bm25_calls.append(query)
        return []

    vector_store.search_bm25.side_effect = capture_bm25

    result = retrieve_domain_candidates(
        source,
        lm,
        vector_store,
        task="add auth middleware to NestJS",
        phase="build",
        domain_tags=None,
        k=4,
        embedding_model="test-model",
    )

    assert result.bm25_source == "rule-extracted"
    assert len(bm25_calls) == 1


def test_retrieval_union_when_env_var_set(monkeypatch: pytest.MonkeyPatch):
    from agentalloy.retrieval.domain import retrieve_domain_candidates

    monkeypatch.setenv("AGENTALLOY_UNION_KEYWORDS", "1")
    source, lm, vector_store = _make_mock_retrieval_env()
    bm25_calls: list[str] = []

    def capture_bm25(query: str, **kwargs: Any) -> list[Any]:
        bm25_calls.append(query)
        return []

    vector_store.search_bm25.side_effect = capture_bm25

    result = retrieve_domain_candidates(
        source,
        lm,
        vector_store,
        task="add auth middleware",
        phase="build",
        domain_tags=None,
        k=4,
        embedding_model="test-model",
        contract_tags=["NestJS"],
    )

    assert result.bm25_source == "union"
    assert len(bm25_calls) == 1
    # Union: should contain both the contract tag and something rule-extracted
    assert "NestJS" in bm25_calls[0]
