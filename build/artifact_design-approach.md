# AgentAlloy v2.0 — Design (Approach)

Milestone 1 (local agent). This answers the spec's "Open design questions > M1" section under the hard constraints of the approved spec (tenets T1–T8, locked model stack, ports, storage, boundary). v1 (`~/dev/agentalloy`) is a **concepts-only** reference — I inherit its validated *concepts* (loop termination, index-grounded validation, marker injection, decision layer) and build v2's own *mechanics*.

## 0. The load-bearing finding: OverGraph is a Rust core

`overgraph` **0.17.0** is published on crates.io and is a **pure-Rust embedded graph DB with built-in HNSW vector search and GQL queries**, shipped to Python via a PyO3 binding (`overgraph.abi3.so`). v1 uses it from Python (`storage/overgraph_skill_store.py`, `code_index/store/overgraph_store.py`).

Consequence for T6 (Rust data layer): the "data layer" is **one Rust crate (`newagent-core`) that depends on the `overgraph` crate**, exposed to Python through PyO3. That satisfies "code index + skill corpus are OverGraph-backed" and "Rust owns the data layer, one-way Python→Rust" *without* us writing a graph DB — we write the **search/validation logic** (hybrid fusion, MaxSim, index-grounded checks) in Rust on top of the overgraph crate. State + telemetry are **DuckDB** (locked), read by the same Rust crate via `duckdb` read-only.

**This makes the PyO3 boundary real and testable:** Python (interpreter loop, phase machine, LM client, proxy, CLI) calls `newagent-core`; Rust never calls back. The overgraph crate *is* the Rust substrate; our crate is the domain logic.

> Verified facts used below: `overgraph` 0.17.0 (crates.io, "pure Rust, sub-microsecond reads, built-in vector search", repo github.com/Bhensley5/overgraph). `llama-server` 9992 supports `--pooling {none,mean,cls,last,rank}`. The live embed server (`:47951`) returns 768-dim vectors via `/v1/embeddings`. Cargo is **not** installed (mise present) — env prerequisite.

## 1. System architecture

```
                        ┌───────────────────────────  Python (request/workflow layer)  ───────────────────────────┐
  harness LLM  ─HTTP──▶ │  Steering proxy (:48950 /proj/<id>/v1)  ── passthrough + strip-and-replace marker injection │
  (Qwen Code)           │        │ injects: phase banner + state panel (AGENTALLOY-STATE) + composed instructions      │
                        │        ▼                                                                                    │
                        │  REST surface (:48950): ask · state · advance/approve · sessions · contracts · compose      │
                        │        │                                                                                    │
                        │  CLI (ask/state/advance/sessions)                                                           │
                        │        ▼                                                                                    │
                        │  Phase machine  (LangGraph outer graph: nodes=phases, gate edges, interrupt() at approvals,  │
                        │                    Send-based per-task fan-out)                                              │
                        │        │  invokes, per turn/work-item                                                       │
                        │        ▼                                                                                    │
                        │  Interpreter  (LangGraph sub-graph: classify→fill→validate→execute→answer, named exits)     │
                        │        │  LM client (HTTP :50001, temp 0, strict json_schema)                                │
                        │        │  executors dispatch (action,args) ──┐   skill_engine (retrieve-then-infer)          │
                        └───────────────────────────────────────────── │ ────────────────────────────────────────────┘
                                                                       ▼  PyO3 (one-way Python→Rust)
                        ┌───────────────────────────  Rust (data layer: newagent-core)  ───────────────────────────┐
                        │  code_index (overgraph): symbol graph + hybrid search (BM25+dense+MaxSim) + index-grounded │
                        │  skill_corpus (overgraph): skill graph + vectors + BM25 → candidate retrieval               │
                        │  state_read (duckdb RO): contracts/artifacts/checkpointer state                             │
                        │  telemetry_read (duckdb RO): interpreter traces                                            │
                        └──────────────────────────────────────────────────────────────────────────────────────────────┘
   writes (Python owns the RW handle): DuckDB state + telemetry via duckdb-python / LangGraph checkpointer
```

