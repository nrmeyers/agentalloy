# Intake: Entity Extraction Phase 2 — Retrieval Integration

## Context

Phase 1 (PR #619, merged to `feature/entity-extraction`) delivered deterministic entity extraction from markdown docs during code-index ingestion. Five edge types: CONSTRAINTS, TOUCHES, REQUIRES, COMMAND, STAKEHOLDER. Edges stored in the graph, rendered in the knowledge push manifest.

**Problem:** The MVP doesn't create graph-native value. Entities are extracted from the same docs the model can already read. The rendered manifest is a noisier copy of information available elsewhere. The knowledge graph's power — discovery through traversal — is unused.

**Goal:** Make entity edges traversable during retrieval so the model discovers connections it wouldn't find by reading docs alone.

## Scope

### 2A: `knowledge_entities` query action

**What:** New action on `agentalloy_query` that returns all typed entity edges touching a symbol.

**Why:** Gives the model a direct way to explore the entity graph. When it encounters a symbol in context, it can ask "what entities touch this?" and discover constraints, dependencies, and commands that aren't visible from the code alone.

**Implementation:**
- Add `knowledge_entities` to `_QUERY_ACTIONS` in `mcp_server.py`
- New HTTP endpoint: `GET /code/search/entities?fqn=...&kind=...`
- Backend: call `graph.typed_edges_for_fqn(fqn)` + optional kind filter
- Formatter: list of edges with kind, source doc, span preview
- Optional: also search by entity name (not just FQN) — `GET /code/search/entities?name=...`

**Files:**
- `src/agentalloy/install/mcp_server.py` — action + dispatch + formatter
- `src/agentalloy/code_index/api/search_router.py` — new endpoint
- `src/agentalloy/code_index/store/graph_store.py` — may need a `typed_edges_by_name()` method
- `tests/install/cli/test_mcp_server.py` — validation + happy path
- `tests/code_index/test_search_router.py` — endpoint tests

**Acceptance criteria:**
- [ ] `agentalloy_query(action="knowledge_entities", query="src/auth/middleware.py")` returns all entity edges touching that file (FQN path)
- [ ] `agentalloy_query(action="knowledge_entities", query="AuthMiddleware")` resolves via `symbols_by_name()` and returns the same shape (name path)
- [ ] FQN resolution tried first, name fallback only when FQN misses
- [ ] Response includes kind, source doc, destination, span preview
- [ ] Empty result returns clear "no entities found" message (not an error)

### 2B: Entity-aware `related_decisions()`

**What:** Enhance `related_decisions()` to traverse entity edges from top results and surface additional decisions connected via those edges.

**Why:** This is the discovery value. Today, `related_decisions()` does vector search constrained to decision-doc chunks. It finds decisions that *semantically match* the query. But it misses decisions that are *structurally connected* via entity edges. Example: a query about "auth middleware" might not semantically match a decision about "rate limiting" — but if the auth middleware doc has a CONSTRAINTS edge pointing to the rate limiter, the graph traversal would surface it.

**Implementation:**
- After `related_decisions()` returns top-k results, collect the chunk QNs
- Call `typed_edges_from_chunks(chunk_qns)` to get entity edges from those chunks
- For each entity edge destination, check if any GOVERNS decisions point at it
- Merge those decisions into the results (deduped, marked as "entity-connected")
- Cap the expansion (max 3 entity-connected decisions) to prevent noise

**Files:**
- `src/agentalloy/code_index/retrieval/hybrid.py` — enhance `related_decisions()`
- `src/agentalloy/code_index/api/models.py` — add `connected_via` field to `SearchResult` (optional)
- `tests/code_index/test_hybrid.py` — entity expansion tests

**Acceptance criteria:**
- [ ] `related_decisions()` returns semantic matches first (RRF-ranked as today), then entity-connected decisions appended after
- [ ] Entity-connected results carry a `[via <KIND>]` label (e.g. `[via CONSTRAINTS]`)
- [ ] Expansion is capped at 3 entity-connected decisions
- [ ] No regression: existing semantic-only results returned unchanged when no entity edges exist

### 2C: (Deferred) Broader extraction sources

Extract entities from contract bodies, conversation transcripts, memory entries. Deferred — the retrieval integration (2A + 2B) creates value with the existing markdown-doc extraction. Broader sources are a coverage improvement, not a capability improvement.

## Design Decisions

### 1. `knowledge_entities` accepts both FQN and short name via `query`

The `query` param uses two-tier resolution: try exact FQN match first, fall back to `symbols_by_name()` if no match. One parameter, consistent with the existing `query` param pattern across other `agentalloy_query` actions. The model usually has an FQN from context (decision manifest, code search results), but sometimes only has a short name. Both paths return the same result shape.

### 2. Entity-connected decisions ranked after semantic matches, clearly labeled

`related_decisions()` returns two groups: semantic matches first (ranked by RRF score as today), then entity-connected decisions appended with a `[via <KIND>]` tag. Semantic matches directly address the query; entity connections are serendipitous discovery — valuable but tangential. The label tells the model *why* each result is here. No synthetic scoring to interleave them — two focused tools (`related_decisions` + `knowledge_entities`) beat one overloaded scoring mechanism.

### 3. No `kind` filter on `related_decisions()`

Deferred. The `knowledge_entities` action (2A) already lets the model explore entities by kind when it wants to. Adding a `kind` filter to `related_decisions()` complicates the API for marginal benefit — the model can get all related decisions, then use `knowledge_entities` to drill into specific relationship types if needed. Revisit if usage patterns show the model repeatedly requesting kind-filtered results.

## Effort Estimate

- 2A: ~100 lines (new action + endpoint + formatter + tests)
- 2B: ~80 lines (enhance existing function + tests)
- Total: ~180 lines + tests

## Dependencies

- Phase 1 entity extraction (done, on `feature/entity-extraction`)
- `typed_edges_from_chunks()` already exists in graph_store.py (unused)
- `typed_edges_for_fqn()` already exists and is tested

## Risks

- **Graph traversal cost:** `typed_edges_from_chunks()` is a SQL query. If the chunk set is large, this could be slow. Mitigate with the chunk QN limit (already capped at k=8 in `related_decisions()`).
- **Noise:** Entity-connected decisions might be tangential. The cap (max 3) and the "connected via" marking should keep this manageable.
- **Stale edges:** If the code changes but the index hasn't re-run, entity edges may point at deleted symbols. The existing `_hydrate()` fallback in `hybrid.py` handles this for semantic results; entity-connected results need the same treatment.
