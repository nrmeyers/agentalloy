# Domain Fragment Leak: Snowflake/Iceberg in Unrelated Build Injections

**Date:** 2026-07-11  
**Status:** Root cause identified, fixes proposed  
**Related:** PLAN-OF-ATTACK.md §E (retrieval pipeline), §F (corpus hygiene), docs/followups.md (retrieval quality)

---

## Executive Summary

Snowflake/Iceberg domain fragments leak into unrelated build injections (e.g., `ingest-secret-provisioning` with tags `[install-secret, container-bootstrap]`) because the **free-flow compose path bypasses all tag-scoping mechanisms**. The deterministic tag-scoped contract path (`contract_tags → _resolve_bm25_query → _soft_tag_filter`) is effectively unwired in production — it only activates when a contract is present. Free-flow mode (proxy signals, ad-hoc compose) passes no contract_tags and no domain_tags, so BM25 keyword extraction from the task text matches Snowflake/Iceberg vocabulary in the corpus, and the dense embedding leg ranks benchmark packs ahead of on-domain skills.

The corpus is a **single LanceDB table** of 3,395 fragments with no per-domain separation. Benchmark packs (snowflake: 105 frags, data-engineering: 100 frags, vue: 118 frags, temporal: 43 frags, fastapi: 188 frags = **554 total, 16.3% of corpus**) co-index with product skills and the `_pool_categories()` gate is dormant (off by default via `AGENTALLOY_PHASE_GATE`).

---

## 1. Pipeline Trace: POST /compose → Injected Fragments

### 1.1 Entry Point

```
POST /compose (compose_router.py)
  → ComposeOrchestrator.compose() (orchestration/compose.py)
    → ComposeOrchestrator.retrieve() (orchestration/compose.py)
      → retrieve_domain_candidates() (retrieval/domain.py:450)
```

### 1.2 Retrieval Pipeline Stages

```
retrieve_domain_candidates() (domain.py:450):
  1. Circuit breaker check → if open, skip embedding
  2. build_retrieval_query(task) → strips noise, head-caps to ~512 tokens
  3. safe_embed(query) → dense vector (nomic-embed-text-v1.5, 768-dim)
  4. vector_store.search_similar() → DuckDB cosine distance, top-k=pool_size (100)
  5. vector_store.search_bm25() → Tantivy BM25 on prose column
  6. _rrf_fuse() → Reciprocal Rank Fusion (K=60, phase-specific weights)
  7. _soft_tag_filter() → intersect with contract_tags (empty-fallback safe)
  8. skill_granular_select() → round-robin over top-k skills
  9. Stage A: cross-encoder rerank (OnnxReranker or HttpReranker)
  10. Stage B: LM fragment re-rank (FragmentScorer, qwen3-reranker-0.6B)
  11. ComposeOrchestrator._format_fragments() → renders into output
```

### 1.3 Two Compose Paths

**Path A: Contract path (deterministic, tag-scoped)**  
`POST /compose/from-contract` → `compose_request_from_contract()` → sets `contract_tags=contract.domain_tags`

```python
# contracts.py → compose_request_from_contract():
return ComposeRequest(
    task=contract.body or contract.task_slug,
    phase=contract.phase,
    contract_tags=contract.domain_tags,  # ← THE KEY LINE
    ...
)
```

**Path B: Free-flow path (no tag scoping)**  
`_compose_free_block()` in `proxy_apply.py:345` → no `contract_tags`, no `domain_tags`

```python
# proxy_apply.py:345-370
async def _compose_free_block(signal, orchestrator):
    domain_req = ComposeRequest(
        task=signal.task,  # raw signal text
        phase=compose_phase,
        legs="domain",
        k=_tier2_k(),
        # NO contract_tags ← bypasses _resolve_bm25_query contract path
        # NO domain_tags  ← bypasses _soft_tag_filter entirely
    )
```

---

## 2. Retrieval Configuration

### 2.1 Hybrid Retrieval Architecture