**One process each:** the Python service (`:48950`) loads `newagent-core` in-process (PyO3 cdylib). OverGraph and DuckDB are embedded (in-process), not separate servers. The three LFM models are separate llama-server processes (`:50001` driver, embed port, rerank port). v2 code never loads model weights.

**Data flow for one `ask`:** CLI/REST → interpreter sub-graph → classify (LM) → fill (LM) → validate (Rust index-grounded) → execute (Rust data op) → loop/`none` → answer (LM) → return `{answer, steps[], stop_reason}` + write a `local_agent_trace` (Python→DuckDB).

## 2. Rust crate layout (`rust/newagent-core/`)

```
rust/
  Cargo.toml               # workspace
  newagent-core/
    Cargo.toml             # deps: pyo3, overgraph=0.17, duckdb, tantivy (BM25), tree-sitter + grammars, serde, rayon
    src/
      lib.rs               # PyO3 module init → exposes `DataLayer`
      datalayer.rs         # `DataLayer`: open(dir) → code_index, skill_corpus, state_read, telemetry_read
      code_index/
        mod.rs             # build() (tree-sitter walk → symbols+edges), search()
        build.rs           # tree-sitter parse → Symbol nodes + Calls/Imports/... edges (GQL upsert)
        search.rs          # hybrid: BM25(tantivy) ∪ dense(HNSW) → RRF fusion → top-k
        maxsim.rs          # ColBERT late-interaction MaxSim (Rust; rayon-parallel)
        validate.rs        # index-grounded checks: symbol-exists, slug-known (fail-open on store error)
      skill_corpus/
        mod.rs             # build() from pack YAMLs → skill nodes + vectors + BM25; retrieve_candidates(task, phase, k)
      state_read.rs        # duckdb RO: get_contract, list_artifacts, get_checkpoint_state, get_cursor
      telemetry_read.rs    # duckdb RO: query_traces(k, phase)
    tests/                 # cargo tests: search, maxsim, validate, read paths (offline, temp overgraph/duckdb files)
```

**PyO3 surface (Python calls these):**
- `dl.code_search(repo, query, k, mode) -> [Hit]`
- `dl.symbols(repo, fqn) -> Symbol`
- `dl.knowledge_related(repo, query, k) -> [Decision]` *(parked: returns `[]` at M1)*
- `dl.knowledge_why(repo, fqn) -> Decision` *(parked: returns not-found at M1)*
- `dl.knowledge_entities(repo, query, kind) -> [Edge]` *(parked: returns `[]` at M1)*
- `dl.skill_candidates(task, phase, k) -> [SkillCard]`
- `dl.get_contract(slug) -> Contract | None`
- `dl.list_artifacts(phase, slug) -> [Artifact]`
- `dl.traces(k, phase) -> [Trace]`
- `dl.validate(action, args, repo) -> (ok, error)`

All returns are `serde`-serializable → PyO3 `Py` objects. **No Rust→Python callbacks.** The knowledge_* methods exist (so the interpreter surface is complete) but return structured empty (fail-open) until the graph-native knowledge-leg spec lands.

## 3. Python package layout (`src/newagent/`)

```
src/newagent/
  __init__.py
  config.py            # env-driven config (ports, budgets, caps, model endpoints) — dataclass + .env loader
  client.py            # LMClient (spike, injectable `LM` protocol) — HTTP :50001, temp 0, json_schema
  protocol.py          # (spike, extended) action schemas: READ + STATE groups; prompts; parse_json
  interpreter.py       # LangGraph sub-graph: classify→fill→validate→execute→answer; named exits
  executors.py         # dispatch (action,args) → DataLayer calls → observation strings
  validate.py          # Python schema check + calls DataLayer.validate (index-grounded)
  skill_engine.py      # retrieve-then-infer: DataLayer.skill_candidates → LM select → compose instructions
  phase_machine.py     # LangGraph outer graph: nodes=phases, gate-passing edges, interrupt() approvals, Send fan-out
  checkpointer.py      # custom BaseCheckpointSaver over duckdb-python (IS the state store)
  state_store.py       # duckdb-python RW writes: contracts, artifacts, sessions, cursor
  telemetry.py         # duckdb-python RW writes: local_agent_trace
  steering/
    proxy.py           # FastAPI: /proj/<id>/v1 passthrough + injection; upstream = harness model
    injection.py       # strip-and-replace marker families (v1 concept) — banner + state + instructions
    state_panel.py     # builds the AGENTALLOY-STATE JSON (phase, gates, cursor, actions)
  server.py            # FastAPI app on :48950 (REST + proxy), wires DataLayer + graphs + stores
  cli.py               # `newagent ask|state|advance|sessions|contracts|...` (structured JSON on stdout)
  harness.py           # Harness protocol; MockHarness (tests) + RealHarness (demo, via proxy)
  runbook/             # server-launch runbook (LFM350M embed/rerank + driver) — named M1 deliverable
tests/                 # pytest, offline-green: FakeLM, FakeEmbed, FakeRerank, MockHarness, temp stores
```

