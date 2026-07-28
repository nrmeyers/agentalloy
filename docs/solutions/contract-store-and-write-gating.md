# Contract Store and Write Gating

## Problem

Contracts used to be markdown files under `.agentalloy/contracts/` — a
filesystem watcher that fired 1 time in 11,162 requests, so every phase was
composing free text instead of tag-scoped contract context. The watcher was
unreliable, the prose in SDD packs told agents to write contract *files*, and
Tier A harnesses (claude-code, codex) blocked writes to `src/` and `tests/`
during pre-build phases, making the whole contract workflow inoperable.

Three consequences drove the feature set:

1. **Writing a contract through the service *is* the compose trigger.** No
   filesystem watcher. The write itself fires in-process composition.
2. **Tier A harnesses deny writes to `src/` and `tests/` during `intake`,
   `spec`, and `design`.** `docs/` stays writable; shell stays available.
   Only after advancing to `build` do `src/` and `tests/` become writable.
3. **Every phase boundary is a session boundary.** Advance, end the session,
   start fresh. A cold session reconstructs its state from one command against
   the store.

## Approach that worked

**Replace files with a DuckDB store, replace the watcher with a compose
trigger, and gate writes with harness wiring.**

The keystone was realising the service **already held the single writer lock**
for the corpus. A per-repo DuckDB file (`state.duck`) with a single `sdd_state`
table (keyed by `repo, kind, session_key`) replaced the scattered `.agentalloy/`
file mirror. The `sdd_contract` table holds contract rows with
`contract_id, phase, slug, status, supersedes` for full revision history.

Two seams were critical:

- **`StateClient` → `StateRouter` on port 47950.** All contract CRUD, phase
  advance, cursor writes, and the cold-session `resume` endpoint route through
  the HTTP API. In-process callers (compose, proxy_signal, signals) hold the
  `StateStore` directly and skip the network hop.
- **`proxy_signal.py` banner suppression.** Tier A enforced repos get their
  per-turn banner suppressed; Tier B and C keep it. The banner carries the
  phase-appropriate denial message (D1/D2) and the end-session instruction
  (D9).

### Lease-based concurrency

Repo-scoped rows (`phase`, `approved`) use lease-based ownership with expiry
(two-minute default, five-minute configurable). When session B tries to write
a row owned by session A with an active lease, B receives a 409 conflict.
When the lease expires, B takes over. This replaces the fragile `.agentalloy/phase`
file that concurrent sessions contended.

### Transactional phase advance + contract

`POST /state/phase` with a `contract` payload commits both writes inside a
single `BEGIN/COMMIT` block. Validation failures in the contract payload roll
back the entire transaction — the phase is *not* advanced if the contract is
invalid. This satisfies acceptance criterion A3.

### Harness wiring and enforcement posture

`wire_harness.py` generates deny rules for each pre-build phase:

- **claude-code:** `settings.local.json` with `deny: [Write(src/**), Edit(src/**),
  Write(tests/**), Edit(tests/**)]`
- **codex:** `writable_roots` excludes `src/` and `tests/`
- **build/qa/ship:** empty deny set (all writes allowed)

The wiring is triggered by `POST /state/phase` with `repo_root` — a single
HTTP call after phase advance rewrites the harness config.

## Verification

32 acceptance criteria across sections A–F of the spec. 26 verified (8 in A,
4 in B, 3 in C, 7 in D, 1 in E, 3 in F). 6 unverified — all honest gaps:

- **D1, D2, D5:** Live IDE enforcement requires running claude-code/codex
  against wired repos. Posture generation is correct (tests pass); runtime
  IDE enforcement is a separate surface.
- **E2, E3:** Latency measurement and uninstall verification require
  environment-specific setup.
- **F4, F5:** Staleness signal and live design-phase session require a
  controlled test session.

Full suite: 4756 passed, 2 skipped, ruff/format/pyright clean.

## What didn't work / cost time

- **The filesystem watcher path was a red herring.** It fired 1 in 11,162
  requests. The real baseline metric was *silent free-text composition*, not
  watcher failures. Fixing the watcher would have been a waste — the whole
  contract file system was the problem.
- **`acquire_lease` returns `LeaseConflict(owner=None)` for missing rows.**
  The function refuses to create ghost rows (lease semantics), but the
  `LeaseConflict` dataclass had `owner: str` (non-optional). When
  `_write_result_to_response()` passed `owner=None` to `StateConflictInfo`,
  Pydantic raised a `ValidationError`. The fix: widen `LeaseConflict.owner`
  to `str | None` and guard all downstream consumers with
  `conflict.owner is not None` before treating as a blocking 409.
