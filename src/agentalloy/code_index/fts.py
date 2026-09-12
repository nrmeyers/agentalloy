"""tantivy BM25 side index for the shared code graph.

The store's ``search_bm25`` is a GQL CONTAINS fallback ("fake" lexical leg);
this is the real one (design §5). Rebuilt wholesale per ingest and swapped
in atomically — the Rust ``lexical.rs`` pattern: build in a sibling tmp dir,
rename over the live dir, drop the old one — so a query never sees a
half-built index.

tantivy-py 0.26 gotchas (verified):
* ``Index(schema, path)`` requires the directory to pre-exist;
* ``index.reload()`` is mandatory after commit/open before searching;
* ``parse_query_lenient`` returns ``(query, errors)``;
* ``searcher.search(...)`` hits are ``(score, DocAddress)`` tuples.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import tantivy

logger = logging.getLogger(__name__)

_TOKENIZER_NAME = "code"
_TEXT_INDEX_OPTION = "position"


@dataclass(frozen=True)
class FtsDoc:
    """One BM25 document: a symbol's searchable text, keyed by qname."""

    qname: str
    text: str


def _build_analyzer() -> tantivy.TextAnalyzer:
    """Word-boundary tokens, lowercased — code tokens survive case/`-`/`_`."""
    return (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.regex(r"\w+"))
        .filter(tantivy.Filter.lowercase())
        .build()
    )


def _build_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("qname", stored=True)
    builder.add_text_field(
        "text", stored=True, tokenizer_name=_TOKENIZER_NAME, index_option=_TEXT_INDEX_OPTION
    )
    return builder.build()


class FtsIndex:
    """BM25 over symbol embed-text; rebuilt wholesale, never patched."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_dir()

    def count(self) -> int:
        index = self._open()
        if index is None:
            return 0
        return index.searcher().num_docs

    def rebuild(self, docs: Iterable[FtsDoc]) -> int:
        """Build a fresh index from ``docs`` and swap it in atomically.

        Returns the number of documents indexed. The old index (if any) is
        removed after the swap succeeds.
        """
        docs = list(docs)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        old = self._path.with_name(self._path.name + ".old")

        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        index = tantivy.Index(_build_schema(), str(tmp))
        index.register_tokenizer(_TOKENIZER_NAME, _build_analyzer())
        writer = index.writer()
        for d in docs:
            doc = tantivy.Document()
            doc.add_text("qname", d.qname)
            doc.add_text("text", d.text or d.qname)
            writer.add_document(doc)
        writer.commit()

        if old.exists():
            shutil.rmtree(old)
        if self._path.exists():
            self._path.rename(old)
        tmp.rename(self._path)
        if old.exists():
            shutil.rmtree(old)
        logger.info("FTS index swapped in place: %d docs -> %s", len(docs), self._path)
        return len(docs)

    def search(self, query: str, *, k: int = 50) -> list[tuple[str, float]]:
        """BM25 top-``k`` as ``(qname, score)``; never raises.

        The FTS leg is best-effort in the hybrid fusion — an unavailable
        side index degrades to the dense leg (or the GQL fallback upstream).
        """
        query = (query or "").strip()
        index = self._open()
        if not query or index is None:
            return []
        try:
            searcher = index.searcher()
            if searcher.num_docs == 0:
                return []
            parsed, errors = index.parse_query_lenient(query, default_field_names=["text"])
            if errors:
                logger.debug("FTS parse skipped %d malformed term(s)", len(errors))
            res = searcher.search(parsed, limit=max(1, int(k)))
            out: list[tuple[str, float]] = []
            for score, addr in res.hits:
                doc = searcher.doc(addr)
                qn = doc.get_first("qname")
                if qn:
                    out.append((qn, float(score)))
            return out
        except Exception:
            logger.debug("FTS search failed", exc_info=True)
            return []

    def _open(self) -> tantivy.Index | None:
        """Open a fresh handle (picks up swaps) or None if not built yet.

        Per-call open is deliberate: ``rebuild`` swaps the directory out
        from under any live handle, and open cost is an mmap of metadata —
        negligible at this service's query rate.
        """
        if not self._path.is_dir():
            return None
        try:
            index = tantivy.Index.open(str(self._path))
            index.register_tokenizer(_TOKENIZER_NAME, _build_analyzer())
            index.reload()
            return index
        except Exception:
            logger.warning("FTS index at %s is unreadable", self._path, exc_info=True)
            return None
