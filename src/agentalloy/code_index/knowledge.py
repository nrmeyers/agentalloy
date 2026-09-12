"""Knowledge index: decision docs + SDD lifecycle state, projected into the
code graph.

The code pipeline indexes what the parser sees; this module indexes what the
project *says*:

* **Decision docs** — markdown under the decision-source globs (v1's set plus
  this repo's root ``DESIGN-*.md``). Each doc is chunked and embedded (kind
  ``MarkdownDoc``) and wired to code two ways:

  - *prose*: code-identifiers in inline spans of the body resolve to
    governed symbols (tier 1 = exact FQN, tier 2 = code-shaped identifier
    with a unique same-named symbol) and become ``Governs`` edges — the v1
    DK2 extraction, ported verbatim;
  - *declared*: an optional YAML front matter (``governs`` / ``requires`` /
    ``touches`` / ``constraints``) emits one edge per target from the doc's
    first chunk — the v2 delta, human-authoritative.

  Targets that resolve to nothing are still recorded: the store anchors
  dangling endpoints, so a declared entry leaves a trace
  (``knowledge_entities`` surfaces it as ``resolved: false``) instead of
  being silently dropped.

* **SDD state** — contracts and artifacts from the StateStore, projected once
  per pass under the ``sdd://`` namespace (repo ``""`` — lifecycle-scoped,
  never swept by a repo full-reindex). Contracts carry their ``touches`` as
  declared ``TOUCHES`` edges; artifacts are chunked for search only.

Disk-doc chunk qualified names are repo-namespaced
(``{repo}/{relpath}::{anchor}``) so two repos with the same doc path cannot
collide in the shared graph. Chunk line numbers offset the front matter they
are stripped from.

A per-doc fingerprint in Meta KV (``knowledge:{doc-uri}``) makes a pass
idempotent: unchanged docs are skipped entirely. A changed doc is
re-derived (derive-first, swap-second) and its chunks re-embedded as a
batch — the doc-level gate bounds the cost, and re-embedding the whole doc
avoids stranding unchanged chunks: ``delete_for_files`` tombstones every
chunk of the doc (``indexed_at = NULL``), so a per-chunk gate would drop the
ones whose content hash did not change. If a re-derivation collapses to zero
governance links while the doc still has chunks, the prior ``Governs``
edges are kept and the doc is reported ``suspicious`` (v1 guard).

The pass ends with the whole-index projections that consume knowledge rows:
an FTS rebuild (markdown reaches the BM25 index through the vector rows) and
a PageRank refresh (declared entity edges move centrality mass).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

import yaml

from agentalloy.state_store import StateStore

from .embed_client import EmbedClient, embed_texts
from .fts import FtsDoc, FtsIndex
from .ingest.embed_text import finalize_embed_text, text_hash
from .ingest.markdown import MarkdownChunk, chunk_markdown, compose_markdown_embed_text
from .open import fts_dir
from .protocols import CodeEdge, CodeGraphStore, CodeSymbol, CodeVectorRow
from .store.pagerank import refresh_centrality

logger = logging.getLogger(__name__)

# v1's decision-source globs, plus this repo's root-level design docs.
_DECISION_SOURCE_GLOBS: tuple[str, ...] = (
    "docs/solutions/*.md",
    "docs/design/*/approach.md",
    "docs/spec-contracts/*.design/approach.md",
    "DESIGN-*.md",
)

# Front-matter keys → edge kind. ``governs`` is human-declared governance
# (same rank as a prose exact-FQN link); the rest are typed entity edges.
_DECLARED_EDGE_KEYS: dict[str, str] = {
    "governs": "GOVERNS",
    "requires": "REQUIRES",
    "touches": "TOUCHES",
    "constraints": "CONSTRAINTS",
}

_MARKDOWN_KIND = "MarkdownDoc"
_VECTOR_TYPE = "markdown"

# v1 DK2: only inline backtick spans in the body are extraction candidates.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

Progress = Callable[[str], None]


@dataclass
class KnowledgeReport:
    """Outcome of one knowledge scope (a repo's decision docs, or the SDD
    projection)."""

    scope: str
    docs: int = 0
    chunks: int = 0
    embedded: int = 0
    edges: int = 0
    suspicious: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    embed_available: bool = False


@dataclass
class _DocOutcome:
    """Per-doc work tally accumulated by the doc-level ingest helpers."""

    chunks: int = 0
    embedded: int = 0
    edges: int = 0
    skipped: bool = False
    suspicious: bool = False
    unresolved: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery + front matter
# ---------------------------------------------------------------------------


def _is_decision_source(rel_path: str) -> bool:
    p = PurePosixPath(rel_path)
    return any(p.match(glob) for glob in _DECISION_SOURCE_GLOBS)


def _discover_decision_docs(repo_root: Path) -> list[Path]:
    """Decision-glob-matched markdown under ``repo_root`` (absolute,
    deduped, sorted)."""
    found: list[Path] = []
    for glob in _DECISION_SOURCE_GLOBS:
        for path in repo_root.glob(glob):
            if path.is_file() and path not in found:
                found.append(path)
    found.sort()
    return found


def _split_front_matter(content: str) -> tuple[dict, str, int]:
    """Split a leading ``---`` YAML block: ``(meta, body, consumed_lines)``.

    No leading block, no closing delimiter, invalid YAML, or a non-mapping
    payload all yield ``({}, content, 0)`` — the body is indexed as-is.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content, 0
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                meta = yaml.safe_load("".join(lines[1:i]))
            except yaml.YAMLError:
                return {}, content, 0
            if not isinstance(meta, dict):
                return {}, content, 0
            return meta, "".join(lines[i + 1 :]), i + 1
    return {}, content, 0