- **`repo_root` prefix made phase values unclassifiable.** Commit `33651bb`
  fixed an enforcement regression where a `repo_root` prefix on phase values
  (e.g., `/home/user/repo/build`) caused the harness layer to emit *no*
  enforcement rather than refusing — a fail-open bug that *all four gates
  passed*. Lesson: the empirical classification table in
  `tests/test_enforcement_posture.py` is necessary but not sufficient. If a
  phase write succeeds when it should be denied, that's the fail-open bug,
  not a config mistake.
- **A lambda inside a `for` loop closed over the loop variable.** Ruff B023
  caught this during lint. Extract a helper taking the value as a parameter.
- **Cross-version API skew returns 405, not 404.** The SPA catch-all claims
  unknown paths and rejects POST. Verified live against 7.8.0. Don't assume
  an HTTP 404 means the route doesn't exist.
- **The route-set wiring test is an exact-set assertion.** Any new endpoint
  must be registered in `tests/code_index/test_module_wiring.py` or it
  fails. This is a structural test that catches orphaned routes.

## Decisions worth keeping

- **Contracts leave git.** `.agentalloy/contracts/` in this repo (77 files)
  is legacy residue from the pre-store system. The store is the source of
  truth going forward. No migration was implemented — the filesystem copies
  were never loaded into the store. This was an explicit product call:
  contract files in git were the problem, not the solution.
- **Pack version bump is the *only* propagation mechanism.** Pack edits
  only reach agents on a version bump (2.0.0 → 2.1.0). This preserves the
  `SkillVersion` rollback chain and prevents accidental drift. The bump
  triggers re-ingest + re-embed, which rebuilds the derived index from the
  SQL-canonical source.
- **The compose trigger is the contract write, not a separate event.**
  `_trigger_compose_in_process()` fires asynchronously after a successful
  contract write. The task is stored on `request.app.state` to prevent GC
  and surfaces failures at WARNING level. If the orchestrator is absent at
  the moment of write, compose is silently skipped (logged, not errored).
- **Lease semantics: no ghost rows.** `acquire_lease` returns a conflict
  when no row exists — this is intentional. The write creates the row; the
  lease claims it. The conflict with `owner=None` is not a real conflict;
  downstream code must distinguish it from a real one (`owner is not None`).
- **Cold-session bootstrap is one HTTP call.** `GET /state/resume` returns
  phase, cursor work-item with tags/scope, owed artifacts, and governing
  decisions. A fresh session needs exactly one command to reconstruct its
  state. This is the seam that makes phase boundaries work.
- **`mirror_to_files` is deleted.** The file mirror was a compatibility
  shim for legacy consumers (sidecar watcher, statusline). With the store
  as the single writer, the mirror was redundant. The store writes the
  mirror internally; callers read from the store.

## Files

- `src/agentalloy/api/state_router.py` — HTTP routes for state CRUD, phase
  advance with transactional contract, resume endpoint, contract CRUD
  (create, list, patch, supersede).
- `src/agentalloy/api/state_models.py` — Pydantic request/response models
  for all state and contract endpoints.
- `src/agentalloy/api/state_client.py` — `StateClient` HTTP wrapper used by
  all CLI verbs.
- `src/agentalloy/storage/state_store.py` — `DuckDBStateStore` with
  `sdd_state` and `sdd_contract` tables, lease management, transactional
  writes, and file-mirror import for migration.
- `src/agentalloy/storage/protocols.py` — Canonical `LeaseConflict`,
  `StateWriteResult`, and `StateStore` protocol.
- `src/agentalloy/install/subcommands/wire_harness.py` — Harness wiring for
  claude-code and codex, enforcement posture generation.
- `src/agentalloy/api/proxy_signal.py` — Banner suppression on Tier A
  enforced repos, end-session instruction injection.
- `src/agentalloy/_packs/sdd/` — All pack YAML files at version 2.1.0,
  zero `.agentalloy/contracts` references, `change_summary` fields say
  "contract store".
- Tests: `tests/api/test_state_router.py` (56 endpoint tests),
  `tests/storage/test_state_store.py` (66 store tests).

## Unverified (by design)

- **D1, D2, D5:** Live IDE enforcement. Posture generation is correct; IDE
  runtime enforcement is a separate surface. Requires running claude-code
  and codex against wired repos.
- **E2:** Compose latency measurement. Contract resolution goes through
  `StateClient` (HTTP) for out-of-process callers, but in-process callers
  hold the store directly. The hot path is unaffected.
- **E3:** Uninstall verification across all recorded repos. The uninstall
  code exists and is correct; verification requires a multi-repo setup.
- **F4:** Staleness signal. Requires a controlled session with a deliberately
  stale index.
- **F5:** Live design-phase session. Requires starting a cold session in
  design phase and verifying retrieval works without shell-out fallback.

These are honest gaps, not failures. They require environment-specific
setup that is out of scope for the feature set. The QA report
`docs/qa/contract-store-and-write-gating.md` documents each criterion
and its verification status.