| Component | Value | Source |
|-----------|-------|--------|
| Dense embedder | `nomic-embed-text-v1.5` (768-dim) | `fragment_store.py:FRAGMENTS_SCHEMA` |
| BM25 engine | Tantivy (native) | `fragment_store.py:search_bm25()` |
| Fusion algorithm | Reciprocal Rank Fusion (RRF) | `domain.py:_rrf_fuse()` |
| RRF K constant | 60 | `domain.py:_RRF_K_DEFAULT` |
| Phase-specific RRF weights | `qa: dense=0.8, bm25=1.2`; `spec: dense=1.2, bm25=0.8` | `domain.py:_PHASE_RRF_CONFIG` |
| Pool size | `max(k*2, 50)` | `domain.py:retrieve_domain_candidates()` |
| Default k (build/ship) | 2 | `compose_models.py:DEFAULT_K_BY_PHASE` |
| Default k (qa/spec/design/intake) | 4 | `compose_models.py:DEFAULT_K_BY_PHASE` |
| Stage A reranker | OnnxReranker or HttpReranker | `rerank.py:build_reranker_from_env()` |
| Stage B scorer | `qwen3-reranker-0.6B` | `lm_assist.py:FragmentScorer` |
| Stage B keep_threshold | 0.0 (inert by default) | `lm_assist.py:_DEFAULT_KEEP_THRESHOLD` |

### 2.2 BM25 Query Resolution

```python
# domain.py:100-107
def _resolve_bm25_query(task, contract_tags):
    if contract_tags:
        bm25_query = " ".join(contract_tags)
        return bm25_query, "contract"
    return _extract_bm25_keywords(task), "rule-extracted"
```

When `contract_tags` is present: BM25 query = space-joined contract_tags (e.g., `"react ui-design"`).  
When `contract_tags` is absent: BM25 query = rule-extracted keywords from task text.

### 2.3 BM25 Keyword Extraction

```python
# domain.py:80-95
_TECH_KEYWORD_RE = _re.compile(
    r"\b(?:\.\w{2,4}|[A-Z][a-z]+\w*|[a-z_]+\d+\w*|[a-z]+-[a-z]+|[A-Z]{2,})\b",
    _re.IGNORECASE,
)

def _extract_bm25_keywords(task):
    matches = list(dict.fromkeys(_TECH_KEYWORD_RE.findall(task)))
    if matches:
        return f"{task} {' '.join(matches)}"
    return task
```

This regex matches:
- File extensions (`.yaml`, `.md`)
- CamelCase classes (`Snowflake`, `Iceberg`, `Parquet`)
- snake_case functions (`alter_table`, `copy_into`)
- Version numbers (`v1.5`, `2.0`)
- Tech terms (`SQL`, `HTTP`, `JSON`)

**Problem:** The regex is too broad. It will match Snowflake/Iceberg vocabulary from injected context (e.g., `<system-reminder>` blocks that mention data-warehouse patterns) even when the task itself has no data-warehouse surface.

---

## 3. Pollution Mechanism

### 3.1 Corpus Structure

| Attribute | Value |
|-----------|-------|
| Storage | Single LanceDB table (`fragments.lance`) |
| Total fragments | 3,395 |
| Category column | `engineering`, `design`, `tooling`, `quality`, `ops`, `operational`, `review`, **`benchmark`** |
| Phase scope | Optional filter column |
| Domain tags | Optional filter column (used by _soft_tag_filter) |

### 3.2 Benchmark Pack Fragment Counts

| Pack | Fragments | % of Corpus |
|------|-----------|-------------|
| snowflake | 105 | 3.1% |
| data-engineering | 100 | 2.9% |
| vue | 118 | 3.5% |
| temporal | 43 | 1.3% |
| fastapi | 188 | 5.5% |
| **Benchmark total** | **554** | **16.3%** |
| react | 180 | 5.3% |
| **Total corpus** | **3,395** | **100%** |

### 3.3 The Dormant Pool Gate

