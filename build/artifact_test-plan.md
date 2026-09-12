# v2-local-agent-mvp — Test Plan (M1)

Cases are behaviors (input/condition → expected result), not test code. AC-N references are to the spec's acceptance criteria. Build turns these into real tests and adds the edges it uncovers.

**Global invariants (every task):** `just ci` green (AC-15) · offline-green — no live model, no running data service, no real harness (AC-12) · v2 never writes target-repo source (AC-16) · v1 at `~/dev/agentalloy` untouched (AC-1).

## Test Cases

### T1 — Scaffold (AC-1, AC-15)
- **TC-1.1** Given the fresh repo, `just ci` runs lint (ruff+clippy) + typecheck (mypy --strict) + tests (pytest+cargo test) and exits 0 on the empty skeleton.
- **TC-1.2** Given `import newagent`, no module from v1's `src/agentalloy` package is importable or referenced by v2 code (AC-1); the `newagent` CLI entry point resolves (`newagent --help`).
- **TC-1.3** Given `.env` unset, config falls back to the dev-port defaults (:48950/:48951/:48952/:50001); given `V2_SERVICE_PORT=47950`, the same code binds 47950 — ports are env-driven, never hard-coded (the flip is config-only).

### T2 — Env prereqs + ColBERT spike
- **TC-2.1** Given the runbook commands executed on a clean box, llama-server serves embeddings on :48951 and rerank on :48952 while v1's :47951/:47952 keep serving unchanged (two independent server processes, no port conflict).
- **TC-2.2** Given a ColBERT query sent with `--pooling none`, the recorded probe output shows either per-token (seq×dim) vectors or pooled — and the design §9 decision (primary / Fallback-A / Fallback-B) is written with that evidence (a recorded artifact, not a guess).
- **TC-2.3** The Rust toolchain installs via mise (`mise exec -- cargo --version` succeeds) — re-verifying the T1 prerequisite, since TC-1.1's clippy + cargo test already require it — and `just build` compiles `newagent-core`.

### T3 — Code index (AC-6, AC-7, AC-13)
- **TC-3.1** Given the newagent repo indexed (self-index), a semantic search for "client that talks to the LLM endpoint" returns the spike's `Client.chat` (or its v2 successor) in top-k — real data, not canned (AC-6).
- **TC-3.2** Given a symbol lookup by exact FQN, the correct symbol (file, line, kind) is returned; given a non-existent FQN, a structured not-found is returned — no fabricated symbol is ever returned (AC-13).
- **TC-3.3** Given hybrid search, a query matching code lexically but not semantically (e.g. a distinctive identifier) still returns the right symbol — the BM25 leg contributes (both legs verified independently, then fused).
- **TC-3.4** Given validation of `symbols` with a real FQN → ok; with a hallucinated-but-plausible FQN → not ok with an error message usable for the fill retry; with the store closed/erroring → the check PASSES (fail-open, the executor decides) — a flaky dependency never blocks a schema-valid question.
- **TC-3.5** Given MRL slicing, a 1024-dim embed is stored as 768-dim L2-normalized; the similarity math is inner product (pre-normalized), not cosine (assert on stored vector norm ≈ 1 and the scoring function).
- **TC-3.6** (offline) All T3 cargo tests run with a fake embed source and a temp overgraph dir — no live model (AC-12).

### T4 — Tool-calling reliability (AC-4 de-risk; GATE; R1)
- **TC-4.1** Given the ~30–40 single-turn scenarios, ≥90% produce a full-correct tool call (right tool + schema-valid required args); zero schema-invalid calls.
- **TC-4.2** Given a `phase_advance` scenario for an in-gate workflow, 100% of proposals target the single legal gate-passing phase with the correct `approved` flag; a blocked-advance scenario produces no skipping proposal (gates enforced downstream, but the LFM must not propose illegal jumps).
- **TC-4.3** Given a non-action request ("summarize the code"), the model emits NO tool call (abstain = the old `none`) — the routing balance holds (read vs state vs abstain).
- **TC-4.4 (multi-turn, the new R1 risk)** Given a first tool result that is insufficient, the model issues the correct NEXT tool call (not a premature final answer); given a sufficient result, it finishes with a final answer (not a redundant call) — ≥80% correct next-step decisions. The spike validated single-turn only.
- **TC-4.5** The harness is re-runnable as a script (the M3 in-CI gate reuses it) and its pass/fail report is machine-readable (a JSON summary, not prose).
- **TC-4.6 (gate)** T8 cannot start while this harness's latest run is failing — enforced by plan sequencing + build refusing to advance the cursor past T4 without a green run recorded.

