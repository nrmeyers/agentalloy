# AgentAlloy v2.0 — Product Specification

*(Contract slug `v2-local-agent-mvp` retained for continuity; scope is the entire product, not an MVP.)*

## Scope in a sentence

A fully-local, zero-cloud-token **spec-driven-development platform** in Python + Rust: a small local **LFM2.5-2.6B model acts as the interpreter** — inferring which skills a task needs, assessing intent, driving contracts/artifacts/phase state, and answering read-only lookups — while the user's own **harness LLM reads the composed instructions and executes**. v2 is the complete successor product to v1 (AgentAlloy): a superset of v1's conceptual surface, in a new architecture.

> **Framing:** this is a *product* spec — the entire v2.0 product — not an MVP scope. Milestone 1 (M1) is the first buildable increment; M2/M3 are staged, not dropped. The acceptance criteria below are M1's (the work item built first); M2/M3 get their own specs + contracts at their design time.

## Core tenets (north star — non-negotiable)

- **T1 — Local-first, zero cloud tokens.** All inference is local: LFM2.5-2.6B interpreter + LFM2.5-Embedding-350M + LFM2.5-ColBERT-350M. No cloud LLM calls, anywhere, ever.
- **T2 — LFM is the interpreter; the harness LLM is the executor.** The control plane (skill inference, intent assessment, contract add/read, artifact recording, phase advance, read-only lookups) runs on the small local LFM. The data plane (reading instructions, writing code, executing) runs on the user's harness model. This split is v2's defining architecture: the small local model thinks; the user's model works.
- **T3 — Dynamic skill engine.** Which skills a task needs is **inferred by the LFM per task**, grounded in the curated corpus (41 packs / 355 skills). This replaces v1's deterministic phase/tag RRF fusion with model inference — a true just-in-time context engine: context varies with the task, not just the phase. Corpus-grounded: the LFM selects/composes from corpus candidates and never fabricates skills.
- **T4 — Wrap, don't replace.** v2 steers the user's existing harness (Qwen Code, Claude Code, …); v2 never writes target-repo source. Execution is always delegated to the harness.
- **T5 — SDD discipline is enforced, not suggested.** A LangGraph phase machine (intake → spec → design → plan → build → qa → ship, plus fast/add-skill lanes in later milestones) with approval gates, per-task build contracts, a work-item cursor, and crash-resumable sessions.
- **T6 — Rust data layer, Python orchestration.** Long-lived, memory-heavy data (code index, skill corpus, state, telemetry, validation) is Rust; workflow + interface (LangGraph graphs, LM client, prompts, surfaces, CLI, config) is Python. One-way boundary: Python calls Rust.
- **T7 — Drop-in successor to v1 (concepts + ports, not mechanics).** v2 inherits v1's ports and conceptual surface (product inventory below) in v2's own architecture. **v1 is a reference for CONCEPTS, not mechanics** — v1's implementations (FastAPI proxy mechanics, deterministic signal layer, RRF fusion, per-repo token plumbing, its doc structure) are NOT inherited; v2 does them better.
- **T8 — Deterministic where it matters, fail-open elsewhere.** temp 0 + pinned local models + strict schemas → reproducible interpreter decisions; named exits for every loop and named transitions for every phase edge; model down → structured error; validation failure → degraded response, never fabrication.

## What v1 teaches (concepts) vs. how v2 does it better

**Inherit as concepts:** JIT context over static AGENTS.md; the three context legs (instructions / code / knowledge); SDD lifecycle with approval gates + per-task build contracts; wrap-don't-replace; local-first; compound engineering (qa lesson → decision doc → promoted skill); telemetry-driven observability.