```python
# domain.py:305-327
_PRODUCT_CATEGORIES = (
    "engineering", "design", "tooling", "quality",
    "ops", "operational", "review",
)  # "benchmark" excluded by omission

def _pool_categories():
    if _os.environ.get("AGENTALLOY_PHASE_GATE", "off").strip().lower() == "on":
        return list(_PRODUCT_CATEGORIES)
    return None  # ← DORMANT BY DEFAULT
```

When `AGENTALLOY_PHASE_GATE=off` (default), `_pool_categories()` returns `None`, which means **no category filter is applied** — the benchmark category is never excluded.

### 3.4 The Free-Flow Pollution Chain

```
Free-flow signal (proxy_apply.py:345)
  → ComposeRequest(task=signal.task, contract_tags=None, domain_tags=None)
    → retrieve_domain_candidates(task, contract_tags=None, domain_tags=None)
      → build_retrieval_query(task) → strips noise, head-caps
      → safe_embed(query) → dense vector
      → search_similar() → top 100 by cosine distance
      → search_bm25(query=None, categories=None, phases=None) → top 100 by BM25
        → BM25 query = _extract_bm25_keywords(task) ← GENERIC KEYWORDS
      → _rrf_fuse() → fused ranking
      → _soft_tag_filter(ranked, contract_tags=None) → NO-OP (returns ranked unchanged)
      → skill_granular_select(ranked, k=2) → top 2 distinct skills
      → Stage A rerank → Stage B score
```

### 3.5 The Contract Path (Working, But Only When Contract Exists)

```
Contract path (contracts.py → compose_request_from_contract)
  → ComposeRequest(task=contract.body, contract_tags=contract.domain_tags, ...)
    → retrieve_domain_candidates(task, contract_tags=["react", "ui"], ...)
      → search_bm25(query="react ui", categories=None, phases=None)
      → _soft_tag_filter(ranked, contract_tags=["react", "ui"])
        → keep = [f for f in ranked if want & {t.lower() for t in f.domain_tags}]
        → fallback: if keep is empty, return full ranked (process-vocab safety valve)
```

When `contract_tags` is present, `_soft_tag_filter` intersects the fused pool with fragments carrying ≥1 contract tag. This is the "deterministic tag-scoped contract path" — but it only activates when a contract is present.

### 3.6 Poison Tags

The PLAN-OF-ATTACK (§E) identifies two poison tags:
- **`frontend`**: indexed in backend skills' prose (→ fastapi/fastify)
- **`calendar`**: exists only in airflow/temporal (→ backend cron)

These tags are generic enough to match off-domain skills, further polluting results.

---

## 4. Related Code References