### T5 — Interpreter tool-calling loop (AC-2, AC-4, AC-8, AC-12, AC-13; R1)
- **TC-5.1** Given `newagent ask "<real question about newagent>"` with a live :50001, the JSON response has a non-empty `answer` and a `steps[]` trace (each step = a tool call + its result) (AC-2); with a `FakeLM`, the same flow runs offline (AC-12).
- **TC-5.2 (exits)** Given a model that emits no tool call → stop_reason=answer (abstain/terminate); a model that keeps calling tools → stop_reason=step_budget at the cap; a model repeating the same (tool,args) → stop_reason=duplicate with NO second execution; a tool execution error → the error returns to the model as a tool result, and if it persists the loop degrades (stop_reason=tool_failed, degraded=true); a schema-invalid call after the single retry → stop_reason=validation_failed, degraded (AC-8: every exit named, no path to "feel done").
- **TC-5.3 (budget semantics)** A multi-step state op (artifact_record then phase_advance) counts as ONE interpreter invocation for the step budget (budget counts model turns; resets at sub-graph entry, per design Q15).
- **TC-5.4 (validation fail)** Given a tool call naming a non-existent symbol, the index-grounded check fails BEFORE execution, the error is fed back as a tool result, and if it repeats the response is degraded with the reason + the steps already retrieved — no fabricated entity is executed (AC-13).
- **TC-5.5 (fail-open LM)** Given an unreachable :50001, `ask` returns a structured error `{error, reason}` — not a traceback (AC-13/AC-12 posture).
- **TC-5.6 (tool-calling transport)** The :50001 chat request carries the full 12-tool `tools` array (9 read + 3 state — no `contract_read`, R2); the assistant `tool_calls` (name + JSON args) are parsed into the dispatch; tool results go back as `tool`-role messages. The two-stage classify→fill protocol is retired — no stage-A/stage-B calls exist anywhere in the loop (R1).

### T6 — Skill engine (AC-5)
- **TC-6.1** Given two tasks with different needs (e.g. "add a Rust FFI boundary" vs "write the CLI arg parser") and the minimal corpus, the engine composes two DIFFERENT skill sets — both a subset of the retrieved candidates (corpus-grounded, AC-5).
- **TC-6.2** Given the LFM selecting a skill_id NOT in the retrieved candidate set, the composition drops it (no fabricated skill is ever injected).
- **TC-6.3** Given the char budget, composition truncates lowest-relevance skills first and stays under budget; given an already-injected skill, it is not re-injected (dedupe).
- **TC-6.4** Given a phase filter, a skill whose `applies_to_phases` excludes the current phase is not retrieved (the phase leg of retrieval works).
- **TC-6.5** (offline) With `FakeLM` returning a fixed selection, composition is deterministic and testable with no live model (AC-12).

### T7 — Checkpointer + stores (AC-11 base, AC-6 state/telemetry)
- **TC-7.1** Given a LangGraph run with the DuckDB checkpointer, the checkpoint tables fill with one row per transition (thread_id/checkpoint_id/parent_id chain intact).
- **TC-7.2 (single-RW enforcement)** Given the service running (Python RW handle open), a Rust `DataLayer` opened on the same `.duck` file in read-only mode reads current state without error; a second RW open attempt — made from a SEPARATE process, since DuckDB's single-RW is a cross-process file lock that two handles in one pytest process don't reproduce — fails (DuckDB constraint); and NO code path in v2 opens a second RW handle (the enforcement test asserts Rust opens RO-only, AC-6/AC-7 boundary).
- **TC-7.3** Given `contract_add` + `artifact_record` (Python writes), a Rust `get_contract`/`list_artifacts` (RO read) returns them — the Python-writes/Rust-reads flow works end-to-end.
- **TC-7.4** Given interpreter traces written to DuckDB, the `telemetry` action (Rust RO read) returns them with the expected fields (AC-6).
- **TC-7.5 (interpreter→store wiring)** Given the interpreter loop with its state executors wired to the real stores (the T7/T8 wiring of the T5 store interface), FakeLM-driven `contract_add` + `artifact_record` tool calls executed through the loop (offline) PERSIST — Rust RO `get_contract`/`list_artifacts` return them; a rejected state op (validation-failed after the single retry) leaves NO rows — a rejected state op is never applied.