# ---------------------------------------------------------------------------
# Chunk + symbol shaping
# ---------------------------------------------------------------------------


def _embed_text(chunk: MarkdownChunk) -> str:
    """The text embedded + indexed for BM25 (compose, then the embed
    prefix + cap)."""
    return finalize_embed_text(compose_markdown_embed_text(chunk))


def _chunk_fingerprint(chunk: MarkdownChunk) -> str:
    return text_hash(_embed_text(chunk))


def _namespaced_chunks(
    rel_path: str, body: str, repo: str, line_offset: int
) -> list[MarkdownChunk]:
    """Chunk a doc body and namespace the qualified names by repo (the
    shared graph would otherwise collide on identical doc paths across
    repos). Line numbers offset the front matter stripped from the body."""
    chunks = chunk_markdown(rel_path, body)
    prefix = f"{repo}/{rel_path}"
    out: list[MarkdownChunk] = []
    for c in chunks:
        anchor = c.qualified_name.split("::", 1)[1]
        out.append(
            replace(
                c,
                qualified_name=f"{prefix}::{anchor}",
                start_line=c.start_line + line_offset,
                end_line=c.end_line + line_offset,
            )
        )
    return out


def _markdown_symbol(chunk: MarkdownChunk, repo: str, file_path: str) -> CodeSymbol:
    return CodeSymbol(
        qualified_name=chunk.qualified_name,
        kind=_MARKDOWN_KIND,
        name=chunk.heading,
        file_path=file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        docstring=None,
        decorators=[],
        is_exported=None,
        is_async=False,
        is_generator=False,
        source_code=chunk.body,
        content_hash=_chunk_fingerprint(chunk),
        repo=repo,
    )


# ---------------------------------------------------------------------------
# Symbol extraction (v1 DK2, ported) + declared-edge resolution
# ---------------------------------------------------------------------------