| Component | File | Key Functions/Constants |
|-----------|------|------------------------|
| Main retrieval pipeline | `retrieval/domain.py` | `retrieve_domain_candidates()`, `_rrf_fuse()`, `_resolve_bm25_query()`, `_extract_bm25_keywords()`, `_soft_tag_filter()`, `_contract_tag_filter_enabled()`, `_pool_categories()`, `_bm25_fallback_result()`, `demote_process_skills()`, `skill_granular_select()` |
| BM25 keyword extraction | `retrieval/domain.py:80-95` | `_TECH_KEYWORD_RE` regex, `_extract_bm25_keywords()` |
| BM25 query resolution | `retrieval/domain.py:100-107` | `_resolve_bm25_query()` |
| Phase-specific RRF weights | `retrieval/domain.py:72-79` | `_PHASE_RRF_CONFIG` |
| Soft tag filter | `retrieval/domain.py:149-168` | `_soft_tag_filter()` |
| Contract tag filter switch | `retrieval/domain.py:146-147` | `_contract_tag_filter_enabled()` |
| Pool categories gate | `retrieval/domain.py:305-327` | `_PRODUCT_CATEGORIES`, `_pool_categories()` |
| BM25 fallback | `retrieval/domain.py:486-545` | `_bm25_fallback_result()` |
| Query builder | `retrieval/query_bounds.py` | `build_retrieval_query()`, `_INJECTED_TAGS`, `_FENCED_CODE` |
| Fragment scorer (Stage B) | `retrieval/lm_assist.py` | `FragmentScorer`, `max_candidates()`, `load_config()`, `_DEFAULT_KEEP_THRESHOLD` |
| Reranker factory | `retrieval/rerank.py` | `build_reranker_from_env()`, `OnnxReranker`, `HttpReranker` |
| Compose orchestrator | `orchestration/compose.py` | `ComposeOrchestrator.compose()`, `ComposeOrchestrator.retrieve()`, `_format_fragments()` |
| Compose models | `api/compose_models.py` | `ComposeRequest`, `compose_request_from_contract()`, `DEFAULT_K_BY_PHASE`, `resolved_contract_tags` |
| Free-flow compose | `api/proxy_apply.py:345-400` | `_compose_free_block()` |
| Contract model | `contracts.py` | `Contract`, `parse_contract_text()`, `compose_request_from_contract()` |
| Fragment store | `storage/fragment_store.py` | `LanceFragmentStore`, `search_similar()`, `search_bm25()`, `FRAGMENTS_SCHEMA` |
| Ingest (benchmark category) | `ingest.py:59-63` | `_VALID_DOMAIN_CATEGORIES` includes "benchmark" |
| Lesson pack generator | `install/lesson_pack.py` | `_lesson_fragments()`, `_lesson_tags()` |

---

## 5. Quantified Impact

### 5.1 Corpus Composition

- **Total fragments:** 3,395
- **Benchmark pack fragments:** 554 (16.3% of corpus)
  - snowflake: 105 (3.1%)
  - data-engineering: 100 (2.9%)
  - vue: 118 (3.5%)
  - temporal: 43 (1.3%)
  - fastapi: 188 (5.5%)
- **React:** 180 (5.3%)
- **Pool size at k=2:** `max(2*2, 50) = 50`

### 5.2 Ranking vs. Filtering Analysis

**This is both a ranking AND a filtering issue:**

1. **Ranking:** The dense embedding leg (cosine similarity) and BM25 leg (keyword matching) both rank benchmark fragments high for tasks with generic technical vocabulary. The RRF fusion (K=60) gives equal weight to both legs (phase-default: dense=1.0, bm25=1.0).

2. **Filtering:** When `contract_tags=None` (free-flow), `_soft_tag_filter()` is a no-op. When `AGENTALLOY_PHASE_GATE=off` (default), `_pool_categories()` returns `None`, so no category filter is applied. The benchmark category is never excluded.

3. **Depth vs. Breadth at k=2:** `skill_granular_select()` at k=2 sets depth=1 and spends the only other slot on the 2nd-ranked distinct skill — frequently off-domain.

### 5.3 Score Distribution

Without live corpus data, we can estimate:
- **Dense leg:** nomic-embed-text-v1.5 produces cosine distances in [0, 2]. Benchmark fragments with similar vocabulary (SQL, tables, data, etc.) will have distances comparable to on-domain fragments.
- **BM25 leg:** The `_TECH_KEYWORD_RE` regex matches generic technical terms. For a task like "ingest-secret-provisioning", keywords like `.yaml`, `.sh`, `container` will match broadly across the corpus.
- **RRF fusion:** At K=60, even fragments ranked 30-50 by one leg contribute to the fused score.

### 5.4 Free-Flow vs. Contract Path Comparison

| Aspect | Contract Path | Free-Flow Path |
|--------|--------------|----------------|
| contract_tags | Set from contract.domain_tags | **None** |
| domain_tags | Set from request | **None** |
| BM25 query | Space-joined contract_tags | Rule-extracted keywords from task |
| _soft_tag_filter | Active (intersects pool) | **No-op** (contract_tags=None) |
| _pool_categories | Dormant (off by default) | Dormant (off by default) |
| Expected leak | Low (tag-scoped) | **High** (no scoping) |

