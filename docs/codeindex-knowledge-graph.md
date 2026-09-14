# Design — Graph-native code index + knowledge index (AgentAlloy v2)

**Date:** 2026-09-07
**Supersedes:** the graph/knowledge portions of `HANDOFF-v2-work.md` (the 4-point
contract is kept as the delivery checklist; this doc changes *how* contract points 1+2
land: Python OverGraph layer, not a Rust `graph_query`).
**Status:** implemented and verified (P0–P3). Full cutover off the Rust
DataLayer complete; the `rust/` crate stays in-tree as a retired, unwired
reference; the `V2_RUST_CORE` A/B flag was removed at P3.

## 1. What we're building

One graph-native store — **OverGraph 0.17** (Rust engine, Python bindings, GQL +
HNSW vectors) — becomes the single backbone for both:

- **Code index**: symbols (function/method/class units with docstrings, not 40-line
  windows), typed relationships (CALLS / IMPORTS / CONTAINS / REFERENCES with
  confidence), dense vectors, centrality.
- **Knowledge index**: decision documents (repo markdown + SDD lifecycle data) as
  graph nodes, with typed edges to the code they govern or touch.

Deliverables through the frozen HTTP surface (TheForge lights up via
`/status` capabilities + `POST /tool` — zero TheForge changes):

| Tool | Serves | Capability |
|---|---|---|
| `code_search` (upgraded) | hybrid retrieval: dense + centrality + real BM25, RRF | existing |
| `symbols` (upgraded) | FQN lookup | existing |
| `graph_query` (new) | neighborhood expansion → `nodes` + `relationships` | flips `graph: true` |
| `knowledge_why` / `knowledge_related` / `knowledge_entities` (de-stubbed) | decision lookup by FQN / by query / typed entity edges | flips `knowledge: true` |

## 2. Why Python, not Rust

- The valuable ~8k lines (v1 engine: per-language cross-reference resolution + edge
  confidence; OverGraph store; hybrid retrieval; knowledge queries) exist and are
  battle-tested in Python. Port = re-vendor; Rust = rewrite of the exact code where
  rewrites quietly lose recall.
- Ingest cost is dominated by embedding I/O (:48951), not parse speed. At this scale
  Python tree-sitter + OverGraph is comfortably adequate.
- The knowledge layer (front-matter contract, SDD projection, GQL shapes) needs fast
  iteration; GQL reads like sentences in Python.
- The Rust crate already declares `overgraph = "0.17"` (TODO in `validate.rs`). If
  perf ever bites at bigger scale, the right Rust move is porting *this* graph schema
  onto the `overgraph` crate — not resurrecting the old flat in-memory index as a
  parallel fallback (second source of truth, divergent answers).

**Rust DataLayer fate:** full cutover — completed at P3. The crate (`rust/`)
stays in-tree as a retired, unwired reference. The `V2_RUST_CORE=1` flag and
every live `_datalayer` wiring (service startup/capabilities/status/reindex,
the executor A/B branches, the CLI index/ask/offline-build paths) were
removed: it delivered inferior recall to the graph index, was a second
source of truth, and left a silent no-index footgun when the flag was off.
Rust's role is the engine tier only — OverGraph (GQL + HNSW) and the tantivy
FTS side index.

## 3. Improvements over v1 (mechanics we're deliberately changing)