def _is_code_shaped(span: str) -> bool:
    """Looks like a code identifier: no spaces, and contains a separator or
    starts camel/PascalCase."""
    if not span or " " in span:
        return False
    if any(sep in span for sep in (".", "::", "/", "_")):
        return True
    return any(c.isupper() for c in span[1:])


def _extract_governed_symbols(
    body: str, store: CodeGraphStore
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Governed symbols referenced by ``body`` — v1 DK2, ported verbatim.

    Returns ``(governed, unresolved)`` where ``governed`` is
    ``[(fqn, span, tier), ...]`` (tier 1 = exact FQN, tier 2 = code-shaped
    span with exactly one same-named code symbol) and ``unresolved`` the
    spans that matched several symbols.
    """
    governed: list[tuple[str, str, int]] = []
    unresolved: list[str] = []
    seen_fqns: set[str] = set()

    for raw in _INLINE_CODE_RE.findall(body):
        span = raw.strip()
        if not span:
            continue
        exact = store.symbol(span)
        if exact is not None and exact.qualified_name == span:
            # Tier 1: exact FQN identity (the store suffix-resolves, so the
            # identity check keeps tier 1 exact-only).
            if exact.kind != _MARKDOWN_KIND and span not in seen_fqns:
                governed.append((span, span, 1))
                seen_fqns.add(span)
            continue
        if _is_code_shaped(span):
            matches = store.symbols_by_name(span)
            if len(matches) == 1:
                fqn = matches[0][0]
                if fqn not in seen_fqns:
                    governed.append((fqn, span, 2))
                    seen_fqns.add(fqn)
            elif len(matches) > 1:
                unresolved.append(span)
    return governed, unresolved


def _resolve_target(item: str, store: CodeGraphStore) -> tuple[str, int]:
    """``(qualified_name, tier)`` for a declared front-matter target.

    Tier 1 — the item is a symbol FQN (exact); tier 2 — a FQN suffix the
    store uniquely resolves, or a code-shaped identifier with exactly one
    same-named code symbol. Tier 0 — nothing matched: the raw item is
    returned and the store anchors it as a dangling endpoint (recorded,
    not dropped).
    """
    item = (item or "").strip()
    if not item:
        return "", 0
    sym = store.symbol(item)
    if sym is not None and sym.kind != _MARKDOWN_KIND:
        tier = 1 if sym.qualified_name == item else 2
        return sym.qualified_name, tier
    short = item.rsplit(".", 1)[-1] if "." in item else item
    if _is_code_shaped(short):
        matches = store.symbols_by_name(short)
        if len(matches) == 1:
            return matches[0][0], 2
    return item, 0


def _declared_edges(
    meta: dict,
    first_qn: str,
    file_path: str,
    repo: str,
    store: CodeGraphStore,
) -> tuple[list[CodeEdge], list[CodeEdge]]:
    """``(govern, entity)`` edges from front matter — one edge per doc ×
    target × kind, emitted from the doc's first chunk. ``governs`` items are
    authoritative declarations and rank as tier 1."""
    govern: list[CodeEdge] = []
    entity: list[CodeEdge] = []
    for key, kind in _DECLARED_EDGE_KEYS.items():
        items = meta.get(key)
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, str):
                continue
            item = item.strip()
            if not item:
                continue
            dst, tier = _resolve_target(item, store)
            if kind == "GOVERNS":
                tier = 1
            edge = CodeEdge(
                src=first_qn,
                dst=dst,
                kind=kind,
                file_path=file_path,
                span=item,
                resolution_tier=tier,
                repo=repo,
            )
            (govern if kind == "GOVERNS" else entity).append(edge)
    return govern, entity


# ---------------------------------------------------------------------------
# Fingerprinting + per-doc state
# ---------------------------------------------------------------------------


def _doc_fingerprint(meta: dict, chunks: Sequence[MarkdownChunk]) -> str:
    """sha1 over the front matter + every chunk's embedded text — a
    front-matter-only edit changes it (re-derive edges) and an unchanged
    doc does not (skip the doc entirely)."""
    h = hashlib.sha1()
    h.update(json.dumps(meta, sort_keys=True, default=str).encode("utf-8"))
    for c in chunks:
        h.update(f"{c.qualified_name}:{_chunk_fingerprint(c)}\n".encode())
    return h.hexdigest()


def _doc_qns(store: CodeGraphStore, uri: str) -> set[str]:
    """Chunk qualified names currently held for a doc uri (a disk doc is
    ``{repo}/{relpath}``, an SDD doc its ``sdd://`` uri)."""
    return {qn for qn in store.decision_qns() if qn == uri or qn.startswith(f"{uri}::")}


def _purge_doc(store: CodeGraphStore, uri: str, file_path: str) -> None:
    """Remove every row a doc owned: symbols, entity edges, govern edges,
    vectors, and its fingerprint marker.

    Chunk nodes are hard-deleted BEFORE the file tombstone pass:
    ``delete_for_files`` drops the Symbol label, after which ``delete``
    (which resolves by Symbol label) can no longer find them and they leak
    as dangling anchors.
    """
    qns = _doc_qns(store, uri)
    if qns:
        store.delete(sorted(qns))
    store.delete_for_files([file_path])
    store.delete_govern_edges_for_doc(file_path)
    store.set_meta(f"knowledge:{uri}", "")


def _embed_chunks(
    store: CodeGraphStore,
    chunks: Sequence[MarkdownChunk],
    file_path: str,
    embed_client: EmbedClient,
) -> int:
    """Embed every chunk in one batch and upsert the vector rows (this also
    publishes the texts to the FTS rebuild). Returns the upserted count."""
    vectors = embed_texts(embed_client, [_embed_text(c) for c in chunks])
    rows = [
        CodeVectorRow(
            qualified_name=c.qualified_name,
            embedding=vec,
            symbol_type=_VECTOR_TYPE,
            file_path=file_path,
            start_line=c.start_line,
            end_line=c.end_line,
            text=_embed_text(c),
            indexed_at=int(time.time()),
        )
        for c, vec in zip(chunks, vectors)
    ]
    return store.upsert(rows)


# ---------------------------------------------------------------------------
# Per-doc ingest (disk decision docs)
# ---------------------------------------------------------------------------


def _ingest_doc(
    store: CodeGraphStore,
    repo_name: str,
    repo_root: Path,
    path: Path,
    *,
    embed_client: EmbedClient | None,
    embed_available: bool,
) -> _DocOutcome:
    """Chunk, embed, and wire one decision doc. Derive-first, swap-second.

    A fingerprint-unchanged doc is skipped; a changed doc has its prior
    rows tombstoned and its chunks re-embedded as a batch (see module
    docstring for why the per-chunk gate is deliberately absent).
    """
    out = _DocOutcome()
    rel = path.relative_to(repo_root).as_posix()
    abs_path = path.as_posix()
    uri = f"{repo_name}/{rel}"

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    meta, body, consumed = _split_front_matter(content)
    chunks = _namespaced_chunks(rel, body, repo_name, consumed)
    if not chunks:
        _purge_doc(store, uri, abs_path)
        return out

    fp = _doc_fingerprint(meta, chunks)
    if store.get_meta(f"knowledge:{uri}") == fp:
        out.skipped = True
        out.chunks = len(chunks)
        return out

    # --- derive (before any deletion: resolution needs the live graph) ---
    govern: list[CodeEdge] = []
    for c in chunks:
        gs, unresolved = _extract_governed_symbols(c.body, store)
        out.unresolved.extend(f"{c.qualified_name}::{s}" for s in sorted(set(unresolved)))
        for fqn, span, tier in sorted(gs):
            govern.append(
                CodeEdge(
                    src=c.qualified_name,
                    dst=fqn,
                    kind="GOVERNS",
                    file_path=abs_path,
                    span=span,
                    resolution_tier=tier,
                    repo=repo_name,
                )
            )
    d_govern, entity = _declared_edges(meta, chunks[0].qualified_name, abs_path, repo_name, store)
    govern.extend(d_govern)

    # v1 guard: zero derived links with prior links + a doc that still has
    # chunks is an extraction regression, not a decision retraction.
    prior_govern = store.count_govern_edges_for_doc(abs_path)
    out.suspicious = not govern and prior_govern > 0

    # --- swap ---
    prior_qns = _doc_qns(store, uri)
    vanished = prior_qns - {c.qualified_name for c in chunks}
    # Hard-delete vanished chunks BEFORE the file tombstone pass drops
    # their Symbol label (delete resolves by that label).
    if vanished:
        store.delete(sorted(vanished))
    store.delete_for_files([abs_path])
    store.upsert_symbols([_markdown_symbol(c, repo_name, abs_path) for c in chunks])
    if embed_available and embed_client is not None:
        out.embedded = _embed_chunks(store, chunks, abs_path, embed_client)
    if not out.suspicious:
        store.delete_govern_edges_for_doc(abs_path)
        if govern:
            store.upsert_edges(govern)
            out.edges += len(govern)
    if entity:
        store.upsert_edges(entity)
        out.edges += len(entity)
    if not out.suspicious:
        store.set_meta(f"knowledge:{uri}", fp)
    # Suspicious path: keep the old fingerprint so the next pass re-derives
    # (e.g. the knowledge pass ran before code symbols existed) — stamping
    # here would pin the stale GOVERNS edges until the doc text changes.
    out.chunks = len(chunks)
    return out


def _vanished_doc_rels(store: CodeGraphStore, repo_name: str, live_rels: set[str]) -> list[str]:
    """Prior doc rels for the repo (decision-glob chunk qns) no longer live
    on disk."""
    prefix = f"{repo_name}/"
    prior: set[str] = set()
    for qn in store.decision_qns():
        if not qn.startswith(prefix):
            continue
        rel = qn[len(prefix) :].rsplit("::", 1)[0]
        if _is_decision_source(rel):
            prior.add(rel)
    return sorted(prior - live_rels)


def ingest_repo_knowledge(
    store: CodeGraphStore,
    repo_name: str,
    repo_path: str | Path,
    *,
    embed_client: EmbedClient | None,
    on_progress: Progress | None = None,
) -> KnowledgeReport:
    """Index one repo's decision docs: chunk, embed, wire declared + prose
    edges, and purge docs that vanished from disk."""
    started = time.monotonic()
    repo_root = Path(repo_path).expanduser().resolve()
    report = KnowledgeReport(scope=repo_name)

    def progress(msg: str) -> None:
        logger.info("[knowledge:%s] %s", repo_name, msg)
        if on_progress is not None:
            on_progress(msg)

    if not repo_root.is_dir():
        progress("path missing; skipped")
        return report

    embed_available = False
    if embed_client is not None:
        try:
            embed_available = embed_client.is_available()
        except Exception:
            embed_available = False
    report.embed_available = embed_available

    live: dict[str, Path] = {}
    for path in _discover_decision_docs(repo_root):
        live[path.relative_to(repo_root).as_posix()] = path

    for rel in sorted(live):
        out = _ingest_doc(
            store,
            repo_name,
            repo_root,
            live[rel],
            embed_client=embed_client,
            embed_available=embed_available,
        )
        report.docs += 1
        report.chunks += out.chunks
        report.embedded += out.embedded
        report.edges += out.edges
        if out.suspicious:
            report.suspicious.append(rel)
        report.unresolved.extend(out.unresolved)
        if out.skipped:
            progress(f"{rel}: unchanged")
        else:
            progress(f"{rel}: {out.chunks} chunks, {out.embedded} embedded, {out.edges} edges")

    for rel in _vanished_doc_rels(store, repo_name, set(live)):
        _purge_doc(store, f"{repo_name}/{rel}", (repo_root / rel).as_posix())
        progress(f"{rel}: removed (vanished from disk)")

    report.duration_s = round(time.monotonic() - started, 3)
    return report


# ---------------------------------------------------------------------------
# SDD projection (contracts + artifacts → sdd:// docs)
# ---------------------------------------------------------------------------


def _contract_body(contract: dict) -> str:
    tags = contract.get("domain_tags") or []
    lines = [f"# Contract: {contract['slug']}", ""]
    if tags:
        lines.append(f"Domain tags: {', '.join(str(t) for t in tags)}")
        lines.append("")
    touches = (contract.get("touches") or "").strip()
    if touches:
        lines.append(f"Touches: {touches}")
    return "\n".join(lines)


def _contract_touch_edges(contract: dict, qn: str, store: CodeGraphStore) -> list[CodeEdge]:
    """Declared TOUCHES edges from a contract's free-text touches — one per
    unique resolved target, comma-split."""
    items = [t.strip() for t in (contract.get("touches") or "").split(",") if t.strip()]
    edges: list[CodeEdge] = []
    seen: set[str] = set()
    for item in items:
        dst, tier = _resolve_target(item, store)
        if dst in seen:
            continue
        seen.add(dst)
        edges.append(
            CodeEdge(
                src=qn,
                dst=dst,
                kind="TOUCHES",
                file_path=qn,
                span=item,
                resolution_tier=tier,
                repo="",
            )
        )
    return edges


def _ingest_sdd_doc(
    store: CodeGraphStore,
    uri: str,
    chunks: list[MarkdownChunk],
    entity: list[CodeEdge],
    *,
    embed_client: EmbedClient | None,
    embed_available: bool,
) -> _DocOutcome:
    """Idempotent upsert of one sdd:// doc (contracts and artifacts share
    this: artifacts simply pass no entity edges). ``file_path`` is the uri
    itself, so the doc-scoped deletes in the store work on it."""
    out = _DocOutcome()
    fp = _doc_fingerprint({}, chunks)
    if store.get_meta(f"knowledge:{uri}") == fp:
        out.skipped = True
        out.chunks = len(chunks)
        return out

    prior_qns = _doc_qns(store, uri)
    vanished = prior_qns - {c.qualified_name for c in chunks}
    # Hard-delete vanished chunks BEFORE the tombstone pass drops their
    # Symbol label (delete resolves by that label).
    if vanished:
        store.delete(sorted(vanished))
    store.delete_for_files([uri])
    if chunks:
        store.upsert_symbols([_markdown_symbol(c, "", uri) for c in chunks])
        if embed_available and embed_client is not None:
            out.embedded = _embed_chunks(store, chunks, uri, embed_client)
    if entity:
        store.upsert_edges(entity)
        out.edges += len(entity)
    store.set_meta(f"knowledge:{uri}", fp)
    out.chunks = len(chunks)
    return out


def _vanished_sdd_uris(store: CodeGraphStore, live_uris: set[str], prefix: str) -> list[str]:
    """Prior sdd:// doc uris under ``prefix`` no longer present in state."""
    prior = {qn.rsplit("::", 1)[0] for qn in store.decision_qns() if qn.startswith(prefix)}
    return sorted(prior - live_uris)


def project_sdd(
    store: CodeGraphStore,
    state_store: StateStore,
    *,
    embed_client: EmbedClient | None,
    on_progress: Progress | None = None,
) -> KnowledgeReport:
    """Project contracts + artifacts into the graph under ``sdd://`` (repo
    ``"``"`` — lifecycle-scoped, never swept by a repo full-reindex).

    Contracts are single-chunk docs whose ``touches`` become declared
    ``TOUCHES`` edges (resolve-or-dangle); artifacts are chunked for search
    only. Idempotent and fingerprint-gated per doc; removed contracts and
    artifacts are purged.
    """
    started = time.monotonic()
    report = KnowledgeReport(scope="sdd")

    def progress(msg: str) -> None:
        logger.info("[knowledge:sdd] %s", msg)
        if on_progress is not None:
            on_progress(msg)

    embed_available = False
    if embed_client is not None:
        try:
            embed_available = embed_client.is_available()
        except Exception:
            embed_available = False
    report.embed_available = embed_available

    # --- contracts ---
    live_contract_uris: set[str] = set()
    for c in state_store.list_contracts():
        uri = f"sdd://contracts/{c['slug']}"
        live_contract_uris.add(uri)
        body = _contract_body(c)
        chunk = MarkdownChunk(
            qualified_name=uri,
            file_path=uri,
            heading=f"Contract: {c['slug']}",
            body=body,
            start_line=1,
            end_line=body.count("\n") + 1,
        )
        out = _ingest_sdd_doc(
            store,
            uri,
            [chunk],
            _contract_touch_edges(c, uri, store),
            embed_client=embed_client,
            embed_available=embed_available,
        )
        report.docs += 1
        report.chunks += out.chunks
        report.embedded += out.embedded
        report.edges += out.edges
        progress(f"contracts/{c['slug']}: {'unchanged' if out.skipped else 'projected'}")

    for uri in _vanished_sdd_uris(store, live_contract_uris, "sdd://contracts/"):
        _purge_doc(store, uri, uri)
        progress(f"{uri}: removed (contract gone)")

    # --- artifacts ---
    live_artifact_uris: set[str] = set()
    for a in state_store.list_artifacts():
        uri = f"sdd://artifacts/{a['phase']}/{a['name']}"
        live_artifact_uris.add(uri)
        body = a["body"] or ""
        chunks = chunk_markdown(uri, body) if body.strip() else []
        out = _ingest_sdd_doc(
            store,
            uri,
            chunks,
            [],
            embed_client=embed_client,
            embed_available=embed_available,
        )
        report.docs += 1
        report.chunks += out.chunks
        report.embedded += out.embedded
        report.edges += out.edges
        state = "unchanged" if out.skipped else "projected"
        progress(f"artifacts/{a['phase']}/{a['name']}: {state}")

    for uri in _vanished_sdd_uris(store, live_artifact_uris, "sdd://artifacts/"):
        _purge_doc(store, uri, uri)
        progress(f"{uri}: removed (artifact gone)")

    report.duration_s = round(time.monotonic() - started, 3)
    return report


# ---------------------------------------------------------------------------
# Pass entry point
# ---------------------------------------------------------------------------


def ingest_knowledge(
    store: CodeGraphStore,
    repos: Sequence[tuple[str, Path]],
    state_store: StateStore,
    *,
    embed_client: EmbedClient | None,
    index_dir: str | Path,
    on_progress: Progress | None = None,
) -> list[KnowledgeReport]:
    """One knowledge pass: decision docs per repo + the SDD projection,
    then the whole-index projections (FTS rebuild + PageRank) that consume
    knowledge rows. Called at startup and after /reindex."""
    reports = [
        ingest_repo_knowledge(store, name, path, embed_client=embed_client, on_progress=on_progress)
        for name, path in repos
    ]
    reports.append(
        project_sdd(store, state_store, embed_client=embed_client, on_progress=on_progress)
    )

    fts_count = FtsIndex(fts_dir(index_dir)).rebuild(
        FtsDoc(qn, text) for qn, text in store.fts_docs()
    )
    nodes = refresh_centrality(store)
    store.set_meta("last_knowledge_at", str(int(time.time())))
    logger.info("knowledge pass done: %d fts docs, %d centrality updates", fts_count, nodes)
    if on_progress is not None:
        on_progress(f"knowledge: {fts_count} fts docs, {nodes} centrality updates")
    return reports