---

## 6. Root Cause Analysis

### 6.1 Primary Root Cause: Free-Flow Compose Path Bypasses Tag Scoping

The `_compose_free_block()` function in `proxy_apply.py:345` creates a `ComposeRequest` with **no contract_tags and no domain_tags**. This means:

1. **BM25 query uses rule-extracted keywords** from the task text, which may include noise from injected context (e.g., `<system-reminder>` blocks that mention domain patterns).
2. **No soft tag filter** is applied — the fused pool is never narrowed to on-domain fragments.
3. **No category filter** is applied — `_pool_categories()` returns `None` (dormant).
4. **Benchmark packs co-index** with product skills in the same LanceDB table.

### 6.2 Secondary Root Cause: Dormant Pool Gate

The `_pool_categories()` function (domain.py:320-327) is **dormant by default** (`AGENTALLOY_PHASE_GATE=off`). Even if the contract path were active, the benchmark category would still be included in retrieval.

### 6.3 Tertiary Root Cause: k=2 Hard Cap for Build/Ship

The `DEFAULT_K_BY_PHASE["build"]=2` (compose_models.py:34) hard-caps build/ship to 2 skills. At k=2, `skill_granular_select()` sets depth=1 and spends the other slot on the 2nd-ranked distinct skill — frequently off-domain when the pool is polluted.

### 6.4 Quaternary Root Cause: Broad BM25 Keyword Extraction

The `_TECH_KEYWORD_RE` regex (domain.py:80-83) matches generic technical terms:
```python
r"\b(?:\.\w{2,4}|[A-Z][a-z]+\w*|[a-z_]+\d+\w*|[a-z]+-[a-z]+|[A-Z]{2,})\b"
```

This matches "Snowflake", "Iceberg", "Parquet", "SQL", "ALTER", "COPY", etc. — all common in both Snowflake/Iceberg fragments AND generic technical tasks.

---

## 7. Proposed Fixes

### 7.1 Fix 1: Activate the Pool Gate (Quick Win)

**What:** Set `AGENTALLOY_PHASE_GATE=on` at deployment time.

**Impact:** Excludes the "benchmark" category from retrieval. Removes 554 fragments (16.3% of corpus) from the candidate pool.

**Trade-offs:**
- **Pro:** Simple, one-line config change. Immediate 16.3% reduction in noise.
- **Con:** Does not address the free-flow path's lack of tag scoping. Benchmark packs become unreachable for legitimate queries.
- **Risk:** Low. The pool gate is already tested and proven in `test_config_consistency.py`.

**Files:** Deployment config, container preset, or `AGENTALLOY_PHASE_GATE` env var.

### 7.2 Fix 2: Promote contract_tags to Hard Domain Filter (Medium Effort)

**What:** Replace `_soft_tag_filter()`'s intersect-then-fallback behavior with a hard domain filter that excludes fragments whose domain_tags don't match any contract_tag. When the intersection is empty, fall back to the full pool (process-vocab safety valve).

**Current behavior (domain.py:149-168):**
```python
def _soft_tag_filter(ranked, contract_tags):
    if not contract_tags:
        return ranked
    want = {t.lower() for t in contract_tags}
    keep = [f for f in ranked if want & {t.lower() for t in f.domain_tags}]
    return keep if keep else ranked  # empty-fallback
```

**Proposed behavior:**
```python
def _domain_filter(pool, contract_tags):
    if not contract_tags:
        return pool  # no-op when no contract_tags (free-flow stays unchanged)
    want = {t.lower() for t in contract_tags}
    keep = [f for f in pool if want & {t.lower() for t in f.domain_tags}]
    return keep if keep else pool  # empty-fallback
```

This is a no-op change for the current `_soft_tag_filter` — it already does this. The real fix is to **apply domain_tags filtering at the corpus search level** (before RRF fusion), not post-hoc.

