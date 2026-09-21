# Handoff — AgentAlloy v2 for the v1 UI surface (TheForge) integration

**Audience:** the session integrating this service into the v1 UI surface — TheForge (spec-first engineering hub, TypeScript/React, `~/dev/TheForge`).
**Written by:** the TheForge integration session, 2026-09-06.
**Updated:** 2026-09-12 — base state moved `b398305` → HEAD `5378840` (clean tree). The 4-point contract in §2 **landed** (code-index P0–P3, `9e9a518`…`bb361ea`, plus follow-up fixes); §2 now documents what was delivered and how the capability flips work today.
**Do not confuse with** the pre-existing `agentalloy-local-agent.md` (orientation doc, unrelated).

## 1. Context

TheForge is rewired so that **skills composition + code index** are served by this service as a plain HTTP capability endpoint:

- TheForge talks to `http://localhost:48950` only. No other ports, no S3, no `repos` registry endpoint. The service binds `127.0.0.1` by default (localhost-only is the safety baseline; `V2_REPO_ALLOWLIST` is defense-in-depth for deliberately exposed deployments).
- TheForge's client (`src/services/retrieval/code-indexer-client.ts`) is **fail-open**: every call degrades to an `Err` envelope, never throws. If this service is down, TheForge features degrade silently — nothing hard-fails.
- TheForge proxies `/api/code-index/*` → this service. Legacy v1-shaped routes that v2 does not serve return `501` in TheForge; **route-liveness is the on-switch** — when v2 lands a route, the 501 disappears and the feature turns on automatically.
- `GET /status` → `capabilities` block is consumed by TheForge's D7c capability UI. Flipping a capability flag to `true` surfaces the feature in TheForge with zero TheForge-side changes.

**Today both integration-relevant flags are real, not aspirational.** `graph` and `knowledge` flip automatically at startup from wiring success (module flags `graph_ready` / `knowledge_ready`, `src/newagent/server.py:54-55`), not from hand-edits:

```python
# src/newagent/server.py:296
def _capabilities() -> dict[str, bool]:
    """`graph` flips only once the index is fully wired (store + searcher via
    set_graph_index); `knowledge` only after a successful knowledge ingest.
    A partial startup must not advertise features that would return
    empty/null forever. The rest are explicit until their code paths land:
    rerank is unused (RRF fusion), reindex is synchronous (no job model)."""
    return {
        "graph": graph_ready,
        "knowledge": knowledge_ready,
        "rerank": False,
        "jobs": False,
    }
```

- `graph_ready` → True only after `open_codegraph` + delta ingest + FTS + `CodeSearcher` all succeed and `set_graph_index` runs (`server.py:226`); a half-wired index is torn down and both flags reset to False (`server.py:246-247`).
- `knowledge_ready` → True only after `ingest_knowledge` succeeds (`server.py:214` at boot, `server.py:669` on `/reindex`). A knowledge-ingest failure degrades to a code-only index, never a boot failure.

So: a healthy startup with a built index advertises `{"graph": true, "knowledge": true, "rerank": false, "jobs": false}` — and TheForge's D7c UI surfaces both features with no further work.

## 2. What was delivered (the old 4-point contract)

1. **`graph_query` — landed as a Python tool, not a Rust DataLayer method.** P0 (`9e9a518`) re-vendored the v1 tree-sitter engine + an OverGraph store; the retrieval path is now `src/newagent/code_index/` with the vendored Rust engine (`rust/newagent-core/src/{lib,ingest,lexical,dense,fusion,validate}.rs`) doing parse/tantivy/dense/fusion. Hybrid retrieval = dense + PageRank + BM25 with RRF fusion (`code_index/retrieval/hybrid.py`).
   - Tool executor: `_graph_query` in `src/newagent/executors.py` → `code_index/graph_query.py:24` (subgraph around hybrid-search seeds; top-centrality overview on empty query; args `query`, `limit`, `hops`, `repo`).
   - Capability auto-flip is the `graph_ready` flag (see §1) — no `hasattr` probe anymore.
2. **Real knowledge graph — landed, and `knowledge` flips on ingest success.** `code_index/knowledge.py::ingest_knowledge` projects decision docs + SDD lifecycle state + front-matter edges into the graph at boot and on `/reindex`. The three executors (`executors.py:273/289/305`) call `retrieval/knowledge.py` (`governing_decision` / `related_decisions` / `entity_edges`). Response keys are exactly the shapes TheForge expects: `decision` / `decisions` / `entities` — unchanged from the stub contract.
3. **Tool surface — 15 tools in the `execute_tool` chain** (`src/newagent/executors.py:87`):
   - READ: `code_search`, `symbols`, `graph_query`, `knowledge_why`, `knowledge_related`, `knowledge_entities`, `artifact_body`, `contract_detail`, `telemetry`, `get_skill_for`, `assemble_skill`
   - STATE: `contract_add`, `artifact_record`, `phase_advance`, `phase_reset`
   - Unknown name → `ValueError` → surfaced as `{"ok": false, "error": ...}`.
   - `POST /tool` (`server.py:567`) envelope unchanged: `{"ok": true, "result": "<JSON string>"}` | `{"ok": false, "error": str}` — `result` is a **JSON string, not an object**. New **optional** `project` field (additive, defaults `""`): state tools run scoped to that project's lifecycle; legacy callers are unaffected.
   - The LFM interpreter (`interpreter.py`) calls the same `execute_tool`, so every new tool is also on the local agent's tool-calling path with no extra wiring.
   - Note for non-TheForge readers: the MCP bridge (`mcp_server.py`) delegates **all** tool calls to this service's `/tool` over HTTP (the service holds the only RW DuckDB handle). Its default surface is deliberately slim — `code_search` + `contract_detail` (`mcp_server.py:52`) — for the local agent's harness, not for TheForge.