**Server = FastAPI** for the Python request layer. (v1's "FastAPI monolith" is redone, not reused: here FastAPI is *only* the request/orchestration surface; the data layer is the separate Rust crate. That's the T6 separation.)

## 4. Interpreter tool surface (locked concept, sized now)

Two tool groups, `none` = abstain/terminate. The READ seed is the spike's validated 10-way; v2 **adds the STATE group** (the acting-interpreter headline, T2).

**READ (read-only; validated seed):** `code_search`, `symbols`, `knowledge_why`, `knowledge_related`, `knowledge_entities`, `artifact_body`, `contract_detail`, `telemetry`, `get_skill_for`, `none`.

**STATE (acting; new):**
- `contract_add` — create a build contract `{slug, task, domain_tags(≤2), touches[], avoids[], success_criteria}`.
- `contract_read` — read a contract by slug (mirrors `contract_detail`; grouped as state for symmetry).
- `artifact_record` — record a phase artifact `{phase, slug, name, body_digest}`.
- `phase_advance` — advance the phase machine to a target phase `{to_phase, route, approved(bool)}` (respects gates; see §13).

**Safety:** state actions are validated (schema + index-grounded where applicable) **before** execution; a failed state call is rejected (never applied) and the error fed to the single retry, then degrades. `phase_advance` cannot skip a gate (see §13). The LFM *proposes* the state op; the phase machine *enforces* it.

**`get_skill_for` vs per-turn composition (open Q1):** both, sharing one core. `get_skill_for` is the interpreter's *on-demand* action (the LFM decides "I need skills for this task"). The *dynamic engine* (T3) is the *JIT* path: on phase/work-item entry the phase machine runs the same retrieve-then-infer and composes the instruction set for the harness. Same engine, two call sites (§7).

## 5. Interpreter loop (LangGraph sub-graph, named exits)

A LangGraph graph (locked: the loop is a sub-graph, reusing the checkpointer for crash-resumability). Nodes: `classify → fill → validate → execute → answer`. Conditional edges. **Named exits** (END edges) — this is AC-8, inherited from v1's `stop_reason`/`degrade_reason` design:

- **`none`** → terminate, answer from gathered context (abstention + sufficient-context).
- **`step_budget`** → hard cap reached (default 2, hard 3) → answer from transcript.
- **`duplicate`** → same (action, canonicalized-args) twice → stop *before* re-executing (v1 runs the duplicate check before execution; keep that).
- **`fill_failed`** → classification/fill invalid after the single retry → degrade with reason.
- **`validation_failed`** → index-grounded check fails after the single retry → degrade, no fabricated entity executed (AC-13).