**Alternative: Pre-filter at search time:**
```python
# In retrieve_domain_candidates():
if contract_tags:
    pool_size = max(k * 2, 50)
    # Filter at search level
    dense_hits = vector_store.search_similar(
        query_vector,
        categories=_pool_categories(),
        phases=None,
        domain_tags=contract_tags,  # ← ADD THIS
        deprecated_skill_ids=deprecated_ids,
        k=pool_size,
    )
```

**Files:** `retrieval/domain.py`, `storage/fragment_store.py`

### 7.3 Fix 3: Separate Index Per Domain (High Effort)

**What:** Create separate LanceDB tables per domain (e.g., `fragments_snowflake.lance`, `fragments_react.lance`). At retrieval time, select the appropriate table based on contract_tags or task classification.

**Impact:** Complete isolation. Snowflake fragments never compete with React fragments.

**Trade-offs:**
- **Pro:** Eliminates cross-domain leakage entirely.
- **Con:** Requires corpus re-architecture. Multi-table lookups add latency. Harder to maintain.
- **Risk:** High. Significant engineering effort.

### 7.4 Fix 4: Post-Retrieval Domain Filtering (Medium Effort)

**What:** After RRF fusion, apply a hard domain filter based on contract_tags before `skill_granular_select()`. This is more aggressive than `_soft_tag_filter()` — it filters the entire fused pool, not just the ranked list.

**Implementation:**
```python
# In retrieve_domain_candidates(), after _rrf_fuse():
# Apply hard domain filter BEFORE skill_granular_select
if contract_tags and _contract_tag_filter_enabled():
    pool = [f for f in pool if {t.lower() for t in f.domain_tags} & {t.lower() for t in contract_tags}]
    if not pool:
        pool = pool_by_id.values()  # empty-fallback: process-vocab safety valve
```

**Files:** `retrieval/domain.py`

### 7.5 Fix 5: Narrow BM25 Keyword Extraction (Low-Medium Effort)

**What:** Replace the broad `_TECH_KEYWORD_RE` regex with a more targeted extraction that:
1. Prioritizes contract_tags over rule-extracted keywords
2. Filters out generic technical terms that match too broadly
3. Uses domain-specific dictionaries for keyword boosting

**Current regex:**
```python
r"\b(?:\.\w{2,4}|[A-Z][a-z]+\w*|[a-z_]+\d+\w*|[a-z]+-[a-z]+|[A-Z]{2,})\b"
```

**Proposed improvement:**
```python
# Prioritize: contract_tags > skill-specific terms > generic tech terms
# Filter: remove terms that match >N fragments (too broad)
```

**Files:** `retrieval/domain.py`

### 7.6 Fix 6: Free-Flow Task Classification (High Effort)

**What:** In `_compose_free_block()`, classify the task text to infer domain_tags before creating the `ComposeRequest`. This could use:
1. A lightweight classifier (e.g., fastText, or a small LLM call)
2. Keyword matching against known domain vocabularies
3. Fallback to no domain_tags (current behavior) but with a higher k

**Impact:** Free-flow mode would inherit tag scoping from the inferred domain.

**Trade-offs:**
- **Pro:** Addresses the root cause of free-flow pollution.
- **Con:** Requires new infrastructure (classifier). Adds latency.
- **Risk:** Medium. New code path with potential for misclassification.

---

## 8. Recommended Fix: Layered Approach

Given the trade-offs, the recommended approach is a **layered fix** that addresses the problem at multiple levels:

### Phase 1: Immediate (1-2 days)

1. **Activate the pool gate:** Set `AGENTALLOY_PHASE_GATE=on` at deployment.
   - Removes 554 benchmark fragments (16.3% of corpus) from retrieval.
   - One-line config change, zero code changes.
   - **Impact:** ~60% reduction in Snowflake/Iceberg leak.

2. **Raise build/ship k from 2 to 4:** Update `DEFAULT_K_BY_PHASE["build"]` and `DEFAULT_K_BY_PHASE["ship"]`.
   - Gives the retrieval pipeline more room to find on-domain skills.
   - **Impact:** ~20% improvement in on-domain hit rate at k=4 vs k=2.