| v1 mechanic | v2 replacement | Why |
|---|---|---|
| "BM25" leg = OverGraph `search_bm25` (GQL LIKE substring count) | **real tantivy FTS** (tantivy-py side index) | v1's lexical leg was fake; exact identifiers are exactly what embeddings miss |
| Entity edges mined by prose regexes (incl. "Alice"/"Bob" stakeholders) | **Declared edges**: YAML front-matter contract on decision docs (`governs` / `requires` / `touches` / `constraints`) + **SDD contract touch points** from state.duck | Deterministic, zero false positives, LLM-writable, testable. Prose FQN mentions still create GOVERNS edges (that part worked). |
| Per-repo OverGraph DBs (`repos/{slug}/{path_key}/`) | **Single shared graph**, `repo` property on every node | v2 already unifies repos (registry `repos.json`); cross-repo relationships (TheForge → AgentAlloy API calls) become visible. Reindex = `delete_for_repo` + re-upsert. |
| Job store / S3 / watchdog | **Synchronous ingest** (per the frozen `POST /reindex` contract) | v2 surface is sync; `jobs` capability stays false until a job model is actually wanted |
| Decisions from disk globs + artifact store | **Repo decision markdown** (approach.md / solutions / spec-contracts globs, v1's set) **+ SDD lifecycle projection** (contracts with `touches`, phase artifacts) | The workflow's own decisions become searchable knowledge — v1 didn't have the live-lifecycle side |

**Kept from v1 (proven):** vendored tree-sitter engine + `facade.py`; OverGraph store
wrapper (incl. its GQL escaping workarounds); embed-text composition (nomic
`search_document:`/`search_query:` prefixes, 4200-char client cap, progressive
halving on 500); content-hash incremental skip; hybrid fusion
(0.7·cosine + 0.3·normalized PageRank → RRF K=60 with the lexical leg); markdown
chunking; pure-Python PageRank (α=0.85) persisted as node `centrality`.

## 4. Data model (OverGraph)

- `Symbol` nodes: `qualified_name, name, kind, file_path, start_line, end_line,
  docstring, repo, content_hash, centrality, is_exported, is_async, decorators,
  source_code (truncated)`, dense vector (nomic 768-d; **dimension is fixed at DB
  creation** — see risks).
- `MarkdownDoc` nodes: decision-doc chunks riding the same node type (v1 semantics,
  `::` in qualified name), plus SDD projections (`source: sdd-contract | sdd-artifact`).
- Code edges: `CALLS, IMPORTS, CONTAINS, REFERENCES` (+ `resolved_via, confidence,
  file_path, line_start`).
- Knowledge edges: `GOVERNS` (decision → symbol, declared or prose-mentioned),
  `REQUIRES, TOUCHES, CONSTRAINTS` (decision → symbol/path, declared or from SDD
  contract touch points).
- Meta KV: counts by kind/repo, `indexed_at` per repo.

## 5. Retrieval

**`code_search {query, k}`** → rows `{file, line, content, score, symbol}` + additive
`{kind, end_line, repo, centrality, qualified_name}`:
1. query rewrite (stopwords) → embed (`search_query:` prefix, model-aware)
2. dense leg: HNSW top-50 → centrality fusion `0.7·cos + 0.3·norm(pagerank)`
3. lexical leg: tantivy BM25 over symbol text + decision text
4. RRF K=60 → hydrate docstring-first snippet from the graph

**`symbols {fqn}`** → rows `{fqn, file, line, kind}` + additive `{end_line, docstring,
repo}` (substring-match semantics preserved).

**`graph_query {query, limit=20, hops=1, repo?}`** → `{nodes, relationships, row_count,
has_more}`:
- empty `query` → top-centrality symbols (graph-explorer overview)
- else hybrid seed (top ~k) → GQL neighborhood expansion (N hops)
- node rows carry `id, qname, file, kind, repo, centrality, hopDistance`;
  relationship rows carry `source, target, type, confidence, file`
  (shape matches TheForge `StructuralSearchResponse` / `searchGraph` mapping).

**Knowledge** (response key names frozen by the handoff — `decision` / `decisions` /
`entities`):
- `knowledge_why {fqn}` → `{decision: {...}|null, fqn}` — best GOVERNS source
  (title, path, excerpt)
- `knowledge_related {query, k}` → `{decisions: [...], query}` — hybrid over
  MarkdownDoc nodes + entity-edge expansion (v1 `related_decisions` concept)
- `knowledge_entities {fqn}` → `{entities: [...], fqn}` — typed entity edges
  touching the fqn, both directions, target-resolved

## 6. Front-matter edge contract (decision docs)

```yaml
---
title: Upstream hot-swap
governs: [newagent.server.hot_swap, src/newagent/usage_tracker.py]
requires: [agentalloy.proxy]
touches: [scripts/v2-bringup.sh]
constraints: [token counter must stay monotonic]
---
```

Declared edges are authoritative. Prose FQN mentions additionally create GOVERNS
edges (high precision — an exact FQN in prose is a strong signal). No other prose
mining. Unknown FQN/path targets are recorded but not dropped (surfaced by
`knowledge_entities` as unresolved).

## 7. Service wiring

- `server.py` startup: open/create `{index_dir}/codegraph/codegraph.overgraph`;
  repos from registry (`repos.json`); ingest any repo missing/stale
  (content-hash delta). The retired Rust DataLayer is no longer initialized
  (P3 removal).
- `executors.py`: `_code_search` / `_symbols` delegate to the graph index (Python
  text-walk stays as last-resort fallback). New `graph_query`; real knowledge
  executors. Interpreter tool schema gains `graph_query` (knowledge tools already
  have entries).
- `_capabilities()`: `graph` probes the graph index (liveness/`graph_query`),
  `knowledge` explicit `True` once P2 lands.
- `/status`: `symbols` / `chunks` from the graph index (keys unchanged).
- `/reindex`: repo re-ingest (delete_for_repo + upsert), synchronous, 200 both
  outcomes, envelope unchanged.
- `pyproject.toml`: + `overgraph>=0.17`, + `tree-sitter` + grammar wheels
  (python/javascript/typescript/rust), + `tantivy>=0.26`.

**Byte-compat (frozen, per handoff §3):** `/health`, `/status` (api_version "2.0", no
`repos` key), `/tool` envelope (`result` is a JSON string), `/reindex` semantics,
`code_search` rows `{file, line, content, score, symbol}` (additive only),
`symbols` rows `{fqn, file, line, kind}` (key `symbols`).

## 8. Phases

- **P0 — re-vendor:** deps; engine + store + embed_text into `src/newagent/code_index/`
  (imports adapted; watch/jobs/S3 dropped); smoke: parse both repos, counts sane.
- **P1 — code index on OverGraph:** ingest pipeline (symbols+edges+vectors+FTS+
  PageRank, incremental); hybrid retrieval; `code_search`/`symbols` cutover;
  `graph_query` tool; `graph` capability flip; `/reindex` + `/status` wiring;
  `V2_RUST_CORE=1` A/B flag.
- **P2 — knowledge index:** decision-doc chunking (repo globs) + SDD lifecycle
  projection (contracts + artifacts from state.duck) + front-matter contract +
  entity edges; three real knowledge executors; `knowledge` flip.
- **P3 — verification + hardening (done):** pytest suite — four new suites
  (`test_code_index_store` 12, `test_code_index_retrieval` 8,
  `test_code_index_knowledge` 7, `test_http_byte_compat` 8) plus the re-pointed
  e2e/server suites; 231 green. Bringup: knowledge index 42 docs / 89 chunks /
  67 edges; graph index 22142 symbols / 9702 chunks (hybrid); `/reindex`
  backfill → 22255 symbols / 11597 chunks. TheForge `pnpm check:indexer` 6/6.
  **A/B flag decision: removed.** `V2_RUST_CORE=1` and all live `_datalayer`
  wiring are gone; the `rust/` crate stays in-tree as a retired, unwired
  reference (rationale in §2).

## 9. Risks

- **Embed model / vector dimension:** DB binds 768-d at creation. Runtime model is
  nomic-embed-text-v1.5 per bringup; `config.embed_model` defaults to a different
  name — prefix handling is model-aware and the ingest records the model in Meta;
  a model swap with a different dimension requires a reindex (documented, checked).
- **GQL quirks:** OverGraph's GQL has workarounds baked into v1's store; we port the
  store rather than re-derive them.
- **qname collisions across repos:** qnames stay repo-root-relative (v1 semantics);
  `repo` property disambiguates; `graph_query`/search accept an optional `repo`
  filter (ignored by TheForge today, additive).
- **First ingest is slow** (all symbols embedded once): one-time per repo; the
  content-hash skip makes subsequent starts/reindexes delta-only.
