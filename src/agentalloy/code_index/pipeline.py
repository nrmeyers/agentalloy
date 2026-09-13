"""Per-repo ingest pipeline for the shared code graph.

One OverGraph DB holds every registered repo (``repo`` property on nodes and
edges); this module turns one repo's tree-sitter parse into graph rows:

**Delta (default)** — the parse engine's per-repo stat/hash sidecars
(``parse-cache/{repo}/.cgr-*.json``) drive change detection:

* files that vanished (sidecar key before parse, absent after) →
  ``delete_for_files``;
* files the parse touched → delete-then-re-add (``delete_for_files`` on their
  absolute paths before upsert), so stale symbols/edges from the old content
  don't survive;
* embeddings are recomputed only for symbols whose ``content_hash`` differs
  from the store (``content_hashes()``), so an unchanged symbol keeps its
  vector.

**Full** (``force_full`` / ``POST /reindex``) — ``delete_for_repo`` + sidecar
wipes + complete parse.

After the graph writes the pipeline rebuilds the tantivy BM25 side index
wholesale from store state (``fts_docs()`` — the vector upsert persists the
embed text as the node's ``text`` property) and refreshes PageRank. The
store is the source of truth; the FTS index and vectors are projections.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .embed_client import EmbedClient, embed_texts
from .facade import ParsedSymbol, parse_repo
from .fts import FtsDoc, FtsIndex
from .ingest.embed_text import compose_symbol_embed_text, content_hash, is_embeddable
from .open import fts_dir, parse_cache_dir
from .protocols import CodeEdge, CodeGraphStore, CodeSymbol, CodeVectorRow
from .store.pagerank import refresh_centrality

logger = logging.getLogger(__name__)

_STAT_CACHE = ".cgr-stat-cache.json"
_HASH_CACHE = ".cgr-hash-cache.json"

Progress = Callable[[str], None]


@dataclass
class IngestReport:
    """Outcome of one repo ingest, for logs and ``/reindex`` responses."""

    repo: str
    mode: str  # "full" | "delta"
    symbols: int
    edges: int
    embedded: int
    deleted: int
    duration_s: float
    embed_available: bool


def _abs(repo_root: Path, rel: str | None) -> str | None:
    """Repo-relative POSIX path -> absolute (the store's canonical form)."""
    if not rel:
        return None
    return (repo_root / rel).as_posix()


def _sidecar_keys(cache_dir: Path) -> set[str]:
    """Repo-relative paths recorded in the stat sidecar (empty if absent)."""
    try:
        data = json.loads((cache_dir / _STAT_CACHE).read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    return {str(k) for k in data}


def _clear_sidecars(cache_dir: Path) -> None:
    for name in (_STAT_CACHE, _HASH_CACHE):
        path = cache_dir / name
        if path.exists():
            path.unlink()


def _hash_cache_map(cache_dir: Path) -> dict[str, str]:
    """Repo-relative path → content SHA from the hash sidecar (empty if absent)."""
    try:
        data = json.loads((cache_dir / _HASH_CACHE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def ingest_repo(
    store: CodeGraphStore,
    repo_name: str,
    repo_path: str | Path,
    *,
    embed_client: EmbedClient | None,
    index_dir: str | Path,
    force_full: bool = False,
    on_progress: Progress | None = None,
) -> IngestReport:
    """Parse ``repo_path`` and sync its symbols/edges/vectors into ``store``.

    ``embed_client=None`` or an unreachable embed server degrades the run to
    lexical-only: graph rows land, vectors are untouched, the report carries
    ``embed_available=False``.
    """
    started = time.monotonic()
    repo_root = Path(repo_path).expanduser().resolve()
    cache_dir = parse_cache_dir(index_dir, repo_name)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def progress(msg: str) -> None:
        logger.info("[%s] %s", repo_name, msg)
        if on_progress is not None:
            on_progress(msg)

    # -- decide full vs delta --------------------------------------------
    keys_before = _sidecar_keys(cache_dir)
    deleted = 0
    if force_full:
        mode = "full"
        progress("full reindex: clearing parse cache")
        _clear_sidecars(cache_dir)
        deleted = store.delete_for_repo(repo_name)
        progress(f"cleared {deleted} rows for repo")
    else:
        mode = "delta"
        if keys_before and store.repo_symbol_count(repo_name) == 0:
            # Warm sidecar but an empty store (fresh DB, moved index dir):
            # the sidecar would hide every file as "unchanged" — force a
            # full parse so the store gets populated.
            progress("warm cache with empty store; forcing full parse")
            _clear_sidecars(cache_dir)
            keys_before = set()

    # -- parse --------------------------------------------------------------
    hashes_before = _hash_cache_map(cache_dir) if not force_full else {}
    progress(f"{mode} parse starting")
    parsed = parse_repo(repo_root, cache_dir=cache_dir)
    progress(f"parsed {len(parsed.symbols)} symbols, {len(parsed.edges)} edges")

    # Embed liveness + PRE-DELETE content hashes. Captured before any row
    # deletion: delete_for_files tombstones the rows the skip-gate compares
    # against, so a later capture would see nothing and re-embed everything.
    # Full mode hard-deletes vectors (delete_for_repo above), so there is
    # nothing to skip against — everything re-embeds.
    embed_available = False
    if embed_client is not None:
        try:
            embed_available = embed_client.is_available()
        except Exception:
            logger.debug("embed liveness probe failed", exc_info=True)
            embed_available = False
    existing = store.content_hashes() if (embed_available and not force_full) else {}

    # -- vanished files (delta only; full already cleared the repo) ---------
    if keys_before:
        vanished = keys_before - _sidecar_keys(cache_dir)
        if vanished:
            progress(f"deleting {len(vanished)} vanished file(s)")
            deleted += store.delete_for_files(
                sorted((repo_root / rel).as_posix() for rel in vanished)
            )

    # -- delete-then-re-add for touched files -------------------------------
    # Touched = files whose sidecar hash changed this run (covers a changed
    # file that now emits ZERO symbols — its stale rows must still go) plus
    # files that emitted symbols (belt and braces).
    hashes_after = _hash_cache_map(cache_dir)
    touched_rel = {rel for rel, sha in hashes_after.items() if hashes_before.get(rel) != sha}
    changed_files = sorted(
        {
            p
            for p in (
                {_abs(repo_root, ps.file_path) for ps in parsed.symbols if ps.file_path}
                | {_abs(repo_root, rel) for rel in touched_rel}
            )
            if p
        }
    )
    if not force_full and changed_files:
        deleted += store.delete_for_files(changed_files)

    # -- convert + upsert ----------------------------------------------------
    symbols = [
        CodeSymbol(
            qualified_name=ps.qualified_name,
            kind=ps.kind,
            name=ps.name,
            file_path=_abs(repo_root, ps.file_path),
            start_line=ps.start_line,
            end_line=ps.end_line,
            docstring=ps.docstring,
            decorators=list(ps.decorators),
            is_exported=ps.is_exported,
            is_async=ps.is_async,
            is_generator=ps.is_generator,
            source_code=ps.source_code,
            content_hash=content_hash(ps),
            repo=repo_name,
        )
        for ps in parsed.symbols
    ]
    edges = [
        CodeEdge(
            src=e.src,
            dst=e.dst,
            kind=e.kind,
            file_path=_abs(repo_root, e.file_path) or "",
            line_start=e.line_start or 0,
            col_start=e.col_start or 0,
            resolved_via=e.resolved_via or "unknown",
            confidence=e.confidence if e.confidence is not None else 1.0,
            new_target=e.new_target or "",
            repo=repo_name,
        )
        for e in parsed.edges
    ]
    store.upsert_symbols(symbols)
    store.upsert_edges(edges)
    progress(f"upserted {len(symbols)} symbols, {len(edges)} edges")

    # -- embed (hash-gated; graceful lexical-only fallback) ------------------
    embedded = 0
    if embed_available and embed_client is not None:
        to_embed: list[tuple[ParsedSymbol, str]] = []
        skipped_unchanged: list[str] = []
        for ps in parsed.symbols:
            if not is_embeddable(ps):
                continue
            if existing.get(ps.qualified_name) == content_hash(ps):
                skipped_unchanged.append(ps.qualified_name)
                continue
            to_embed.append((ps, compose_symbol_embed_text(ps)))
        if skipped_unchanged:
            # The delete-then-re-add above tombstoned their vector marker;
            # the vector itself survived the merge-preserving upsert.
            restored = store.restore_vector_membership(skipped_unchanged)
            progress(f"kept {restored} unchanged vector(s) (no re-embed)")
        if to_embed:
            progress(f"embedding {len(to_embed)} changed symbol(s)")
            vectors = embed_texts(embed_client, [text for _, text in to_embed])
            rows = [
                CodeVectorRow(
                    qualified_name=ps.qualified_name,
                    embedding=vec,
                    symbol_type=ps.kind,
                    file_path=_abs(repo_root, ps.file_path) or "",
                    start_line=ps.start_line,
                    end_line=ps.end_line,
                    text=text,
                    indexed_at=int(time.time()),
                )
                for (ps, text), vec in zip(to_embed, vectors, strict=True)
            ]
            embedded = store.upsert(rows)
            progress(f"embedded {embedded} vector(s)")
    else:
        progress("embedder unavailable; graph rows only (lexical-only)")

    # -- projections: FTS (whole index) + PageRank ----------------------------
    fts = FtsIndex(fts_dir(index_dir))
    fts_count = fts.rebuild(FtsDoc(qn, text) for qn, text in store.fts_docs())
    progress(f"FTS rebuilt: {fts_count} docs")
    nodes_changed = refresh_centrality(store)
    progress(f"PageRank refreshed: {nodes_changed} node scores updated")

    # -- meta stamps -----------------------------------------------------------
    store.set_meta(
        "embed_model",
        embed_client.model if (embed_client is not None and embed_available) else "none",
    )
    store.set_meta("last_ingest_at", str(int(time.time())))

    report = IngestReport(
        repo=repo_name,
        mode=mode,
        symbols=len(symbols),
        edges=len(edges),
        embedded=embedded,
        deleted=deleted,
        duration_s=round(time.monotonic() - started, 3),
        embed_available=embed_available,
    )
    progress(
        f"done: {report.symbols} symbols, {report.edges} edges, "
        f"{embedded} embedded, {deleted} removed, {report.duration_s}s"
    )
    return report


def ingest_all_repos(
    store: CodeGraphStore,
    repos: Sequence[tuple[str, Path]],
    *,
    embed_client: EmbedClient | None,
    index_dir: str | Path,
    force_full: bool = False,
    on_progress: Progress | None = None,
) -> list[IngestReport]:
    """Ingest every registered repo into the shared DB (startup / /reindex).

    ``repos`` is ``(name, path)`` pairs in registry order. A repo whose path
    no longer exists is skipped with a progress note, not fatal — the rest
    of the index stays warm.
    """
    reports: list[IngestReport] = []
    for name, path in repos:
        if not Path(path).is_dir():
            logger.warning("repo %s path missing; skipping: %s", name, path)
            if on_progress is not None:
                on_progress(f"{name}: path missing, skipped")
            continue
        try:
            reports.append(
                ingest_repo(
                    store,
                    name,
                    path,
                    embed_client=embed_client,
                    index_dir=index_dir,
                    force_full=force_full,
                    on_progress=on_progress,
                )
            )
        except Exception:
            logger.exception("ingest failed for repo %s; continuing with others", name)
            if on_progress is not None:
                on_progress(f"{name}: ingest failed, see logs")
    return reports