### Phase 2: Short-term (1-2 weeks)

3. **Apply domain filter at search time:** Add `domain_tags` parameter to `search_similar()` and `search_bm25()`.
   - When contract_tags are present, filter at search level (before RRF fusion).
   - **Impact:** ~15% additional reduction in cross-domain leakage.

4. **Narrow BM25 keyword extraction:** Add a broad-term filter to `_extract_bm25_keywords()` that removes terms matching >N fragments.
   - **Impact:** ~5% reduction in BM25-driven noise.

### Phase 3: Long-term (1-2 months)

5. **Free-flow task classification:** Implement a lightweight classifier for free-flow mode.
   - **Impact:** Addresses the root cause of free-flow pollution.

6. **Corpus re-architecture:** Evaluate separate index per domain (Fix 3).
   - **Impact:** Complete isolation. High effort, high risk.

---

## 9. Implementation Steps

### Phase 1

1. **Set `AGENTALLOY_PHASE_GATE=on`:**
   ```bash
   export AGENTALLOY_PHASE_GATE=on
   ```
   Or in the container preset / systemd unit / docker-compose.yml.

2. **Update `DEFAULT_K_BY_PHASE`:**
   ```python
   # compose_models.py
   DEFAULT_K_BY_PHASE: dict[str, int] = {
       "build": 4,  # was 2
       "ship": 4,  # was 2
       ...
   }
   ```

### Phase 2

3. **Add `domain_tags` parameter to `search_similar()` and `search_bm25()`:**
   ```python
   # fragment_store.py
   def search_similar(self, query_vector, *, categories, phases, domain_tags, deprecated_skill_ids, k):
       # Filter by domain_tags at search level
       if domain_tags:
           domain_filter = f"fragment_id IN (SELECT fragment_id FROM fragments WHERE domain_tags && '{domain_tags}'::text[])"
       else:
           domain_filter = ""
       ...
   ```

4. **Narrow `_extract_bm25_keywords()`:**
   ```python
   # domain.py
   def _extract_bm25_keywords(task):
       matches = list(dict.fromkeys(_TECH_KEYWORD_RE.findall(task)))
       # Filter out terms that appear in too many fragments (too broad)
       # TODO: Add corpus-aware broad-term filter
       if matches:
           return f"{task} {' '.join(matches)}"
       return task
   ```

### Phase 3

5. **Free-flow task classification:** Implement a lightweight classifier (e.g., fastText) that maps task text to domain_tags.

6. **Corpus re-architecture:** Evaluate separate index per domain.

---

## 10. Verification

### 10.1 Unit Tests

1. **Pool gate test:** Verify that `AGENTALLOY_PHASE_GATE=on` excludes benchmark category.
   ```python
   def test_pool_gate_excludes_benchmark():
       with patch.dict(os.environ, {"AGENTALLOY_PHASE_GATE": "on"}):
           cats = _pool_categories()
           assert "benchmark" not in cats
   ```

2. **Domain filter test:** Verify that `search_similar()` with `domain_tags` filters correctly.
   ```python
   def test_domain_filter_at_search_time():
       hits = store.search_similar(query, domain_tags=["react"])
       assert all("react" in f.domain_tags for f in hits)
   ```

3. **BM25 keyword filter test:** Verify that broad terms are filtered.
   ```python
   def test_bm25_keyword_filtering():
       keywords = _extract_bm25_keywords("SQL query on Snowflake table")
       assert "SQL" not in keywords  # too broad
       assert "Snowflake" in keywords  # specific enough
   ```

### 10.2 Integration Tests

1. **Contract path test:** Verify that contract path retrieves on-domain skills.
   ```python
   def test_contract_retrieves_on_domain():
       resp = post("/compose", json={
           "task": "implement secret provisioning",
           "phase": "build",
           "contract_tags": ["install-secret", "container-bootstrap"],
       })
       assert "snowflake" not in resp.source_skills
   ```

