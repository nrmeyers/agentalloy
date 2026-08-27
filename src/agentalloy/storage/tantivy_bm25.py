"""Tantivy-based BM25 full-text search sidecar for OverGraph.

Provides proper BM25 keyword search over fragment prose, complementing
OverGraph's HNSW dense vector search for hybrid retrieval.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Sequence

import tantivy

from agentalloy.storage.protocols import BM25Hit

logger = logging.getLogger(__name__)


class TantivyBM25Index:
    """Tantivy-backed BM25 full-text search index.

    Stores fragment metadata (fragment_id, skill_id, category, fragment_type,
    phase_scope, domain_tags) alongside the prose text for BM25 scoring and
    filtered search.
    """

    def __init__(self, index_path: str | Path) -> None:
        self._path = str(index_path)
        self._schema = self._build_schema()
        Path(self._path).mkdir(parents=True, exist_ok=True)
        if (Path(self._path) / "meta.json").exists():
            self._index = tantivy.Index(self._schema, path=self._path)
        else:
            self._index = tantivy.Index(self._schema, path=self._path)
        self._writer = self._index.writer()
        self._index.reload()

    @staticmethod
    def _build_schema() -> tantivy.Schema:
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("fragment_id", stored=True)
        sb.add_text_field("skill_id", stored=True)
        sb.add_text_field("category", stored=True)
        sb.add_text_field("fragment_type", stored=True)
        sb.add_text_field("prose", stored=True)
        sb.add_text_field("phase_scope", stored=True)
        sb.add_text_field("domain_tags", stored=True)
        return sb.build()

    def upsert(
        self,
        fragment_id: str,
        skill_id: str,
        category: str,
        fragment_type: str,
        prose: str,
        phase_scope: Sequence[str] | None = None,
        domain_tags: Sequence[str] | None = None,
    ) -> None:
        """Insert or replace a document in the BM25 index."""
        doc = tantivy.Document()
        doc.add_text("fragment_id", fragment_id)
        doc.add_text("skill_id", skill_id)
        doc.add_text("category", category or "")
        doc.add_text("fragment_type", fragment_type or "")
        doc.add_text("prose", prose or "")
        doc.add_text("phase_scope", " ".join(phase_scope) if phase_scope else "")
        doc.add_text("domain_tags", " ".join(domain_tags) if domain_tags else "")
        # Delete existing doc with same fragment_id, then add
        self._writer.delete_documents("fragment_id", fragment_id)
        self._writer.add_document(doc)

    def commit(self) -> None:
        """Flush pending writes to the index."""
        self._writer.commit()
        del self._writer  # Release the lock
        self._index.reload()
        self._writer = self._index.writer()

    def search(
        self,
        query: str,
        *,
        categories: list[str] | None = None,
        deprecated_skill_ids: list[str] | None = None,
        domain_tags: list[str] | None = None,
        k: int = 10,
    ) -> list[BM25Hit]:
        """BM25 keyword search with optional filters."""
        if not query or not query.strip():
            return []

        searcher = self._index.searcher()

        # Build the text query
        try:
            text_query = self._index.parse_query(query.strip(), default_field_names=["prose"])
        except Exception:
            logger.debug("Tantivy query parse failed for %r", query, exc_info=True)
            return []

        # Build filter clauses
        must_clauses: list[tuple[tantivy.Occur, tantivy.Query]] = [
            (tantivy.Occur.Must, text_query),
        ]

        if categories:
            cat_queries = []
            for cat in categories:
                cat_queries.append(
                    tantivy.Query.term_query(self._index.schema, "category", cat)
                )
            if len(cat_queries) == 1:
                must_clauses.append((tantivy.Occur.Must, cat_queries[0]))
            else:
                should_clause = tantivy.Query.boolean_query(
                    tuple((tantivy.Occur.Should, q) for q in cat_queries)
                )
                must_clauses.append((tantivy.Occur.Must, should_clause))

        if domain_tags:
            tag_queries = []
            for tag in domain_tags:
                tag_queries.append(
                    tantivy.Query.term_query(self._index.schema, "domain_tags", tag)
                )
            if len(tag_queries) == 1:
                must_clauses.append((tantivy.Occur.Must, tag_queries[0]))
            else:
                should_clause = tantivy.Query.boolean_query(
                    tuple((tantivy.Occur.Should, q) for q in tag_queries)
                )
                must_clauses.append((tantivy.Occur.Must, should_clause))

        combined = tantivy.Query.boolean_query(tuple(must_clauses))
        deprecated_set = set(deprecated_skill_ids or [])

        results: list[BM25Hit] = []
        try:
            search_result = searcher.search(combined, limit=k * 3)
            for score, doc_addr in search_result.hits:
                doc = searcher.doc(doc_addr)
                frag_id = doc["fragment_id"][0] if doc["fragment_id"] else ""
                skill_id = doc["skill_id"][0] if doc["skill_id"] else ""
                if skill_id in deprecated_set:
                    continue
                results.append(BM25Hit(fragment_id=frag_id, score=float(score)))
                if len(results) >= k:
                    break
        except Exception:
            logger.debug("Tantivy search failed", exc_info=True)

        return results

    def delete_all(self) -> None:
        """Delete all documents from the index."""
        self._writer.delete_all_documents()
        self._writer.commit()
        del self._writer
        self._index.reload()
        self._writer = self._index.writer()

    def delete_skill(self, skill_id: str) -> int:
        """Delete all documents for a skill."""
        q = tantivy.Query.term_query(self._index.schema, "skill_id", skill_id)
        searcher = self._index.searcher()
        search_result = searcher.search(q, limit=10000)
        count = search_result.count
        for _, doc_addr in search_result.hits:
            doc = searcher.doc(doc_addr)
            frag_id = doc["fragment_id"][0] if doc["fragment_id"] else ""
            if frag_id:
                self._writer.delete_documents("fragment_id", frag_id)
        self._writer.commit()
        del self._writer
        self._index.reload()
        self._writer = self._index.writer()
        return count

    def count(self) -> int:
        """Return the number of documents in the index."""
        searcher = self._index.searcher()
        q = tantivy.Query.all_query()
        return searcher.search(q, limit=1).count

    def close(self) -> None:
        """Close the index."""
        try:
            self._writer.commit()
        except Exception:
            pass