Termination is guaranteed by LangGraph `recursion_limit` **and** an explicit step counter (so "step budget 2, hard cap 3" is exact, not `recursion_limit`-derived). Every exit writes a named, telemetry-able `stop_reason`. `degraded` + `degrade_reason` ride along (v1's model). Fail-open: store-lookup error in validation *passes* (the executor decides), so a flaky dependency never blocks a schema-valid question.

**AC-8 budget semantics (open Q15):** the budget applies **per interpreter invocation** — one `ask`, or one multi-step state op (e.g. `artifact_record`→`phase_advance` counts as one invocation, not two). The budget is reset at each top-level entry into the sub-graph.

## 6. State-action reliability validation (LOCKED first build task)

De-risks AC-4 (the acting interpreter) *before* the phase machine is built on it. Design:
- **Task set:** ~30–40 structured prompts, each a realistic request that maps to one state op (contract add with correct slug/tags/touches, artifact record with correct phase/name, phase advance to the correct target with correct `approved` flag) + ~10 read ops + ~10 `none` (abstain) for routing balance. Grounded in the SDD packs' real shapes (v1's `sdd-plan-and-contracts.yaml` contract fields).
- **Protocol:** LFM @ `:50001`, temp 0, strict `json_schema` for the target action group. One call per prompt.
- **Check:** (a) output parses to a valid JSON object; (b) `action` field is the *expected* action; (c) required args are present and schema-valid; (d) for routing, `none`/read vs state is chosen correctly.
- **Pass criteria:** ≥ 90% full-correct (action+args) across the set, **zero** schema-invalid outputs, and 100% of `phase_advance` proposals that would skip a gate are the *correct target* (gates are enforced downstream, but the LFM must not propose an illegal jump).
- **On fail:** iterate prompts/schemas (the state-action system prompt + per-action fill hints) until green, *then* build the phase machine. This is the "week-1, not week-6" insurance the user approved.
- Deliverable: a reusable eval harness (`tests/state_action_reliability/`) the M3 in-CI gate reuses.

## 7. Skill inference mechanism (retrieve-then-infer, T3)

A 2.6B cannot hold 355 skills, so the engine is **retrieve → infer → compose**:
1. **Retrieve:** `DataLayer.skill_candidates(task_text, phase, k=K)` — OverGraph dense + BM25 over the skill corpus, filtered by `applies_to_phases ⊇ phase`, top-K (K≈20–40). Returns `SkillCard{skill_id, name, one_liner, domain_tags, applies_to_phases}`.
2. **Infer:** LFM (temp 0, strict `json_schema`) sees the K candidate *cards* (metadata only, not full prose) + the task + phase, and outputs `{"selected": [skill_id...], "rationale": str}` — the subset the task needs.
3. **Compose:** load the selected skills' `raw_prose`, render into the instruction set under a **char budget** (drop lowest-relevance first), **dedupe** against already-injected skills this session. Corpus-grounded: the LFM selects only from the K retrieved → **no fabricated skills** (AC-5).
4. **Deliver:** composed instructions → harness via the steering proxy injection (§14) or the persona/contract handoff.

Two call sites (open Q1): on-demand (`get_skill_for`) and JIT (phase/work-item entry). The compose step is **model-driven selection** over **deterministic retrieval** — v1's deterministic RRF *selection* is what's replaced; retrieval stays deterministic.

## 8. Code index design (Rust, OverGraph-backed)