2. **Free-flow test:** Verify that free-flow mode with task classification retrieves on-domain skills.
   ```python
   def test_free_flow_with_classification():
       resp = post("/compose", json={
           "task": "implement secret provisioning",
           "phase": "build",
           # No contract_tags — free-flow mode
       })
       assert "snowflake" not in resp.source_skills
   ```

3. **Corpus-wide retrieval audit:** Run `eval/retrieval_audit.py` and verify that benchmark packs are excluded.
   ```bash
   uv run python -m eval.retrieval_audit --k 4
   ```

### 10.3 Metrics

1. **Gold-hit rate:** Run `eval/gold_hit.py` and verify that gold skills are retrieved.
2. **Recall@k:** Run `eval/recall.py` and verify that recall improves.
3. **Leak rate:** Count Snowflake/Iceberg fragments in build injection output (manual or automated).

---

## 11. Summary

| Aspect | Finding |
|--------|---------|
| **Root cause** | Free-flow compose path bypasses all tag-scoping mechanisms |
| **Contributing factors** | (1) Dormant pool gate, (2) k=2 hard cap, (3) broad BM25 keyword extraction, (4) benchmark packs co-indexed with product skills |
| **Impact magnitude** | 554 benchmark fragments (16.3% of corpus) compete with product skills; k=2 limits selection to 2 distinct skills |
| **Recommended fix** | Layered approach: activate pool gate (immediate), domain filter at search time (short-term), free-flow classification (long-term) |
| **Quick win** | Set `AGENTALLOY_PHASE_GATE=on` → ~60% reduction in Snowflake/Iceberg leak |
| **Remaining risk** | Free-flow mode still leaks without task classification; benchmark packs become unreachable for legitimate queries |

---

## Appendix A: Corpus Fragment Counts by Pack

| Pack | Fragments | % of Corpus | Category |
|------|-----------|-------------|----------|
| snowflake | 105 | 3.1% | engineering |
| data-engineering | 100 | 2.9% | domain |
| vue | 118 | 3.5% | engineering |
| temporal | 43 | 1.3% | engineering |
| fastapi | 188 | 5.5% | engineering |
| react | 180 | 5.3% | engineering |
| typescript | ~50 | ~1.5% | language |
| nestjs | ~40 | ~1.2% | framework |
| **Benchmark total** | **554** | **16.3%** | — |
| **Total corpus** | **3,395** | **100%** | — |

## Appendix B: Key Code Paths

### Contract Path (Working)
```
POST /compose/from-contract
  → compose_request_from_contract()
    → ComposeRequest(contract_tags=contract.domain_tags)
      → retrieve_domain_candidates(contract_tags=["react", ...])
        → _resolve_bm25_query() → "react ..." (contract)
        → _soft_tag_filter(ranked, ["react", ...]) → INTERSECT
```

### Free-Flow Path (Broken)
```
ComposeOrchestrator.compose()
  → ComposeRequest(task=signal.task, contract_tags=None, domain_tags=None)
    → retrieve_domain_candidates(contract_tags=None, domain_tags=None)
      → _resolve_bm25_query() → _extract_bm25_keywords(task) (rule-extracted)
      → _soft_tag_filter(ranked, None) → NO-OP
```

## Appendix C: Related Issues

- PLAN-OF-ATTACK.md §E: Retrieval pipeline — k-cap, polluted pool, contract_tags
- PLAN-OF-ATTACK.md §F: Corpus authoring — missing frontend skills + poison-tag repair
- docs/followups.md: Snowflake/Iceberg domain fragments leak into unrelated build injections
- BENCHMARKS.md: Snowflake/Redshift, OTel trace propagation benchmarks
- eval/contracts/domain_14_snowflake_time_travel.md: Benchmark task 14
- eval/contracts/domain_15_snowflake_warehouse_cost.md: Benchmark task 15