4. **Byte-compat surface — preserved, and now machine-enforced.** `tests/test_http_byte_compat.py` (P3, `bb361ea`) pins the §3 invariants; the P3 suite also covers store/retrieval/knowledge (`tests/test_code_index_{store,retrieval,knowledge}.py`).

## 3. Byte-compat invariants (load-bearing — do not change)

| Endpoint | Contract |
|---|---|
| `GET /health` | `{"status":"ok"}` exactly (scripts grep for `"status":"ok"`) |
| `GET /status` | `{api_version: "2.0", capabilities: {graph, knowledge, rerank, jobs}, phase, service_port, model_port}` + **additive** stats when wired: `symbols`, `chunks`, `skills`, `corpus_loaded`. `api_version` stays the string `"2.0"` (`server.py:35`). No `repos` key. |
| `POST /tool` | `{name, args: <JSON string>, project?: str}` → `{"ok":true,"result":"<JSON string>"}` or `{"ok":false,"error":"..."}` |
| `POST /reindex` | `{repo: <path>}` → **200 on both outcomes**, synchronous, idempotent per repo (re-ingest replaces the repo's entries). Path is validated **before** registration (must be a directory; optional `V2_REPO_ALLOWLIST`, colon-separated roots — unset = unrestricted, since the default bind is localhost). Response is additive: `repos` list always (once initialized), plus `symbols`/`chunks`/`mode` on success. Registry persists next to `state.duck`, so the index rebuilds across restarts. |
| `code_search` rows | `{file, line, content, symbol: string|null}` — base keys preserved. The graph-index path **now emits `score`** (real per-hit, RRF-fused) plus additive `kind`, `end_line`, `repo`, `centrality`, `qualified_name`; top-level envelope carries additive `mode` (`"hybrid"` \| `"lexical-only"`) and `error` on failure. |
| `symbols` rows | `{fqn, file, line, kind}` (key is `symbols`, not `results`) |

Previously-known quirks, now resolved:

- The Rust path's missing `score` is gone — scores come from RRF fusion in the Python hybrid searcher.
- The Python text-walk fallback (no index wired, `V2_REPO_ROOT` walk) now carries `symbol: null` on every row, so the row contract holds on the degraded path too.

## 4. Serve & verify

```bash
# full stack: uv sync + podman model servers + service
# (interpreter :50001 is now MiniCPM5-2B with DSpark speculative decoding;
#  embed nomic-embed-text-v1.5 :48951; local-agent proxy :48953 — the last
#  two are for the v2 local agent, not TheForge)
scripts/v2-bringup.sh up            # or: serve | smoke | status | down

# the service alone
cd ~/dev/newagent && source ~/.local/share/agentalloy-v2-instance/env.sh \
  && uv run agentalloy serve --port 48950 --host 127.0.0.1

# smoke
curl -s localhost:48950/health
curl -s localhost:48950/status
curl -s -X POST localhost:48950/tool \
  -H 'content-type: application/json' \
  -d '{"name":"code_search","args":"{\"query\":\"embedding\",\"k\":5}"}'
curl -s -X POST localhost:48950/tool \
  -H 'content-type: application/json' \
  -d '{"name":"graph_query","args":"{\"query\":\"ingest\",\"limit\":10,\"hops\":1}"}'
```

Verification suites (all in this repo, no TheForge needed):

```bash
# byte-compat + code-index contract suite (P3)
uv run pytest tests/test_http_byte_compat.py \
  tests/test_code_index_store.py tests/test_code_index_retrieval.py \
  tests/test_code_index_knowledge.py

# retrieval quality, live against the running service (read-only; recall@5/@10/MRR)
uv run python scripts/codeindex_recall_benchmark.py
```

End-to-end from the TheForge side (runs `scripts/check-indexer.sh`: probes `/health`, `/status`
`api_version` 2.0 + capabilities dict, `code_search` ×2, `symbols` via `/tool`):

```bash
cd ~/dev/TheForge && pnpm check-indexer
```

TheForge graph-viewer queries arrive via `POST /api/code-index/search/semantic` and
`/api/code-index/search/symbol` → mapped to `code_search` / `symbols`.

## 5. Coordination rules

- **`src/newagent/server.py` is the shared file** between the two sessions — keep diffs there minimal and additive.
- Changes to existing tool row shapes are **additive only** (new fields OK; renaming/reordering/removing keys is not). The byte-compat test file is the guardrail — extend it when you extend the surface.
- `/status` and `/tool` envelopes are frozen; extend with new fields, never reshape.
- **Capability flags are truthful readiness flags, not config.** `graph`/`knowledge` flip from wiring success at startup (`set_graph_index` / `ingest_knowledge`); a partial startup must advertise `false`, never a feature that would return empty/null forever. Don't hardcode `True`.
- If a contract point above turns out infeasible, say so in the commit message rather than reshaping the surface.
