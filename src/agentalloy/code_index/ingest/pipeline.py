"""The ingest pipeline: parse → graph → centrality → embed → fts → finalize.

``run_index_job`` is the single orchestration entry point; the ``/code``
routers and the watch manager both funnel through it. It rewrites the ESSENCE
of codebase-indexer's ``routers/index.py`` job body — phase/progress updates,
heartbeats, cancellation checks between phases, incremental content-hash skip
— without the S3/GitHub/actor machinery.

Concurrency model: the job runs as an asyncio task inside the service
process. CPU-bound work (tree-sitter parse, DuckDB/Lance writes) and the
synchronous embed calls all run via ``asyncio.to_thread`` so the event loop
never blocks; graph + vector write phases hold the per-slug write lock so two
in-process jobs for one repo cannot interleave.

Incremental contract: every stored row (code symbol or markdown chunk)
carries a SHA-1 content hash. On a non-force re-run, rows whose hash matches
the stored one are skipped entirely — no graph rewrite, no embed call.
Markdown chunk qualified names contain ``::`` (code ones never do), which is
how the two populations are told apart in the stored hash map.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentalloy.code_index.facade import ParsedEdge, ParsedSymbol, ParseResult, parse_repo
from agentalloy.code_index.ingest.embed_text import (
    compose_symbol_embed_text,
    content_hash,
    finalize_embed_text,
    is_embeddable,
    text_hash,
)
from agentalloy.code_index.ingest.markdown import (
    MarkdownChunk,
    collect_markdown_chunks,
    compose_markdown_embed_text,
)
from agentalloy.code_index.store import (
    CodeIndexJobsStore,
    code_index_paths,
    open_code_index,
    refresh_centrality,
    slug_write_lock,
)
from agentalloy.config import Settings
from agentalloy.embed_provider import EmbedClient
from agentalloy.storage.protocols import (
    CodeEdge,
    CodeGraphStore,
    CodeIndexHandles,
    CodeSymbol,
    CodeVectorRow,
)

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 32
_MARKDOWN_KIND = "MarkdownDoc"

ProgressCallback = Callable[[str, float], None]


class JobCancelled(Exception):
    """Internal control-flow signal: the job's cancel flag was set."""


@dataclass(frozen=True)
class IndexResult:
    """Summary of one ``run_index_job`` run."""

    slug: str
    job_id: str
    status: str  # "done" | "failed" | "cancelled"
    symbols_total: int = 0
    edges_total: int = 0
    symbols_embedded: int = 0
    markdown_embedded: int = 0
    duration_s: float = 0.0
    error: str | None = None


def _to_code_symbol(s: ParsedSymbol, digest: str) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=s.qualified_name,
        kind=s.kind,
        name=s.name,
        file_path=s.file_path,
        start_line=s.start_line,
        end_line=s.end_line,
        docstring=s.docstring,
        decorators=list(s.decorators),
        is_exported=s.is_exported,
        is_async=s.is_async,
        is_generator=s.is_generator,
        source_code=s.source_code,
        content_hash=digest,
    )


def _to_code_edge(e: ParsedEdge) -> CodeEdge:
    return CodeEdge(
        src=e.src,
        dst=e.dst,
        kind=e.kind,
        file_path=e.file_path or "",
        line_start=e.line_start or 0,
        col_start=e.col_start or 0,
        resolved_via=e.resolved_via or "unknown",
        confidence=1.0 if e.confidence is None else e.confidence,
        new_target=e.new_target or "",
    )


def _markdown_symbol(chunk: MarkdownChunk, digest: str) -> CodeSymbol:
    """Markdown chunks ride the symbols table (kind ``MarkdownDoc``) so the
    content-hash incremental skip covers them without extra plumbing."""
    return CodeSymbol(
        qualified_name=chunk.qualified_name,
        kind=_MARKDOWN_KIND,
        name=chunk.heading,
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=chunk.body,
        content_hash=digest,
    )


# --- decision linkage (Knowledge module) ------------------------------------