- **Ingest:** tree-sitter walk of the repo (grammars: python + rust for the newagent self-index; the set is config-driven so M3 multi-repo is additive). Extract `Symbol` nodes (FQN, file, line, kind) + edges: `Calls`, `Imports`, `Inherits`, `Implements`, `Defines`, `HasMember` (v1's edge-kind → label mapping, inherited concept). Node IDs are OverGraph integers; a FQN→node_id map is maintained.
- **Dense leg:** embed each symbol's FQN+signature+short body via the embed model (:48951), MRL-slice 1024→768 + L2-renorm, store as OverGraph vector properties (built-in HNSW). Pre-normalized; similarity = inner product (not cosine).
- **Lexical leg:** tantivy BM25 over symbol names + bodies.
- **Fusion:** Reciprocal Rank Fusion (deterministic) of dense + lexical → top-k. *(Inherited concept; the RRF here fuses retrieval legs, it does not pick skills — skill selection is LFM-inferred, §7.)*
- **Second stage (ColBERT, §9):** optional re-rank of the fused top-k via MaxSim.
- **Index-grounded validation** (`validate.rs`): `symbols`/`knowledge_*` → the FQN must resolve to an existing Symbol node; `artifact_body`/`contract_detail` → the slug must be a known contract (Rust reads the DuckDB state, §12). Fail-open on store error (v1's direction). This is the "no fabricated entity is executed" guarantee (AC-13).
- **Knowledge leg (parked):** the `Decision` node type + `Governs` edges + `knowledge_*` methods exist in the schema and surface, but return structured empty at M1 (the graph-native design is a follow-up spec item).

## 9. ColBERT serving protocol + risk (open Q4) — the #1 technical risk

**Goal:** per-token document vectors + per-token query vectors → MaxSim (late interaction), math in Rust (locked).
- **Primary plan:** serve LFM2.5-ColBERT-350M on the rerank port with `llama-server --pooling none` and **spike the response shape in build task 2** (after the driver is confirmed). If the endpoint emits per-token (seq_len × dim) vectors → the Rust index precomputes per-token doc vectors at index-build, fetches per-token query vectors at query-time, and computes MaxSim (rayon). Clean, matches "separate llama-server, v2 never loads weights."
- **Fallback A (capability gap):** if llama-server cannot emit per-token vectors (likely for a ColBERT model via the standard `/embeddings` path), load the ColBERT GGUF **inside `newagent-core` via llama.cpp (llama.rs)** for per-token hidden-state extraction + MaxSim. This bends "separate llama-server" for the reranker *only*, justified by the capability gap; driver + embedding stay llama-server.
- **Fallback B (schedule):** ship M1's code index with **dense+BM25 fusion only**; wire the MaxSim pipeline as a swappable second stage so ColBERT lands as **M1.x** once the protocol is proven. AC-6 ("real data") and AC-14 are satisfiable with dense+BM25; ColBERT is a quality upgrade, not an M1 AC prerequisite.

**Decision:** primary plan first, with Fallback B as the M1 floor (ColBERT is a stretch within M1, not a blocker). The build order (§24) puts the ColBERT spike early so the decision is made with data, not assumptions. I flag this as the item most likely to move.

## 10. Embedding API shape (open Q5)

`POST {embed_port}/v1/embeddings` (OpenAI-compat, confirmed live on `:47951`), body `{"input": [...strings], "model": <name>}` → `{"data":[{"embedding":[...768...]}]}`. Batch at index-build (e.g. 64–256 symbols/batch). **MRL slice (1024→768) + L2-renorm applied in Rust at index-build time** (the server returns full-dim; the index stores 768). Query-time: same endpoint, single or small batch. The embed model is injectable/fakeable for offline tests (AC-12).

## 11. Phase machine (LangGraph outer graph)

Nodes = phases (intake, spec, design, plan, build, qa, ship). Edges = **gate-passing transitions only** (AC-8: every phase change is a named edge; no open-ended "keep working").
- **Approval gates** at spec→design, design→plan, plan→build (§13): the transition is a LangGraph **`interrupt()`** — the graph pauses, surfaces the gate (digest, artifacts, blockers) to the user, and resumes on `Command(resume=approval)`. `--force` is *not* a resume path (it never bypasses).
- **Per-task fan-out** in plan/build/qa: plan produces N build contracts (one per task, ≤2 domain_tags, covers all tasks — the `build_contracts_cover_tasks` gate). The graph `Send`s each task as an **independent parallel branch** with per-task state + the work-item cursor; branches join (fan-in) at the phase exit. At M1 the fan-out is **structural** (driver LM runs `--parallel 1`); real concurrency is at the harness level (AC-10).
- **Phase node behavior:** on entry, run skill inference (§7) → compose instructions → hand the phase persona + active contract + state panel to the harness (steering) → the harness executes (T4) → on work-item completion the interpreter records artifacts/advances (the acting loop, §4).
- **State schema (graph state):** `{repo, lane, phase, cursor{task_index, task_slug}, contracts[], artifacts{}, approvals{}, session_key}` — checkpointed on every node transition (AC-11).

## 12. Checkpointer on DuckDB + concurrency (open Q7)

LangGraph's `BaseCheckpointSaver` has no DuckDB impl (only InMemory/SQLite/Postgres), so **`checkpointer.py` is a custom `BaseCheckpointSaver` over duckdb-python** — and it **IS the state store** (locked).
- **Tables:** `checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_id, metadata, state_blobs)` for LangGraph's crash-resumability; **separate** human-facing tables `contracts`, `artifacts`, `sessions`, `cursor` that the Rust `state_read` and the CLI/state-panel consume. The checkpointer writes the checkpoint tables; `state_store.py` writes the human tables; both in the **same DuckDB file** (so the Rust read path + the analytics DuckDB see one store).
- **Concurrency model (critical):** DuckDB allows **one RW process** + many RO processes on a file. **Python owns the single RW handle** (all writes: checkpoints, contracts, artifacts, sessions, telemetry). **Rust opens the same file read-only** (`duckdb` `readonly=true`) for `state_read`/`telemetry_read`. No write contention. (This is why the boundary is "Python writes, Rust reads" — it's a DuckDB requirement, not just a style choice.)
- **Crash-resumability (AC-11):** because state is checkpointed to durable DuckDB on every transition, a killed process resumes from the last checkpoint (`resume` → load thread state → continue the graph from the recorded node).

## 13. Approval + digest mechanics (open Q8)

- **Digest:** each recorded artifact stores a content digest (hash of its body). The approval record stores the digests of the artifacts it approved.
- **Void-on-edit:** advancing through a gate is **refused** unless (a) the phase's exit artifact is recorded, and (b) every digest in the approval matches the *current* artifact digest. Editing an approved artifact changes its digest → the approval no longer matches → **unapproved again** (AC-9).
- **Single-call advance-with-approval:** `phase_advance` (interpreter state op) or the REST `advance` action takes `{to_phase, route, approved}`. When `approved:true` the service records the approval *and* transitions atomically (v1's single-call semantics). `--force` is **never** a bypass (AC-9) — it's simply not a resume/approve path.
- **Enforcement point:** the gate check lives in the phase machine's edge condition (Python), reading digests via `state_store` (Python, RW handle). The interpreter's `phase_advance` *proposes*; the phase machine *enforces* — a proposed illegal jump is rejected and reported, never applied.

## 14. Steering surface — the minimal proxy (open Q9)

On `:48950`, a `/proj/<repo_id>/v1` OpenAI-compatible **pass-through** that (v1's marker concept, v2's mechanics):
1. Reads the current workflow state (phase, gates, cursor, active contract, composed instructions) from `state_store`.
2. **Strip-and-replaces** marker blocks in the outgoing request: a one-line **phase banner** (`AGENTALLOY-BANNER`, strip-and-replace every turn) + the **state panel** (`AGENTALLOY-STATE`, structured JSON, strip-and-replace every turn) + the **composed skill instructions** (`AGENTALLOY-INSTRUCTIONS`). Markers are non-phase-stamped (strip-and-replace without knowing the prior phase) — inherited from v1's `proxy_injection.py` design.
3. Forwards to the upstream harness model; passes the response back unchanged.

This is what steers the real harness in the AC-3 demo **and** makes the final port flip drop-in (the harness config change is a port number). **M2** adds composition *auto-injection into every request* on the same intercept (the dynamic engine live on all traffic, T3). M1 injects state panel + banner + the phase's composed instructions (already computed at phase entry, §7).

## 15. Telemetry schema (open Q10)

DuckDB tables (Python writes):
- `local_agent_trace(ts, run_id, repo, kind{ask|state_op}, steps_json[{n, action, args, stop/degrade, ms, tokens}], stop_reason, degraded, degrade_reason, answer_chars, total_ms)`.
- `workflow_event(ts, repo, session_key, phase, event{enter|exit|approve|interrupt|resume|advance|contract_added|artifact_recorded}, task_slug, data_json)`.
- Analytics queries: per-request step/token/latency/exit-reason; per-workflow phase durations; approval waits (interrupt→resume Δt); task fan-out counts. Queryable from the duckdb CLI/Jupyter ad hoc (the `.duck` is the analytics surface — the reason it's DuckDB, per the user). Rust `telemetry_read` serves the `telemetry` action.

## 16. Session/cursor mechanics (open Q11)

- **Sessions** (`sessions` table): `{session_key, repo, lane, status{active|stashed|archived|cancelled}, created, updated}`.
  - `stash`: set status=stashed, checkpoint current graph state (durable).
  - `resume`: status active → load thread state from the checkpointer → continue from the recorded node (AC-11).
  - `archive`/`cancel`: terminal (work item done / abandoned); keep records, no resume.
- **Work-item cursor** (`cursor` table): `{session_key, task_index, task_slug, phase}` — "which task am I on," persists across stash/resume (v1's persistent cursor concept). Fan-out branches update it as tasks complete.

## 17. M1 corpus (open Q12)

A **minimal** OverGraph skill corpus — a few packs (lean toward the ones the self-index + a real newagent task actually touch: `python`, `rust`, `engineering`, `conventions`, `code-review`) — enough to demonstrate LFM-inferred composition across ≥2 tasks with different inferred sets (AC-5). Pack YAMLs → skill nodes (skill_id, name, one_liner, domain_tags, applies_to_phases, raw_prose) + vectors + BM25. Edge generation follows v1's `generate-skill-edges.py` concept (domain/phase edges). Full 355-skill/41-pack import + parity check is M2.

## 18. Config shape (open Q13)

Env-driven (`.env`, `config.py` dataclass), all ports/paths overridable so the dev→drop-in flip is config-only:
- `V2_SERVICE_PORT=48950` (flip to 47950), `V2_EMBED_PORT=48951` (→47951), `V2_RERANK_PORT=48952` (→47952), `V2_MODEL_PORT=50001`.
- `V2_MODEL=lfm2.5-2.6b`, `V2_EMBED_MODEL`, `V2_RERANK_MODEL`.
- `V2_MAX_STEPS=2`, `V2_HARD_CAP=3`, `V2_SEARCH_K=10`, `V2_SKILL_CANDIDATES=30`, `V2_INSTRUCTION_BUDGET_CHARS=24000`.
- `V2_MAX_TOKENS` (driver) + **thinking budget** (reasoning-on: reserve output tokens so thinking doesn't exhaust `max_tokens` — the spike's "thinking budget exhausted" path; explicit config, not an accident).
- `V2_REPO_ROOT`, `V2_STATE_DUCK`, `V2_INDEX_DIR`, `V2_CORPUS_DIR`.

## 19. REST/CLI surface inventory (open Q14)

- **REST (FastAPI, `:48950`):** `POST /ask` (interpreter read path) · `GET /state/panel?repo=` · `POST /state/advance` (body `{slug, to_phase, route, approved}`) · `POST /state/approve-phase` · `GET/POST /state/sessions/{active,stash,resume,archive,cancel}` · `GET/POST /contracts` · `GET/POST /state/artifact` · `POST /compose` (skill composition) · `POST /proj/<id>/v1` (steering proxy) · `GET /health`.
- **CLI:** `newagent ask "..."` · `newagent state [--repo]` · `newagent advance --to <phase> [--approved]` · `newagent sessions {list|stash|resume|archive|cancel}` · `newagent contracts [list|add]` · `newagent artifacts [list|record]` · `newagent compose --task "..." --phase <p>`. All emit structured JSON on stdout (v1 concept).

## 20. Contract schema + domain_tags (open Q16)

`{slug, phase, task, domain_tags (required, ≤2), touches[], avoids[], success_criteria[], created, digest}`. `domain_tags` is **required** (the v1 contract for this spec is untagged — a known v1 gap; v2's schema makes it mandatory and the `build_contracts_cover_tasks` gate checks count ≥ tasks + ≤2 tags each). Stored in the `contracts` table (DuckDB, Python RW), read by Rust `state_read`.

## 21. Build system — one command (AC-15)

A `justfile` (mise-managed):
- `just build` — `cargo build --release` (compiles `newagent-core`) + `pip install -e .` (builds the PyO3 extension via maturin).
- `just test` — `cargo test` + `pytest` (offline-green).
- `just lint` — `ruff check .` + `clippy`.
- `just typecheck` — `mypy --strict src/newagent` (+ `cargo` types are inherent).
- **`just ci` = `lint` + `typecheck` + `test` + `build`** — the one command that must be green. Pre-commit hooks run `ruff` + `clippy`.

## 22. Environment prerequisites (scheduled, not code)

1. **Rust toolchain:** `mise use rust@latest` (add to `mise.toml`). *(Not installed today.)*
2. **LFM350M GGUFs** (Q8_0): obtain `LFM2.5-Embedding-350M` + `LFM2.5-ColBERT-350M`, start llama-server on `:48951` (embed mode) and `:48952` (rerank mode, `--pooling none` for the ColBERT spike) as **new processes** — v1's `:47951`/`:47952` untouched. Launch commands → `runbook/` (named M1 deliverable). Needed at **index-build time** (early M1).
3. Driver LFM2.5-2.6B already running on `:50001` (shared).

## 23. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|-----------|
| 1 | **ColBERT per-token vectors unavailable from llama-server** (`--pooling none` may not emit seq×dim) | High | Med | Build-task-2 spike decides; Fallback B = M1 ships dense+BM25, ColBERT = M1.x (AC-6/AC-14 still green) |
| 2 | **2.6B can't reliably do state actions** (contract add / phase advance) | Med | High | **Locked first task** (§6) validates *before* the phase machine; iterate prompts/schemas until ≥90% |
| 3 | overgraph Rust crate **library ergonomics** (as a `cargo` dep vs the Py binding) differ from the Python API | Med | Med | The crate is on crates.io (0.17.0); spike the `cargo` dependency in build task 1; fallback = use the Py binding from Python for storage and keep MaxSim/fusion/validation in our Rust crate |
| 4 | **DuckDB single-RW** contention if any path writes from Rust | Low | High | Hard rule: Rust opens RO only; all writes via Python's RW handle (enforced in code + a test) |
| 5 | **reasoning-on** thinking tokens exhaust `max_tokens` on the driver | Med | Med | Explicit thinking budget in config (§18); the client already detects "empty content, finish_reason=length" as retryable |
| 6 | M1 scope is large (interpreter + full-lane phase machine + Rust data layer + proxy) | Med | High | Build order (§24) front-loads the two highest-risk items (state-action reliability, ColBERT spike); each AC independently verifiable |

## 24. Build order (dependency DAG → feeds the plan)

1. **Scaffold:** repo layout (`pyproject.toml`, `justfile`, `mise.toml`+rust, `rust/` workspace, `src/newagent/`), `config.py`, `just ci` green on an empty skeleton. *(AC-1, AC-15 base.)*
2. **Env prereqs:** install Rust; obtain LFM350M GGUFs; start embed/rerank servers on `:48951`/`:48952`; **ColBERT `--pooling none` response spike** (decides §9). *(Risk 1, 3, 5 data.)*
3. **`newagent-core` crate:** overgraph code index (build + hybrid search) + `validate.rs`; cargo tests. *(AC-6/7/8 data layer.)*
4. **State-action reliability validation** (the locked first *behavioral* task, §6) — **gate** before the phase machine. *(AC-4 de-risk.)*
5. **Interpreter:** `client.py`, `protocol.py` (read+state), `interpreter.py` (LangGraph sub-graph), `executors.py`, `validate.py`; `newagent ask` end-to-end (read path). *(AC-2, AC-3-read, AC-8.)*
6. **Skill corpus + engine:** `skill_corpus` (Rust) + `skill_engine.py` (retrieve-then-infer). *(AC-5.)*
7. **Checkpointer + state/telemetry stores:** `checkpointer.py` (DuckDB), `state_store.py`, `telemetry.py`; Rust `state_read`/`telemetry_read` (RO). *(AC-11 base, AC-12/13.)*
8. **Phase machine:** `phase_machine.py` (LangGraph outer graph, gates via `interrupt()`, Send fan-out), approval+digest (§13), sessions/cursor (§16). *(AC-3, AC-9, AC-10, AC-11.)*
9. **Steering proxy + REST/CLI:** `steering/` (proxy + injection + state panel), `server.py`, `cli.py`. *(AC-2-REST, AC-3-demo, AC-14.)*
10. **Integration + AC verification:** full main-lane run with the real harness (via proxy), stash→kill→resume, one build contract per task, `--force`-never-bypasses, v2-never-writes-source test. *(AC-3, AC-9, AC-10, AC-11, AC-16.)*
11. **M1.x (if Risk 1 triggered):** ColBERT MaxSim second stage in Rust; re-run AC-6/AC-14.

**Sequencing rationale:** items 2–4 are the de-risking front — the two highest-uncertainty items (ColBERT protocol, state-action reliability) are resolved with data *before* the phase machine (item 8) is built on them. Everything after 4 is lower-risk composition of validated parts.