**Redo (v1 mechanics are reference only):**
- v1's deterministic signal layer + RRF fusion decides what to inject → **v2: the LFM interpreter infers per task** (T3). v1's "deterministic by default" becomes "reproducible temp-0 model inference — model-driven, not table-driven."
- v1's MCP-derived 10-way query surface → **v2: an interpreter-native tool surface** with BOTH read actions and state/contract actions (the LFM adds/reads contracts, records artifacts, advances phases). The spike-validated 10-way protocol (this repo: `protocol.py`, `tasks/tasks.jsonl`, gates green) is the *validated seed* for the read subset; the final surface is v2's, sized in design.
- v1's vendored Python code index → **v2: a new Rust OverGraph-native index** (hybrid search + ColBERT second stage).
- v1's FastAPI proxy mechanics → **v2: its own proxy mechanics** (M2), same conceptual surface (intercept harness traffic, auto-inject composed skills).
- v1's FastAPI monolith → **v2: Rust data layer + Python/LangGraph workflow layer.**

## Product inventory (v1 superset + v2-new)

1. **Dynamic skill engine (instructions leg)** — LFM-inferred, per-task skill composition from the 41-pack corpus. *(v2-new: model inference vs v1's deterministic fusion.)*
2. **Code index leg** — Rust OverGraph symbol graph; hybrid semantic (LFM-Embedding-350M) + lexical (BM25) + ColBERT MaxSim second stage; callers/callees, budgeted context bundles, staleness watch. *(v2-new: Rust-native, ColBERT.)*
3. **Knowledge leg (decisions)** — typed decision layer over the code index: why code exists; on-demand query + automatic push when a work item's scope touches governed code; superseded decisions never surface. *(concept inherited.)*
4. **SDD phase machine** — full main lane + sdd-fast + add-skill lanes; approval gates with digest invalidation; per-task build contracts; work-item cursor; sessions (stash/resume/archive/cancel); workflow pause/resume. *(concept inherited; v2 mechanics: LangGraph.)*
5. **LFM interpreter** — intent assessment; skill inference; contract add/read; artifact recording; phase advance; read-only lookups; the always-on local brain. *(v2-new — the headline.)*
6. **Harness integration** — transparent proxy with auto-injection (proxy-wired harnesses), MCP server, sidecar (static rules file), dual-carrier (BYOK + sidecar), git worktree support. *(concept inherited; v2 mechanics.)*
7. **Profiles** — user-scoped skill contexts, auto-resolved per repo. *(concept inherited.)*
8. **Contracts system** — per-task build contracts (domain_tags, touches/avoids, success criteria) as retrieval input and execution scoping. *(concept inherited.)*
9. **Telemetry** — structured traces for every interpreter action, composition, and proxied request; savings/coverage analytics; queryable DuckDB. *(concept inherited.)*
10. **Web UI** — operator dashboard (config, telemetry, skills, playground, repos/approvals). *(concept inherited; M2.)*
11. **Install/ops** — setup wizard, hardware presets, container + native deployment, service units, upgrade/release check, doctor. *(concept inherited; M3.)*
12. **Skill corpus + authoring** — 41 packs / 355 skills, R1–R8 quality contract, local-first author-critic pipeline. *(content inherited; v2's OverGraph store.)*
13. **Compound engineering** — qa-gated lesson capture (`docs/solutions/<slug>.md`), promotion to injected skills, dedup-gated. *(concept inherited; M3.)*
14. **Eval/benchmark gates** — pre-registered task matrix, LLM-judge cross-check, CI gate that must re-baseline against the LFM350M retrieval stack. *(concept inherited; M3.)*
15. **Surfaces** — CLI (structured JSON on stdout) + REST. *(v2 owns the shape.)*

## Milestone map (local-agent first)

- **M1 — Local agent (this spec's build target; ACs below).** LFM interpreter (acting: read + state tools) · LangGraph phase machine, full main lane · dynamic skill engine core (LFM inference over a minimal corpus) · Rust data layer (self-index of newagent, OverGraph corpus, DuckDB state + telemetry) · LFM model stack · steering + ask surfaces (CLI + REST) · sessions, gates, contracts. The product's identity (T2/T3) is present from day one.
- **M2 — Composition.** Transparent proxy (auto-injection of composed skills into harness traffic; v2's own mechanics) · full 355-skill / 41-pack corpus import + parity check vs v1's store · profiles · MCP server · sidecar/dual-carrier harnesses · Web UI.
- **M3 — Scale & ops.** sdd-fast + add-skill lanes · setup wizard/installer · container deployment + service units · upgrade/release check · SSE streaming · multi-repo · in-CI eval gate (LFM350M re-baseline) · worktrees · compound engineering (lessons promote) · telemetry analytics depth.

## Locked decisions

### Product-wide
- **Codebase home:** standalone v2.0 in `newagent`; v1 (`~/dev/agentalloy`) is untouched reference.
- **Interpreter/executor split (T2):** the LFM directly invokes its tool surface — read-only lookups AND state operations (contract add/read, artifact recording, phase advance). The harness LLM reads composed instructions and executes; it does not drive v2 state.
- **Dynamic skill engine (T3):** LFM-inferred, corpus-grounded, per-task; present from M1 over a minimal corpus; scales to full corpus + proxy auto-injection in M2.
- **Model stack (all local, separate llama-server processes; v2 code never loads weights):**
  - **Interpreter/driver:** LFM2.5-2.6B-Q8_0 + DSpark @ `127.0.0.1:50001`, temp 0, strict `json_schema`. The server runs with `reasoning on` — thinking tokens consume the output budget (the spike's "thinking budget exhausted" path proves it), so `max_tokens`/answer budgeting is an explicit config decision.
  - **Embedding:** LFM2.5-Embedding-350M @ `127.0.0.1:47951`. Native 1024-dim; MRL slice to 768 + L2 re-normalization at index-build; pre-normalized inner product (not cosine).
  - **Reranking:** LFM2.5-ColBERT-350M @ `127.0.0.1:47952`, late-interaction MaxSim over per-token vectors; MaxSim math in the Rust index (O(N), CPU/edge-optimized). Accepted trade: strictly semantic, no instruction steering.
  - The 2.6B performs no similarity math — retrieval lives entirely in the 350M models.
- **Storage (T6):** code index + skill corpus → **OverGraph** (graph + vectors + BM25 sidecar); state + telemetry → **DuckDB** (Python writes, Rust reads via duckydb; the `.duck` file is ad-hoc queryable from CLI/Jupyter).
- **Workflow runtime:** LangGraph (Python). Outer phase-machine graph (nodes = phases, edges = gate-passing transitions, `interrupt()` at approvals, `Send`-based task fan-out) + interpreter sub-graph. **The LangGraph checkpointer IS the state store** (custom `BaseCheckpointSaver` over duckdb-python; no official DuckDB saver exists).
- **Boundary (T6):** Rust owns the data layer (code index, skill corpus, state/telemetry read paths, index-grounded validation); Python owns the workflow + request layer (LangGraph graphs, LM client, prompts, surfaces, CLI, config, state/telemetry writes). One-way Python→Rust via PyO3; no Rust→Python callbacks.
- **Ports (T7):** service `:47950`, embed `:47951`, rerank `:47952`, model `:50001`.
- **Posture (T8):** v2 never writes target-repo source; fail-open; reproducible (temp 0, named exits/transitions); crash-resumable.
- **v1 is a concepts-only reference (T7).**
- **Build discipline:** every change auto-checked (ruff, clippy, mypy --strict, pytest, cargo test); one command builds Python + Rust.

### M1-specific
- **Lane coverage:** full main lane only (intake → spec → design → plan → build → qa → ship); sdd-fast / add-skill are M3.
- **Skill corpus:** minimal (a few packs) — enough to demonstrate LFM-inferred composition end-to-end; full corpus is M2.
- **Harness integration:** steering + ask surfaces (CLI + REST); the transparent proxy is M2; MCP/sidecar are M2.
- **Build executor:** the user's harness agent executes code under v2's contracts (T4); a mock harness exists for offline tests.
- **Code index:** self-index of the newagent repo (real data for the read actions); multi-repo is M3.

## Environment prerequisites (setup, not code)

- The two LFM350M GGUFs are **not yet present** in `~/.local/share/agentalloy/models/` (currently only nomic-embed-text-v1.5.Q8_0.gguf and Qwen3-Reranker-0.6B-Q8_0.gguf). Before the live demos: obtain `LFM2.5-Embedding-350M` + `LFM2.5-ColBERT-350M` GGUFs (Q8_0), and (re)start llama-server on `:47951` (embedding mode) and `:47952`, replacing the nomic/Qwen3 servers. Launch commands ship as runbook text, not code.
- Model provisioning infra (auto-pull, service units) is M3; a manual runbook suffices for M1.

## Assumptions (correct me now, or I build on these)

1. The harness + harness model are the user's (e.g. Qwen Code + Qwen3.8-27B @ `:60005`); v2 assumes a harness that can consume steering context and dispatch work; v2 does not manage or configure it.
2. The code index is v2's own new Rust implementation (not a port of v1's vendored Python index); it must serve the read actions with real data over at least the newagent repo. Exact machinery (tree-sitter grammars, OverGraph usage, hybrid design, ColBERT integration) = design.
3. "Real data" at M1 = the newagent repo self-indexed; the state store holds real contracts/artifacts the workflow produces; telemetry records interpreter traces. Multi-repo = M3.
4. **Skill-engine grounding:** a 2.6B model cannot hold 355 skills in context, so the engine is retrieve-then-infer (corpus retrieves candidates; the LFM infers over candidates). The exact two-stage shape is a design question; corpus-grounding is locked (T3).
5. **Interpreter tool surface:** the spike-validated 10-way (read actions + `get_skill_for` + `none`) is the validated seed; v2's final surface ADDS state/contract actions (contract add, contract read, artifact record, phase advance, session ops). Exact list + schemas = design; the concept (acting interpreter with read + state tool groups, `none` = abstain/terminate) is locked.
6. Drop-in successor = v1's ports + conceptual-surface parity; during development v2's service owns `:47950` and v1 does not run concurrently on it.
7. Retrieval quality is accepted without an in-M1 eval gate (the spike baseline was nomic-embed; the LFM350M adoption is a deliberate upgrade; the M3 eval gate re-baselines).
8. The M1 demo uses the user's actual harness as build executor; a mock harness covers offline tests.

## Acceptance Criteria (M1)

## AC-1: Standalone codebase
`newagent` is self-contained: its own `pyproject.toml`, its own Rust workspace, its own CLI. It does **not** import v1's `src/agentalloy` at runtime, and `~/dev/agentalloy` is **not modified**.

## AC-2: Ask end-to-end (interpreter read path)
`ask "<a real question about the newagent codebase>"` (CLI, and REST on the service port) sends the question to the **live** LFM2.5-2.6B @ `:50001`, drives the interpreter loop, and returns a JSON response with a **non-empty `answer`** and a **`steps[]` trace**. Demonstrated with at least one real question.

## AC-3: Phase machine end-to-end (full main lane)
A small real task in the newagent repo is driven through intake → spec → design → plan → build → qa → ship: phase state recorded, spec/design artifacts produced, **one build contract per planned task**, the **harness agent** steered through build/qa under those contracts, and **sessions** (stash → process kill → resume) demonstrably surviving a restart. The harness performs code edits; v2 performs orchestration.

## AC-4: Acting interpreter (read + state tools)
The LFM directly invokes both tool groups: **read actions** (code_search, symbols, knowledge_why, knowledge_related, knowledge_entities, artifact_body, contract_detail, telemetry, get_skill_for) and **state actions** (contract add, contract read, artifact record, phase advance). All read actions are read-only; `none` is the abstain/terminate. The exact surface is per design, but the M1 build demonstrates the LFM driving a contract add and a phase advance itself.

## AC-5: Dynamic skill engine core
Given a task, the LFM infers the skills it needs from the (minimal) OverGraph corpus and composes them into the instruction set delivered to the harness. Composition is **corpus-grounded**: every injected skill is a real corpus entry, nothing is fabricated. Demonstrated with ≥2 tasks whose inferred skill sets differ, all corpus-valid.

## AC-6: Real data from v2's stores
Read actions are served by **v2's own data layer** — not v1-over-HTTP, not canned fixtures: code/symbol/knowledge actions from v2's Rust code index over the newagent repo; artifact_body/contract_detail from LangGraph checkpointer state in DuckDB; telemetry from real interpreter traces in DuckDB; get_skill_for from the OverGraph skill corpus.

## AC-7: Py/Rust boundary
The **data layer** (code index, skill corpus, state/telemetry read paths, index-grounded validation) is **Rust** (OverGraph + DuckDB via duckydb). The **workflow + request layer** (LangGraph graphs, LM client, prompts, surfaces, CLI, config, state/telemetry writes) is **Python**. The boundary is **one-way** (PyO3, Python→Rust only); no Rust→Python callbacks.

## AC-8: Reproducible, fully terminating
The interpreter loop exits **only** on: `none`, step budget (default 2, hard cap 3), duplicate detection (same action+args twice), or validation failure after one retry — every exit a named, telemetry-able reason. The phase machine has **named transitions only** (every phase change is a gate-passing edge). temp 0 + pinned model + strict schemas.

## AC-9: Approval gates
spec→design, design→plan, and plan→build are **approval-gated**: advance refused until the phase's exit artifact is recorded; approval recorded via the advance/approve surface; **editing an approved artifact voids the approval** (digest invalidation); **`--force` never bypasses** the gate. A test proves each of these four properties.

## AC-10: Per-task build contracts + parallel structure
Plan produces **one build contract per task** (contracts ≥ tasks; each ≤2 domain tags; each with touches/avoids scope). The graph models N tasks as **independent parallel branches** with per-task state + work-item cursor. At M1 the fan-out is structural (driver LM runs `--parallel 1`); real concurrency happens at the harness level (independent tasks only, sibling synthesis).

## AC-11: Crash-resumable sessions
Sessions support **stash**, **resume**, **archive**, **cancel**. A test proves: run a workflow partway, kill the process, restart, `resume` → the workflow continues from the exact recorded position (phase, task cursor, approval state).

## AC-12: Injectable clients; offline-green tests
The LM client and the embed/rerank model calls are **injectable** (small protocols). The **full suite (Python + Rust) passes with no live model, no running data service, and no real harness** (fakes + mock harness).

## AC-13: Fail-open posture
Model down → **structured error** (with reason), never a traceback. Validation failure after retry → **degraded response** with reason + steps already retrieved; no fabricated entity is ever executed.

## AC-14: v1's ports; native LFM stack
Service `:47950`, embed `:47951`, rerank `:47952`, model `:50001`; ask + steering surfaces reachable on the service port. Model stack = LFM2.5-2.6B @ `:50001`, LFM2.5-Embedding-350M @ `:47951` (MRL 768 + inner product), LFM2.5-ColBERT-350M MaxSim @ `:47952`; all separate llama-server processes; v2 code never loads weights.

## AC-15: One-command build; every change auto-checked
One command builds Python + Rust. ruff + clippy (lint), mypy --strict (types), pytest + cargo test (tests) all run and **pass**.

## AC-16: v2 never writes target-repo source
v2-owned code paths **mutate no target-repo source file**. v2 writes only: workflow state, contracts, artifacts, approvals, sessions, telemetry — all in v2's own stores. A test asserts the target repo's source tree is untouched after a full mock-harness workflow run (only the harness's own edits appear — the harness's, not v2's).

## Out of Scope (M1 — staged, not dropped)

Deferred to **M2 (Composition)**: transparent proxy with composed-skill auto-injection (v2 mechanics, not v1's) · full 355-skill/41-pack corpus import + parity check vs v1's store · profiles · MCP server · sidecar/dual-carrier harnesses · Web UI.

Deferred to **M3 (Scale & ops)**: sdd-fast + add-skill lanes · setup wizard/installer · container deployment + service units · upgrade/release check · SSE streaming · multi-repo · in-CI eval gate (LFM350M re-baseline) · git worktrees · compound engineering (qa lessons promote) · telemetry analytics depth · model provisioning infra.

**Never in v2 (tenet-level exclusions):** cloud LLM calls (T1) · v2 writing target-repo source (T4) · fabricated skills (T3) · porting v1's mechanics (T7 — concepts only).

## Open design questions

### M1 (design phase, now)
- **Interpreter tool surface:** final action list + JSON schemas (read seed from the spike's 10-way + state/contract actions); how `get_skill_for` and per-turn skill composition interact (explicit action vs implicit composition); prompt engineering for state-action safety.
- **Skill inference mechanism:** the retrieve-then-infer two-stage shape — corpus candidate retrieval (OverGraph dense + BM25) → LFM selection/composition; candidate budgets; how composition renders into the instruction set the harness reads; dedupe against already-injected skills.
- **Code index design:** tree-sitter grammars, OverGraph usage, hybrid (semantic/lexical) design, first-stage dense + second-stage ColBERT pipeline integration.
- **ColBERT serving protocol:** how the Rust index obtains **per-token** vectors from the served LFM2.5-ColBERT-350M (llama.cpp's `/embeddings` pools by default — custom serving mode, rerank-style endpoint, or client-side MaxSim over raw token outputs); MaxSim math is locked to the Rust index.
- **Embedding API shape:** request/response contract against `:47951` (batching, MRL slice at index-build time).
- **LangGraph graph structure:** outer phase machine (nodes, gate-passing edges, `interrupt()` at approvals, `Send`-based fan-out) + interpreter sub-graph; state schema of each graph.
- **LangGraph checkpointer on DuckDB:** the custom `BaseCheckpointSaver` over duckdb-python; table layout for checkpoints vs the human-facing state/contracts/artifacts tables; how the Rust read path (duckydb) consumes it.
- **Approval + digest mechanics:** where artifact digests live, the void-on-edit rule's enforcement point, single-call advance-with-approval.
- **Steering surface shape:** state-panel endpoint contract, composed-instruction/persona delivery, and how a harness ingests it (hook/prompt/config) without v2 owning the harness.
- **Knowledge layer (M1 shape):** decision docs + a deterministic decision-linking pass (v1's `_index_decisions` concept) over the Rust index; on-demand `why` queries; push at cursor entry.
- **Telemetry schema:** interpreter trace fields (per-step action, tokens, latency, exit reasons), per-workflow phase durations, approval waits, task fan-out; indexing for analytics.
- **Session/cursor mechanics:** work-item cursor semantics across stash/resume; archive/cancel terminal-state handling.
- **M1 corpus:** which packs, OverGraph layout + edge generation (v1's `generate-skill-edges.py` is the precedent).
- **Config shape**, step budget, result cap (env-driven pattern), including the driver's `max_tokens`/thinking-budget allocation.
- **REST/CLI surface inventory** (ask, state panel, advance/approve, sessions, contracts, artifacts, compose).

### M2 (spec'd at its design time)
- Transparent proxy mechanics (traffic interception, per-harness surfaces, auth pass-through, composed-skill auto-injection) — v2's own design, not v1's.
- Full corpus import + parity check vs v1's store; profiles; MCP server; sidecar/dual-carrier; Web UI.

### M3 (spec'd at its design time)
- sdd-fast + add-skill lane semantics (approval-flag semantics per v1's packs); installer/wizard; container + service units; upgrade/release; SSE; multi-repo; in-CI eval gate (LFM350M re-baseline); worktrees; compound engineering (qa lesson capture + promote); telemetry analytics depth.