# Decision sources: lifecycle docs that carry rationale. All docs/**/*.md are
# already ingested as MarkdownDoc chunks; this allow-list selects which chunks
# are decision-bearing. Both live approach.md shapes are covered. docs/ship and
# docs/qa are process/PR narrative, not rationale, so they are excluded by
# default (add here if a repo puts rationale there).
_DECISION_SOURCE_GLOBS: tuple[str, ...] = (
    "docs/solutions/*.md",
    "docs/design/*/approach.md",
    "docs/spec-contracts/*.design/approach.md",
)

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _is_decision_source(doc_path: str) -> bool:
    """True when ``doc_path`` matches a decision-source glob (DK5)."""
    p = PurePosixPath(doc_path)
    return any(p.match(g) for g in _DECISION_SOURCE_GLOBS)


def _is_code_shaped(span: str) -> bool:
    """True when ``span`` looks like a code identifier, not an English word.

    Tier-2 name resolution (DK2) links a fenced span only if it carries a
    namespace/path separator, an internal underscore, or internal CamelCase — so
    a bare dictionary word (``run``/``build``) matching one symbol is rejected as
    a coincidental false positive."""
    if not span or " " in span:
        return False
    if any(sep in span for sep in (".", "::", "/", "_")):
        return True
    return any(c.isupper() for c in span[1:])


def _extract_governed_symbols(body: str, graph: CodeGraphStore) -> set[str]:
    """Code fqns a decision body governs, via fenced-span resolution (DK2).

    Tier 1: a span equal to a non-``MarkdownDoc`` symbol fqn. Tier 2: a
    code-shaped span matching exactly one code symbol's short name. Ambiguous,
    non-code-shaped, or unresolved spans are dropped; the result is always code
    fqns, never a markdown chunk."""
    governed: set[str] = set()
    for raw in _INLINE_CODE_RE.findall(body):
        span = raw.strip()
        if not span:
            continue
        exact = graph.symbol(span)
        if exact is not None:
            if exact.kind != _MARKDOWN_KIND:
                governed.add(span)
            continue
        if _is_code_shaped(span):
            matches = graph.symbols_by_name(span)
            if len(matches) == 1:
                governed.add(matches[0][0])
    return governed


def _index_decisions(
    graph: CodeGraphStore,
    *,
    changed: list[MarkdownChunk],
    removed: list[str],
    chunks: list[MarkdownChunk],
) -> int:
    """Overlay ``GOVERNS`` edges from decision chunks to the code they govern.

    Doc-granular (DK6): every decision-source doc with >=1 chunk in ``changed`` or
    ``removed`` has all its ``GOVERNS`` edges dropped and re-derived over its
    *current* chunks — so an unchanged sibling's links survive a neighbour's
    edit/removal (AC 3). A chunk becomes a decision iff it yields >=1 governed
    symbol. Returns the number of ``GOVERNS`` edges written."""
    affected: set[str] = {c.file_path for c in changed if _is_decision_source(c.file_path)}
    for qn in removed:
        doc = qn.rsplit("::", 1)[0]
        if _is_decision_source(doc):
            affected.add(doc)
    if not affected:
        return 0

    by_doc: dict[str, list[MarkdownChunk]] = {}
    for c in chunks:
        if c.file_path in affected:
            by_doc.setdefault(c.file_path, []).append(c)

    written = 0
    for doc in sorted(affected):
        graph.delete_govern_edges_for_doc(doc)
        edges = [
            CodeEdge(src=c.qualified_name, dst=fqn, kind="GOVERNS", file_path=doc)
            for c in by_doc.get(doc, [])
            for fqn in sorted(_extract_governed_symbols(c.body, graph))
        ]
        if edges:
            graph.upsert_edges(edges)
            written += len(edges)
    return written


def _parse_full(repo_path: Path, cache_dir: Path) -> ParseResult:
    """Full-tree parse: the engine's sidecar hash/stat caches make a re-parse
    return a PARTIAL symbol set (unchanged files are skipped entirely), which
    would corrupt the content-hash diff — every absent symbol would read as
    "removed". Incrementality is owned by the content-hash layer here, so the
    sidecar caches are cleared before each parse."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for sidecar in cache_dir.glob(".cgr-*.json"):
        sidecar.unlink(missing_ok=True)
    return parse_repo(repo_path, cache_dir=cache_dir)


def _git_head_sha(repo_path: Path) -> str | None:
    """``git rev-parse HEAD`` — None for non-git repos or any git failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


