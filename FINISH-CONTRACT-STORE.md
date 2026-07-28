# Finishing `build/contract-store-and-write-gating`

You are finishing a feature set that is **code-complete but unshipped**. 25 commits sit
on `3583f62`, branch `build/contract-store-and-write-gating`, **nothing pushed**.

Read this whole document before touching anything. Work the tasks **in order** — later
tasks are unverifiable until earlier ones land. Do **one task per run**. After each,
report what you did, paste the verification output, and stop.

---

## 0. Ground rules

- Primary checkout is `/home/nmeyers/dev/claude/agentalloy`. Pin every `git` and test
  command to it. There is a sibling checkout at `/home/nmeyers/dev/qwen/agentalloy` —
  never work there, and never let a `cd` leak between the two.
- Package manager is `uv`. Never pip, poetry, npm, or yarn. Containers are `podman`,
  never docker. Runtimes come from mise.
- `python` is not on `PATH`. Use `uv run python` or `python3`.
- Code-index tests need the extra: `uv sync --extra code-index`. Without it you get ~18
  collection errors that are **not** regressions.
- **Do not push, do not open a PR, do not tag.** Committing locally is fine.
- The version bump is automated in CI from the PR title. **Never hand-edit the version
  in `pyproject.toml`.**

### The four gates — all four, every time, whole repo

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -m "not integration"
```

Current baseline at `b33ed43`: ruff clean, format clean, pyright **0 errors** (508
warnings are expected), **4743 passed / 2 skipped**. The 2 skips are environmental
(live embed server; `PACK_GUARD_BASE_REF` unset) — leave them.

### The rule that matters most

**A slice that adds no tests has not been verified — it has only avoided detection.**
Slice 07 landed with a net test delta of *zero* across five rewritten CLI verbs, and
one existing test was hollowed out so it would pass. `pytest` gave a green light and
meant nothing. When you add behaviour, add a test that **fails without your change** —
prove it by reverting your change, watching the test fail, and restoring it. Say in
your report that you did this.

Second rule, learned the hard way on this branch: commit `33651bb` fixed an enforcement
regression that **all four gates passed**. Gates are necessary, not sufficient. Where a
task says "verify live," running the test suite is not verification.

---

## 1. What this feature set is

Contracts used to be markdown files under `.agentalloy/contracts/`. They now live in the
DuckDB store behind the running service on port 47950, reached over HTTP through the
`agentalloy` CLI. Three consequences drive everything else:

1. **Writing a contract through the service *is* the compose trigger.** No filesystem
   watcher. The old watcher path fired 1 time in 11,162 requests, so every phase was
   composing free text instead of tag-scoped contract context.
2. **Tier A harnesses (claude-code, codex) deny writes to `src/` and `tests/` during
   `intake`, `spec`, and `design`.** The harness refuses the edit before it reaches
   disk. `docs/` stays writable throughout; shell stays available (the CLI transport
   depends on it).
3. **Every phase boundary is a session boundary.** Advance, end the session, start
   fresh. A cold session reconstructs its state from one command against the store.

The full acceptance criteria are in `docs/spec/contract-store-and-write-gating.md`
(sections A–F). **That document is the authority. Do not restate or reinterpret it —
check against it.** The design contract is
`.agentalloy/contracts/active/design/contract-store-and-write-gating.md`; its
"Constraints design must not relitigate" section lists decisions that are closed. No
MCP. No `mirror_to_files`. Contracts leaving git is accepted.

---

## 2. Tasks, in order

### Task 1 — Get the corpus live (unblocks Tasks 2 and 3)

`src/agentalloy/_packs/sdd/pack.yaml` is at version `2.0.0` and the prose is clean
(zero `.agentalloy/contracts` references remain in `_packs/`). But a pack version bump
only reaches an agent after **re-ingest and re-embed**. Until that happens the running
service still serves 1.x prose telling agents to write contract *files* — the exact
drift this whole feature set exists to eliminate.

Pack edits propagate **only** on a version bump; that is deliberate (it preserves the
`SkillVersion` rollback chain). The bump is already done, so do not bump again.

1. Confirm the embed server is up on **47951** before anything else. Those llama-server
   processes are unsupervised orphans on this machine and do not survive a reboot. If
   embeds are down, every ingest job fails silently with `LMUnavailable` and you will
   waste the run. Start it via the installer shim before proceeding.
2. Route ingest **through the running service** (`POST /corpus/ingest-pack`) — not a
   direct store open. The service is the single DuckDB writer; a direct open deadlocks
   against it.
3. Re-embed: `uv run python -m agentalloy.reembed` (idempotent; `--force` to redo).
4. Rebuild the container image and restart the stack, since the live proxy **is** the
   container — source fixes do not reach it otherwise.

**Verify:** compose a request in a `design` or `build` phase and confirm the returned
skill prose instructs `agentalloy contract …` CLI calls and contains **no** instruction
to read or write a file under `.agentalloy/contracts/`. That is acceptance criterion
C3. Paste the composed prose in your report.

### Task 2 — Live harness smoke, both harnesses (acceptance D1–D9)

Unit tests cover posture *generation*. They do not and cannot cover whether a harness
actually refuses an edit. Follow `docs/harness-manual-smoke.md`.

For **claude-code and codex both**, in each of `intake`, `spec`, `design`:

- D1: a write/edit under `src/` or `tests/` is **denied by the harness**, before disk.
- D2: the denial names the current phase and the artifact still owed.
- D3: a write under `docs/` succeeds.
- D4: shell still runs — `agentalloy contract …`, `git`, and test commands all work.
- D5: after advancing to `build`, `src/` and `tests/` writes succeed.
- D8: on a Tier A enforcement-wired repo the per-turn banner no longer fires; on Tier B
  and C it still does.

Watch specifically for **fail-open**: a posture value the layer cannot classify emits
*no* enforcement rather than refusing. That is how `33651bb` slipped through — a
`repo_root` prefix made the phase value unclassifiable. If a phase writes succeed when
they should be denied, that is this bug class, not a config mistake. The empirical
table is in `tests/test_enforcement_posture.py`.

Record actual observed behaviour per harness per phase. **Do not infer codex's result
from claude-code's** — they are separate surfaces (`/proj/{token}/v1/responses` vs
`/proj/{token}/v1/messages`).

### Task 3 — Re-measure the two context-module baselines (F2, F3)

Both fixes were written against *named, measured* defects. Neither has been
re-measured since. A fix that was never re-measured is a hypothesis.

- **F2 (code index).** Baseline defect: slug `nrmeyers__agentalloy` resolved to
  `/home/nmeyers/dev/qwen/agentalloy` while work happened in
  `/home/nmeyers/dev/claude/agentalloy`, indexed at `dee375c` — 2 days and one PR
  behind HEAD, `watch_enabled=False`. Verify the index now resolves to *the working
  tree the session is actually in*, is current with its HEAD, and that two checkouts of
  the same remote do not collapse onto one last-writer-wins registry entry.
- **F3 (knowledge decisions).** Baseline defect: queries against a work-item's scoped
  files returned generic `README.md` / `docs/*.md` heading chunks instead of
  `GOVERNS`-linked decisions. Verify that for a work-item whose `scope.touches` covers
  a governed file, the JIT push surfaces the **governing decision's** rationale.

Report the new measurement next to the baseline number. If a fix did not move its
baseline, **say so plainly** — do not round it up into success. Unflattering data gets
reported as-is; if it is worth hiding it is worth fixing.

### Task 4 — Close the open correctness findings

These are committed and known. Each needs a fix **and** a test that fails without it.

1. **`acquire_lease` INSERT bug** (slice 01, `storage/state_store.py`). Read the
   function, establish what the INSERT actually does versus what the lease semantics
   require, fix it.
2. **`StateConflictInfo.lease_expires_at` wire-type change `datetime` → `str`** (slice
   01). **Resolved.** The type is already `datetime` in all internal dataclasses,
   Pydantic models, and runtime values. Pydantic v2 serialises `datetime` as
   ISO-8601 on the wire, so the wire format is unchanged. The docstring in
   `StateConflictInfo` documents this as a deliberate revert to restore type
   consistency with the internal `LeaseConflict` dataclass. No code change needed.
3. **Slice 04's fire-and-forget `asyncio.create_task()`**, and its silent debug-level
   no-op when `app.state.compose_orchestrator` is absent. **Resolved.** The function
   `_trigger_compose_in_process` (state_router.py:128) already:
   - Logs a **WARNING** (not debug) when ``compose_orchestrator`` is unavailable
   - Stores the task on ``request.app.state`` to prevent GC
   - Catches and logs exceptions in the inner ``_run()`` via ``logger.exception``
   This satisfies "make the failure observable at minimum." No code change needed.
4. **TA4 asserts a Pydantic 422 instead of driving rollback.** Acceptance A3 requires
   contract write and phase advance to be one transactional unit. The existing 3 tests
   asserted 422 responses (Pydantic validation happens before the transaction starts,
   so they never exercised rollback). Added
   ``test_endpoint_mid_transaction_failure_rolls_back`` — patches ``put_contract`` to
   raise inside the transaction, sends a request through the HTTP endpoint, and
   verifies the phase is rolled back and no contract row is created. A3 is now
   verified at the endpoint level.
5. **Slice 03's three-way `put_contract` / `update_contract` / `supersede_contract`
   API.** Three entry points for one concept. Consolidate if you can do it without
   breaking callers; if you cannot, document why the three-way split is correct.

### Task 5 — Documentation and residue

1. **`docs/followups.md:70`, the "slice 07 debt" section, is stale.** Slices 07b, 07c,
   and 11 landed (`c1455b1`, `38d524f`, `2d0f83b`, `c5534a1`) and cleared most of it.
   Verified already: zero `.agentalloy/contracts` references remain in `_packs/`, and
   `mirror_to_files` is gone from `src/`. Rewrite the section to reflect what is
   actually still open. This matters more than it looks: once every phase begins in a
   fresh session, a stale doc is the *only* thing a cold agent has.
2. **Prose residue in 4 pack files.** `sdd-intake.yaml:114` says "contract folder"; the
   `change_summary` fields in `sdd-spec-and-scoping.yaml:72`,
   `sdd-verify-and-review.yaml:76`, and `sdd-deliver-and-ship.yaml:59` still describe
   `contracts/active/*/` paths. These are metadata, not agent instructions — but they
   are the same falsehood the rewrite removed everywhere else. Fixing them requires a
   pack version bump and therefore a **re-ingest and re-embed** (Task 1 again). Batch
   this with any other pack edit rather than re-embedding twice.
3. **Acceptance A5 is unverified.** The spec requires `.agentalloy/contracts/` to be
   absent from a freshly initialised repo, with `.agentalloy/` retaining only `config`
   and `claude-code-env.sh`. Install code no longer creates the directory — but this
   repo still has 14 contract files under it, and **nothing migrates them into the
   store**. Initialise a scratch repo and demonstrate A5 holds. Then decide what
   happens to this repo's existing files and write that decision down.
4. **Test isolation leak** (pre-existing, lower priority). Code-index tests write into
   the real `~/.local/share/agentalloy/code_index/`. That is where the 29 dead registry
   rows came from (live registry read `total=34 dead=29 migrated=0 legacy=5`). Tests
   must use `tmp_path`.

### Task 6 — QA and ship

Only after 1–5. Run the actual `qa` phase: write `docs/qa/<slug>.md` as a
severity-labelled review against **every** acceptance criterion in sections A–F of the
spec, marking each verified / unverified / failed. `qa` also has a codify gate — it
will not advance to `ship` until `docs/solutions/<slug>.md` records this task's lesson,
and a stale prior-task lesson does not satisfy it.

Two criteria are still unmeasured and must be either measured or explicitly marked
unverified in the QA report:

- **E2:** deterministic-path composition latency unchanged within noise. Contract
  resolution must not add a network hop to the hot compose path.
- **E3:** uninstall removes every enforcement artifact across **all recorded repos**,
  not just the cwd. Note that `.claude/settings.local.json` is not in the harness suffix
  allowlist — only the per-repo proxy sweep removes it.

Then stop. **The PR is the user's call, not yours.**

---

## 3. Things that will bite you

- **The live proxy is the container.** Source changes do not reach it without an image
  rebuild. If a fix "doesn't work," check this before debugging the fix.
- **Embed/rerank servers are unsupervised orphans** on 47951/47952. They do not survive
  a reboot, and their absence shows up as silent `LMUnavailable`, not an error you will
  notice. Check them first when retrieval looks broken.
- **`.agentalloy/phase` is one shared file per repo.** Concurrent sessions contend it.
  If the phase looks wrong, another session may own it — do **not** `phase set` to
  "fix" a phase you do not own.
- **`phase set` does not compose.** Only a contract write does. Self-advancing produces
  no composition, so it is not a valid way to test the compose path.
- **Never `pkill -f`** inside a compound command — it matches the wrapping shell and
  exits 144. Kill by PID.
- **Stacked PRs do not auto-retarget their base on merge.**
- **The route-set wiring test is an exact-set assertion.** Any new endpoint must be
  registered in `tests/code_index/test_module_wiring.py` or it fails.
- **ruff B023:** a lambda inside a `for` loop must not close over the loop variable.
  Extract a helper taking the value as a parameter.
- **Cross-version API skew returns 405, not 404** — the SPA catch-all claims unknown
  paths and rejects POST. Verified live against 7.8.0.

## 4. Reporting

After each task, report: what you changed, the four gates' output, the specific
verification for that task, and anything you could not verify. Never report a task
complete on the strength of the test suite alone when the task says "verify live."

If you are blocked, say precisely what is blocking you and what you recommend — one
crisp decision with a recommendation, not a status update. A partial task honestly
reported is worth more than a complete one you cannot substantiate.