### T8 — Phase machine (AC-3, AC-9, AC-10, AC-11)
- **TC-8.1 (gate: artifact required)** Given plan with no recorded exit artifact, advance to build is REFUSED (named reason), even if approved=true is passed.
- **TC-8.2 (gate: void-on-edit)** Given a spec approved, then the spec artifact edited, the approval no longer matches (digest mismatch) and advance is refused until re-approval (AC-9).
- **TC-8.3 (gate: --force never bypasses)** Given a blocked gate, a `--force`-equivalent request does not advance (AC-9) — force is not a resume/approve path.
- **TC-8.4 (single-call)** Given advance `{to_phase, route, approved:true}` with the exit artifact recorded, approval is recorded AND the transition happens in one call (no intermediate state visible).
- **TC-8.5 (named transitions only)** Given the graph, no edge leads to a phase except a gate-passing transition; every phase change in a run appears as a `workflow_event` with a named event (AC-8).
- **TC-8.6 (contracts cover tasks)** Given a plan with N tasks, the `build_contracts_cover_tasks`-style check requires ≥N build contracts, each ≤2 domain_tags, each covering a distinct task (AC-10).
- **TC-8.7 (fan-out structure)** Given N contracts, the build phase models N independent branches with per-task state; the work-item cursor tracks which task is current (structural at M1 — driver --parallel 1; concurrency is the harness's, AC-10).
- **TC-8.8 (sessions)** Given a run stashed, then the process killed, then the service restarted and `resume`, the workflow continues from the exact recorded position — same phase, same task cursor, same approval state (AC-11); `archive`/`cancel` are terminal (no resume after).

### T9 — Steering proxy + REST/CLI (AC-2-REST, AC-3-demo, AC-14)
- **TC-9.1** Given `POST :48950/ask` (same request as CLI), the same `{answer, steps[]}` JSON is returned — the ask surface is CLI + REST (AC-2).
- **TC-9.2 (injection)** Given a harness chat request through `/proj/<id>/v1`, the outgoing request carries the AGENTALLOY-BANNER + AGENTALLOY-STATE + AGENTALLOY-INSTRUCTIONS blocks; given a repeat request, the prior blocks are STRIP-REPLACED (no duplicate markers accumulate) — the non-phase-stamped marker families work.
- **TC-9.3 (passthrough)** Given a model response, the proxy returns it to the harness unchanged (injection is request-side only).
- **TC-9.4 (port flip)** Given `V2_SERVICE_PORT=47950` and the same code, the service binds :47950 and the harness config change is a port number only (AC-14 drop-in).
- **TC-9.5 (contract schema)** Given a contract with `domain_tags` missing or >2, creation is rejected (required, ≤2); given a valid one, it is stored with its digest.

### T10 — Integration + AC verification (AC-3, AC-9, AC-10, AC-11, AC-13, AC-15, AC-16)
- **TC-10.1 (full lane)** Given a small real task in the newagent repo, the full main lane intake→spec→design→plan→build→qa→ship completes with the REAL harness performing the code edits under the per-task contracts, steered via the dev-port proxy; v2 performs orchestration only (AC-3).
- **TC-10.2 (resume across kill)** The TC-8.8 scenario run for real (process actually killed between stash and resume).
- **TC-10.3 (read-only guarantee)** Given a source-tree hash before the full-lane run and a hash after, v2's writes are confined to its own stores — the only source changes are the harness's own edits (AC-16).
- **TC-10.4 (model down)** With :50001 stopped, `ask` and a workflow LM step return structured errors, not crashes (AC-13).
- **TC-10.5 (degraded, not fabricated)** The TC-5.4 scenario observed at the service level (degraded response with reason + partial steps).
- **TC-10.6 (v1 untouched)** At the end of the whole build, `~/dev/agentalloy` has zero modifications (git clean) and no v2 process depends on it (AC-1).

### T11 — M1.x ColBERT (conditional; AC-6, AC-14)
- **TC-11.1** Given per-token doc vectors precomputed and a per-token query vector, MaxSim (Rust, rayon) scores the fused candidates and the top-k changes measurably vs dense+BM25 on the probe set (the second stage is wired and effective).
- **TC-11.2** If T2 chose the primary server path, this task is closed no-op with the rationale recorded (not silently skipped).

## AC coverage map

- AC-1: TC-1.1/1.2, TC-10.6 · AC-2: TC-5.1, TC-9.1 · AC-3: TC-10.1, TC-8.1–8.8 · AC-4: TC-4.1–4.4, TC-5.2–5.4 · AC-5: TC-6.1–6.4 · AC-6: TC-3.1, TC-7.3, TC-7.4 · AC-7: TC-7.2 (boundary/RO enforcement) · AC-8: TC-5.2, TC-5.3, TC-8.5 · AC-9: TC-8.1–8.4 · AC-10: TC-8.6, TC-8.7 · AC-11: TC-7.1, TC-8.8, TC-10.2 · AC-12: TC-3.6, TC-5.1, TC-6.5 (offline invariance) · AC-13: TC-3.2, TC-3.4, TC-5.4, TC-5.5, TC-10.4, TC-10.5 · AC-14: TC-1.3, TC-9.4 · AC-15: TC-1.1 (+ every task) · AC-16: TC-10.3.