async def _embed_batches(
    embed_client: EmbedClient,
    model: str,
    texts: list[str],
    *,
    heartbeat: Callable[[], None],
) -> list[list[float]]:
    """Batch-embed synchronously via ``asyncio.to_thread`` (loop never blocks).

    A batch that the server rejects (typically "input is too large" — a text
    whose token count beat the client-side char cap despite the code-realistic
    ratio) falls back to per-item embedding with progressive halving, so one
    pathological symbol degrades to a shorter embed instead of failing the
    whole index job.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        try:
            vectors.extend(await asyncio.to_thread(embed_client.embed, model=model, texts=batch))
        except Exception:
            for text in batch:
                vectors.append(await _embed_one_with_halving(embed_client, model, text))
        heartbeat()
    return vectors


async def _embed_one_with_halving(
    embed_client: EmbedClient, model: str, text: str, *, min_chars: int = 256
) -> list[float]:
    """Embed one text, halving its length on each server rejection.

    Gives up (re-raising the last error) only when the text is already at
    ``min_chars`` — at that point the failure is the server, not the input.
    """
    attempt = text
    while True:
        try:
            return (await asyncio.to_thread(embed_client.embed, model=model, texts=[attempt]))[0]
        except Exception:
            if len(attempt) <= min_chars:
                raise
            logger.warning("embed rejected a %d-char input; retrying at half length", len(attempt))
            attempt = attempt[: len(attempt) // 2]


async def run_index_job(
    settings: Settings,
    embed_client: EmbedClient,
    jobs: CodeIndexJobsStore,
    *,
    repo_path: Path,
    slug: str,
    force: bool,
    index_markdown: bool = True,
    job_id: str | None = None,
    progress_cb: ProgressCallback | None = None,
) -> IndexResult:
    """Run one full index job for ``repo_path`` under ``slug``.

    When ``job_id`` is None a job row is created here; the routers create the
    row up front (so the 202 response and the duplicate-slug 409 check share
    one snapshot) and pass its id in. Exceptions are recorded on the job and
    NOT re-raised — the returned :class:`IndexResult` carries the outcome.
    Cancellation (``jobs.request_cancel``) is honored between phases.
    """
    started = time.monotonic()
    if job_id is None:
        job_id = jobs.create_job(slug=slug, repo_path=str(repo_path), force_reindex=force).job_id

    def _progress(phase: str, pct: float) -> None:
        jobs.update_progress(job_id, phase=phase, progress_pct=pct)
        if progress_cb is not None:
            progress_cb(phase, pct)

    def _check_cancel() -> None:
        if jobs.is_cancel_requested(job_id):
            raise JobCancelled

    def _result(status: str, **kw: object) -> IndexResult:
        return IndexResult(
            slug=slug,
            job_id=job_id,
            status=status,
            duration_s=time.monotonic() - started,
            **kw,  # type: ignore[arg-type]
        )

    lock = slug_write_lock(slug)
    handles: CodeIndexHandles | None = None
    locked = False
    try:
        # --- parse phase (CPU-bound: worker thread) ---------------------------
        _check_cancel()
        _progress("parse", 5.0)
        paths = code_index_paths(settings, slug)
        parsed = await asyncio.to_thread(_parse_full, repo_path, paths.cache_dir)
        hashes = {s.qualified_name: content_hash(s) for s in parsed.symbols}
        code_symbols = [_to_code_symbol(s, hashes[s.qualified_name]) for s in parsed.symbols]
        code_edges = [_to_code_edge(e) for e in parsed.edges]
        jobs.update_progress(job_id, symbol_count=len(code_symbols), edge_count=len(code_edges))

        # --- graph phase (under the per-slug write lock) ----------------------
        _check_cancel()
        _progress("graph", 20.0)
        await asyncio.to_thread(lock.acquire)
        locked = True
        handles = open_code_index(settings, slug, role="service")
        graph, vectors = handles.graph, handles.vectors

        prior = {} if force else await asyncio.to_thread(graph.content_hashes)
        prior_code = {qn: h for qn, h in prior.items() if "::" not in qn}
        full_rebuild = force or not prior

        removed_code: set[str] = set()
        if full_rebuild:
            await asyncio.to_thread(graph.replace_all, code_symbols, code_edges)
            changed_qns = {s.qualified_name for s in parsed.symbols}
        else:
            changed_qns = {qn for qn, h in hashes.items() if prior_code.get(qn) != h}
            removed_code = set(prior_code) - set(hashes)

            def _incremental_write() -> None:
                removed_files = {
                    sym.file_path
                    for qn in removed_code
                    if (sym := graph.symbol(qn)) is not None and sym.file_path is not None
                }
                changed_files = removed_files | {
                    s.file_path
                    for s in parsed.symbols
                    if s.qualified_name in changed_qns and s.file_path is not None
                }
                if changed_files:
                    files = sorted(changed_files)
                    graph.delete_for_files(files)
                    graph.upsert_symbols(
                        [cs for cs in code_symbols if cs.file_path in changed_files]
                    )
                    graph.upsert_edges([e for e in code_edges if e.file_path in changed_files])
                # Changed symbols without a resolvable file (rare) still land.
                graph.upsert_symbols(
                    [
                        cs
                        for cs in code_symbols
                        if cs.file_path is None and cs.qualified_name in changed_qns
                    ]
                )

            await asyncio.to_thread(_incremental_write)

        # --- centrality phase --------------------------------------------------
        _check_cancel()
        _progress("centrality", 40.0)
        await asyncio.to_thread(refresh_centrality, graph)

        # --- embed phase ---------------------------------------------------------
        _check_cancel()
        _progress("embed", 50.0)
        to_embed = [
            s for s in parsed.symbols if s.qualified_name in changed_qns and is_embeddable(s)
        ]
        texts = [compose_symbol_embed_text(s) for s in to_embed]
        embeddings = await _embed_batches(
            embed_client,
            settings.runtime_embedding_model,
            texts,
            heartbeat=lambda: jobs.touch_heartbeat(job_id),
        )
        now = int(time.time())
        rows = [
            CodeVectorRow(
                qualified_name=s.qualified_name,
                embedding=vec,
                symbol_type=s.kind,
                file_path=s.file_path or "",
                start_line=s.start_line,
                end_line=s.end_line,
                text=text,
                indexed_at=now,
            )
            for s, text, vec in zip(to_embed, texts, embeddings, strict=True)
        ]
        if force:
            await asyncio.to_thread(vectors.bulk_replace, rows)
        else:
            await asyncio.to_thread(vectors.upsert, rows)
            if removed_code:
                await asyncio.to_thread(vectors.delete, sorted(removed_code))
        symbols_embedded = len(rows)
        jobs.update_progress(job_id, embedding_count=symbols_embedded)

        # --- markdown phase (optional) -----------------------------------------
        markdown_embedded = 0
        if index_markdown:
            _check_cancel()
            _progress("markdown", 75.0)
            md = await _index_markdown(
                settings,
                embed_client,
                jobs,
                job_id=job_id,
                repo_path=repo_path,
                handles=handles,
                prior=prior,
                full_rebuild=full_rebuild,
            )
            markdown_embedded = md.embedded
            # decision phase: overlay GOVERNS edges from the just-indexed
            # decision chunks to the code they govern (doc-granular, DK6).
            await asyncio.to_thread(
                _index_decisions,
                graph,
                changed=md.changed,
                removed=md.removed,
                chunks=md.chunks,
            )

        # --- fts phase ------------------------------------------------------------
        _check_cancel()
        _progress("fts", 90.0)
        await asyncio.to_thread(vectors.rebuild_fts_index)

        # --- finalize ----------------------------------------------------------------
        head_sha = await asyncio.to_thread(_git_head_sha, repo_path)
        jobs.upsert_repo(
            slug=slug,
            repo_path=str(repo_path),
            data_dir=str(paths.repo_dir),
            head_sha=head_sha,
        )
        jobs.mark_indexed(slug, head_sha=head_sha)
        jobs.mark_done(
            job_id,
            symbol_count=len(code_symbols),
            edge_count=len(code_edges),
            embedding_count=symbols_embedded + markdown_embedded,
        )
        return _result(
            "done",
            symbols_total=len(code_symbols),
            edges_total=len(code_edges),
            symbols_embedded=symbols_embedded,
            markdown_embedded=markdown_embedded,
        )
    except JobCancelled:
        jobs.mark_failed(job_id, error="cancelled by request", terminal_status="cancelled")
        return _result("cancelled", error="cancelled by request")
    except asyncio.CancelledError:
        # Task-level cancellation (service shutdown): record and propagate so
        # the awaiting task registry unwinds normally.
        jobs.mark_failed(job_id, error="service shutdown", terminal_status="interrupted")
        raise
    except Exception as exc:  # noqa: BLE001 — the job row is the error surface
        logger.exception("index job %s (%s) failed", job_id, slug)
        jobs.mark_failed(job_id, error=f"{type(exc).__name__}: {exc}")
        return _result("failed", error=str(exc))
    finally:
        if handles is not None:
            handles.close()
        if locked:
            lock.release()


@dataclass(frozen=True)
class _MarkdownResult:
    """Outcome of the markdown phase — the embedded count plus the change sets
    the decision phase needs (it acts on the same chunks, no re-walk)."""

    embedded: int
    changed: list[MarkdownChunk]
    removed: list[str]
    chunks: list[MarkdownChunk]


async def _index_markdown(
    settings: Settings,
    embed_client: EmbedClient,
    jobs: CodeIndexJobsStore,
    *,
    job_id: str,
    repo_path: Path,
    handles: CodeIndexHandles,
    prior: dict[str, str],
    full_rebuild: bool,
) -> _MarkdownResult:
    """Embed + store changed markdown chunks; prune removed ones. Returns the
    embedded count and the change sets (for the decision phase)."""
    graph, vectors = handles.graph, handles.vectors
    chunks = await asyncio.to_thread(collect_markdown_chunks, repo_path)
    texts = {c.qualified_name: finalize_embed_text(compose_markdown_embed_text(c)) for c in chunks}
    hashes = {qn: text_hash(t) for qn, t in texts.items()}

    prior_md = {} if full_rebuild else {qn: h for qn, h in prior.items() if "::" in qn}
    changed = [c for c in chunks if prior_md.get(c.qualified_name) != hashes[c.qualified_name]]
    removed = sorted(set(prior_md) - set(hashes))

    embeddings = await _embed_batches(
        embed_client,
        settings.runtime_embedding_model,
        [texts[c.qualified_name] for c in changed],
        heartbeat=lambda: jobs.touch_heartbeat(job_id),
    )

    def _write() -> None:
        if removed:
            removed_files = {
                sym.file_path
                for qn in removed
                if (sym := graph.symbol(qn)) is not None and sym.file_path is not None
            }
            if removed_files:
                graph.delete_for_files(sorted(removed_files))
                # Re-upsert surviving chunks of the pruned files.
                graph.upsert_symbols(
                    [
                        _markdown_symbol(c, hashes[c.qualified_name])
                        for c in chunks
                        if c.file_path in removed_files
                    ]
                )
            vectors.delete(removed)
        graph.upsert_symbols([_markdown_symbol(c, hashes[c.qualified_name]) for c in changed])
        now = int(time.time())
        vectors.upsert(
            [
                CodeVectorRow(
                    qualified_name=c.qualified_name,
                    embedding=vec,
                    symbol_type="markdown",
                    file_path=c.file_path,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    text=texts[c.qualified_name],
                    indexed_at=now,
                )
                for c, vec in zip(changed, embeddings, strict=True)
            ]
        )

    await asyncio.to_thread(_write)
    return _MarkdownResult(embedded=len(changed), changed=changed, removed=removed, chunks=chunks)
