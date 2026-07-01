# Combined Spec: Web UI + `add-skill` Intake Lane

**Baseline: v5.0.3 (`7b1e3d2`).** Every file:line anchor in this document was verified
against that commit.

**Supersedes:**
- `web-ui-spec.md` (MVP 4-page spec, written pre-v5 — Coverage tab and LadybugDB
  references are stale)
- `web-ui-roadmap.md` (post-MVP feature tiers, grounded at #255)
- `add-skill-workflow-lane-spec.md` (authored in a sibling worktree against v4.0.4,
  unpushed there; absorbed here with its three open questions resolved)

`web-ui-design.md` remains useful as frontend implementation reference (component
structure, React Query patterns, Vite/build integration) but its backend claims
(LadybugDB, coverage endpoint, config field list) are stale — trust this document.

**External dependency (blocking, see §4):** the custom-skill mechanical rail —
`new-skill-pack` (scaffold), `validate-pack` (strict lint dry-run), `install-pack`
strict-lint + dedup gate — exists only in an unpushed commit (`aa4929b`) in a sibling
worktree, based on **v4.0.4, which predates the v5.0.0 storage rebuild**. It must be
rebased onto 5.0.x — non-trivially, since its ingest/dedup paths target the removed
storage engine and must port to `skill_store`/`fragment_store` (LanceDB dedup queries).

---

## 1. v5.0.3 ground truth — deltas vs the superseded docs

| Area | Was (superseded docs) | Is (v5.0.3) |
|---|---|---|
| Storage | LadybugDB graph + monolithic DuckDB vector store | Three stores: `agentalloy.duck` (DuckDB: `skills`, `skill_versions`, `fragments`, `skill_dependencies`, `corpus_meta`; `storage/skill_store.py:38`), `fragments.lance` (LanceDB: 768-dim vectors + Tantivy BM25; `storage/fragment_store.py:48`), `telemetry.duck` (`storage/telemetry_store.py:33`) |
| Version history | Assumed lost in rebuild | **Intact**: `skill_versions` rows (version_number, authored_at, author, change_summary, status, raw_prose) + `skills.current_version_id` with consistency guard (`reads/active.py`) |
| Telemetry endpoints | traces + savings + coverage | `/telemetry/coverage` **removed**; traces + savings only (`api/telemetry_router.py:138,165`) |
| Trace columns | Stage B scores discarded | `lm_assist_kept_ids`, `lm_assist_dropped_ids`, `lm_assist_scores` now persisted per trace |
| Signal CLI | `agentalloy signal …` subcommand | **Removed**; logic lives in `api/proxy_signal.py:evaluate_signal()`, runs on every proxy request |
| Anthropic proxy | `/v1/messages` translation shim | Shim removed; Claude Code uses per-repo passthrough `/proj/{token}/v1/messages` only |
| Upstream config | Global `.env` only | Per-repo adoption: `agentalloy add <harness>` writes `.agentalloy/upstream` (url/model/key_env); per-repo wins over global |
| CLI | 43 subcommands | 50: +`add`, +`worktree`, +`approve`, +`rerank-warmup`; `cleanup --deep`; `signal` gone |
| Workflow overrides | All fields editable via `customize` | `exit_gates`, `applies_to_phases`, `signal_keywords`, `contract_template` are **product-owned/locked** (`customize.py:176-199`); users edit `raw_prose` + `domain_tags` only. `prose_invariants` linted — customized prose must retain load-bearing commands |
| Profile store | `profile_skills` table | Same, plus `enabled` flag (stale-override soft-disable) and `customize revalidate` |
| Phase vocabulary | 8 declaration sites | **10**: + `install/mcp_server.py:60` (enum) and `:118` (validation) |
| Approval | Manual `phase set` after approve | `agentalloy approve <phase>` writes marker **and auto-advances** via `_PHASE_GRAPH` (`approve.py:96-97`) |

---

## 2. Part A — `add-skill` intake lane (backend)

A third intake-routing branch alongside `spec` (full) and `sdd-fast` (fast): a workflow
skill that guides the user's in-session coding LLM through authoring a custom skill for
the local corpus, with an unconditional human-approval exit gate. Design decisions
confirmed in the v4 spec (workflow-skill delivery, intake branch, unconditional
approval) carry over unchanged; everything below is re-anchored to v5.0.3 and the open
questions are resolved.

### A1. New phase: `add-skill`

Add to **all 10** vocabulary sites:

| # | Site | v5.0.3 anchor |
|---|---|---|
| 1 | `_VALID_PHASES` | `ingest.py:74` |
| 2 | `_PHASE_GRAPH` | `signals/gates.py:29-39` |
| 3 | `Phase` Literal | `api/compose_models.py:20` |
| 4 | `DEFAULT_K_BY_PHASE` | `api/compose_models.py:31-39` |
| 5 | `DEFAULT_MAX_TOKENS_BY_PHASE` | `api/compose_models.py:45-53` |
| 6 | `VALID_PHASES` | `install/subcommands/phase.py:23` |
| 7 | `_VALID_PHASES` | `bootstrap.py:39` |
| 8 | `_VALID_PHASES` | `api/proxy_apply.py:39` |
| 9 | Phase enum | `install/mcp_server.py:60` |
| 10 | Phase validation | `install/mcp_server.py:118` |

- `_PHASE_GRAPH["add-skill"] = "intake"` — the lane's deliverable is a locally installed
  corpus skill, not a shippable code change; on completion the session returns to intake.
  This one entry also powers `approve`'s auto-advance (`approve.py:96`:
  `nxt = _PHASE_GRAPH.get(phase, phase)`).
- `DEFAULT_K_BY_PHASE["add-skill"] = 2`, `DEFAULT_MAX_TOKENS_BY_PHASE["add-skill"] = 2048`
  (copy `sdd-fast`'s confirmed values — both are short single-purpose lanes).
- **Tripwire:** `tests/test_config_consistency.py:240` derives `_ALL_PHASES` from the
  `Phase` Literal; `:263-268` assert both `DEFAULT_*` dicts cover it. **Risk:** sites 9-10
  (mcp_server) are NOT covered by that tripwire — add a consistency assertion for the MCP
  enum as part of this change.

### A2. Intake routing: third `route` value

- `contracts.py:203-205` currently rejects anything but `full`/`fast`:
  ```python
  route = str(data.get("route") or "full").strip().lower()
  if route not in ("full", "fast"):
      raise ContractMalformed(...)
  ```
  Accept `"add-skill"` (1:1 with the phase name — no `fast`→`sdd-fast` style indirection).
- `signals/skill_loader.py` `_read_intake_route()` (:416-435) / `_intake_route_hint()`
  (:438-464): `route == "add-skill"` → hint `"add-skill"`.
- `api/proxy_signal.py:530-536` needs no change — the hint flows into
  `decide_transition()` as-is.

### A3. Intake prose: third branch in `sdd-intake.yaml`

The intake workflow skill is **`_packs/sdd/sdd-intake.yaml`** (resolves the v4 spec's
open question 1). Changes:

- Extend the route-choice prose (currently full-vs-fast at lines 93-102) with the third
  branch: when the request is "add/create/teach a custom skill to this repo's corpus"
  rather than a code change, set `route: add-skill`.
- `prose_invariants` (:19-20, currently `["agentalloy phase set spec",
  "agentalloy phase set sdd-fast"]`) gains `"agentalloy phase set add-skill"`.

### A4. New workflow skill: `_packs/sdd/sdd-add-skill.yaml`

```yaml
skill_id: sdd-add-skill
canonical_name: SDD — Add a Custom Skill (add-skill)
skill_class: workflow
applies_to_phases: [add-skill]
signal_keywords: [ready to install, looks good, approve, ship the skill, install it]
prose_invariants:
  - "agentalloy validate-pack"
  - "agentalloy approve add-skill"
exit_gates:
  all_of:
    - approval_recorded:
        since: ".agentalloy/custom-skills/**/*.yaml"
contract_template: |
  ---
  phase: add-skill
  task_slug: {{task_slug}}
  route: add-skill
  ---
  # {{task_slug}}
  ## What the skill should cover
  <one paragraph: what knowledge/process this skill teaches, and why>
raw_prose: |
  <see prose outline below>
```

**Resolved (v4 open question 2) — `since` semantics and the scaffold directory.**
`eval_approval_recorded` (`signals/predicates.py:328-357`): `since` is optional; when
empty, marker existence alone = MET. When set, an **empty glob returns NOT_MET** — the
gate blocks forever if the pack is scaffolded elsewhere. Independently,
`approve.py` refuses to write a marker when its `_EXIT_ARTIFACT_GLOB` matches nothing.
Consequence: the conventional location **`.agentalloy/custom-skills/<pack-name>/` is
required for the lane** (the v4 spec's "recommend, don't require" doesn't survive
contact with these two mechanics). `new-skill-pack`/`install-pack` still accept any path
when used outside the lane; the lane's prose states the requirement plainly.

Staleness comes free: marker mtime is compared `>=` against the newest matching
artifact, so editing the skill YAML after approval re-blocks the gate.

**`raw_prose` outline** (the actual deliverable; carried over from the v4 spec, two
v5 adjustments marked):

1. **Skill-class choice** — default `domain`; `system`/`workflow` only for operational
   rules or lifecycle-tied processes (rare, advanced).
2. **Draft** — `raw_prose` plus, for domain skills, 3+ fragments across the six-type
   taxonomy (execution/verification/rationale minimum, matching the scaffold).
3. **Self-critique against R1-R9** (`_packs/meta/sys-skill-authoring-rules.md` — cite,
   don't restate): all nine rules **with explicit per-rule N/A cases** for
   hand-authored, non-sourced skills.
4. **Scaffold + iterate** — `agentalloy new-skill-pack .agentalloy/custom-skills/<name>
   --skill-id <id> [--skill-class ...]`; fill placeholders; `agentalloy validate-pack`
   strict until zero errors. *(v5 note: directory is required, per above.)*
5. **Stop for human approval** — present the final skill content and what install will
   do; do not proceed until the human explicitly says so. The exit gate (not the prose)
   is what actually blocks advancement.
6. **Approve + install** — `agentalloy approve add-skill`, then
   `agentalloy install-pack .agentalloy/custom-skills/<name>`. *(v5 change: do NOT
   instruct a trailing `agentalloy phase set intake` — `approve` auto-advances via
   `_PHASE_GRAPH`, unlike the v4-era manual flow.)*

### A5. Approval wiring

- `signals/predicates.py:302`: `_ALWAYS_APPROVAL_PHASES = ("spec", "design")` gains
  `"add-skill"` — unconditional, NOT behind a settings flag (contrast
  `sdd_fast_require_approval`, `config.py:94`, default off).
- `install/subcommands/approve.py`:
  - `:25` `_APPROVABLE = ("spec", "design", "sdd-fast")` gains `"add-skill"`.
  - `:26-30` `_EXIT_ARTIFACT_GLOB["add-skill"] = ".agentalloy/custom-skills/**/*.yaml"`.
  - Marker format unchanged: `approver` / `approved_at` / `artifact_sha256` (sorted
    per-file digest) at `.agentalloy/approved/add-skill`.
  - Auto-advance to `intake` falls out of the A1 graph entry.

### A6. Asides and non-goals

- `verify_pack.py:204` `_VALID_PROBE_PHASES` still omits `sdd-fast` (pre-existing bug,
  confirmed at v5.0.3). While touching phase vocabulary, add both `sdd-fast` and
  `add-skill`, or explicitly re-defer with a comment.
- No changes to the rail commands' own behavior; no remote registry; no
  `authoring/` pipeline changes (maintainer pipeline remains out of scope).

### A7. Tests

- `test_config_consistency.py` passes with `add-skill` in all sites; new assertion
  covering the MCP-server enum (A1 risk).
- Intake contract `route: add-skill` → `decide_transition()` routes to `add-skill`.
- Exit gate blocks until `agentalloy approve add-skill`; stale marker (YAML edited
  after approval) re-blocks.
- `sdd-add-skill.yaml` passes `ingest._validate`/`_lint`.
- `_ALWAYS_APPROVAL_PHASES` includes `add-skill`; no settings flag disables it.
- `approve add-skill` auto-advances to `intake`.
- Verify: full `pytest -m "not integration" -q` + ruff + pyright (phase-graph blast
  radius), not just new files.

---

## 3. Part B — Web UI

### B0. Architecture (carried from web-ui-spec.md, still valid)

React SPA (Vite + Tailwind, HashRouter, React Query) served as static files by the
existing FastAPI process on **47950**; `/api/*` router alongside the 8 existing routers
(19 endpoints). Single process, lazy `npx vite build` fallback, optional `web` extra,
no auth (localhost-only), polling not streaming. See `web-ui-design.md` for component
and build details.

### B1. MVP pages (corrected to v5.0.3)

1. **Config** — GET/PUT `/api/config` + POST `/api/config/reload` over the real
   `Settings` (`config.py:50-151`): upstream (`upstream_url`, `upstream_model`,
   `upstream_api_key` masked, `anthropic_upstream_url`), embedding
   (`runtime_embed_base_url`, `runtime_embedding_model`, `embedding_provider`), runtime
   (`log_level`, `dedup_hard_threshold`, `dedup_soft_threshold`, `bounce_budget`,
   `sdd_fast_require_approval`), profile (`profile_root`, `forced_profile`),
   integrations (`code_indexer_url`), authoring (`AUTHORING_*` via `AuthoringConfig`,
   `config.py:17-34`), read-only paths (`duckdb_path`, `fragments_lance_path`,
   `telemetry_db_path`). Show per-repo upstream overrides (`.agentalloy/upstream`)
   read-only with a pointer to the repos page.
2. **Telemetry** — three tabs: Traces, Savings, and **Coverage v2**. The hook-era
   `/telemetry/coverage` endpoint is gone and its shape is not coming back; Coverage v2
   is a new aggregation over `composition_traces` — composed vs passthrough rate
   (`event_type` = `proxy_composed` / `proxy_passthrough`) per phase / repo / session,
   answering "how often does AgentAlloy actually fire, and where does it pass through?"
   Cheap GROUP BY over indexed columns. Trace expander shows the full signal/retrieval
   story, all persisted:
   `event_type`, `pre_filter_matched`, `gates_met[]`/`gates_unmet[]`, `qwen_calls`,
   `phase_gate_embed_failed`, `dense_leg_degraded`, `bm25_source`, `reranked`,
   `lm_assist_outcome` + **`lm_assist_kept_ids`/`dropped_ids`/`scores`**, `repo`,
   `session_key`, `contract_path`. Filters: phase, status, since/until, **repo,
   session** (indexed: `idx_traces_repo`, `idx_traces_session`).
3. **Diagnostics** — `/diagnostics/runtime` (store/cache consistency, per-path
   readiness) + `/diagnostics/corpus` (skill/vector counts, embedding dim).
4. **Health** — `/health` + `/readiness` (container warm-up progress).

### B2. Tier 1 — skill browser + override editor

The pain: `customize edit` → raw YAML in `$EDITOR` → `validate` → `update`, three
non-atomic commands, prose in a `raw_prose: |` block.

- **Browser** — list/filter skills by class/category/phase/pack/free-text; detail view
  with fragments and active-version metadata. **Version history** from `skill_versions`
  (intact at v5, needs an endpoint).
- **Editor** — markdown editor for `raw_prose` (+ `domain_tags` chips); server owns YAML.
  Save = validate + upsert atomically, reusing `customize.py` logic. Layer banner
  (project → profile → shipped), side-by-side diff, reset-to-default.
- **v5 constraint (changed from roadmap):** workflow fields `exit_gates`,
  `applies_to_phases`, `signal_keywords`, `contract_template` are product-owned and
  locked for overrides — render them **read-only** (pretty-printed gate tree, not an
  editor). The editable surface is `raw_prose` + `domain_tags` only.
- **`prose_invariants` linting in the editor** — a customized prose that drops a
  load-bearing command (e.g. `agentalloy approve add-skill`) fails validation; show the
  missing invariant inline. Also surface the `enabled=false` stale-override state and a
  **Revalidate** action (`customize revalidate`).
- Provenance badges: shipped pack vs custom pack (`.agentalloy/custom-skills/`, once the
  rail lands).

### B3. Tier 1 — retrieval/compose playground

- Prompt + phase + tags → ranked hits and the exact injected block. `/retrieve` and
  `/compose` exist.
- **Explain mode** — `RetrievalResult` already carries `scores_by_id`, `skills_ranked`,
  `eligible_count`, `bm25_source`, `reranked`, `lm_assist_*` in-process
  (`retrieval/domain.py:226-251`); a `debug=true` compose variant serializes it. Cheaper
  than the roadmap assumed — the struct exists, it's just not exposed.
- **Signal simulator** — the signal CLI is gone; add `POST /api/signal/evaluate`
  wrapping `proxy_signal.evaluate_signal()` to show `should_compose`, gates met/unmet,
  advisories, announce/banner state for a sample prompt against a repo's current phase.

### B4. Tier 2 — repos & phase dashboard + approval queue

- Per-repo: harness, `lifecycle_mode` (full/off), current phase, resolved profile,
  **per-repo upstream** (`.agentalloy/upstream`), **worktrees** (`agentalloy worktree`
  creates token-isolated copies), sentinel tamper state.
- **Live gate status** for the current phase with advisories.
- **Approval queue** — pending `approval_recorded` gates across repos (`spec`, `design`,
  `sdd-fast` when flagged, `add-skill` unconditional once Part A lands). Show the exit
  artifacts (globs from `_EXIT_ARTIFACT_GLOB`) with content preview, staleness badge
  (marker mtime vs artifact mtime), approver/timestamp/sha256 from the marker. One-click
  approve = marker + **auto-advance** — the UI must say "this advances the phase to X",
  because `approve` does both.
- Contract viewer + task cursor (`task next|start|status`, `contract show|validate|init`).
- Phase timeline per session from `phase_transition` trace events.

### B5. Tier 2 — packs, corpus, ops, profiles

- Packs: installed list, install/remove (`install-pack`), `verify-pack` probes inline.
- Reembed: trigger/dry-run/progress; "N unembedded fragments" badge; corpus drift from
  `agentalloy update`.
- Ops: `doctor` checks + repair; embed (47951) / reranker (47952) health + log tail;
  **`rerank-warmup` status** (KV-cache warm after restart); `upgrade --check` + upgrade;
  `cleanup --dry-run` / `--deep` preview. Constraint: the UI is served by the process it
  restarts — container deployments self-restart, native renders instructions.
- Profiles: CRUD + match-rule editor + "which profile does repo X resolve to" tester.

### B6. Tier 3

- **Custom skill creation wizard** — human-driven twin of the Part A lane, same rails:
  scaffold form (`new-skill-pack` into `.agentalloy/custom-skills/`), B2 editor for
  drafting, R1-R9 checklist with per-rule N/A, `validate-pack` inline with dedup
  preview, approval via B4 queue, `install-pack` + reembed progress. **Blocked on the
  rail rebase (§4 step 0) and Part A.**
- Authoring console (maintainer pipeline review queue) — after the authoring redesign.
- Eval dashboard (`eval.recall`, `eval.intent_bench` trends).
- Live activity feed (proxy_composed/passthrough tail; composed text still not
  persisted — opt-in persistence or on-demand re-compose).

### B7. New/changed API endpoints

| Need | Status at v5.0.3 | Proposed |
|---|---|---|
| Config read/write | absent | `GET/PUT /api/config`, `POST /api/config/reload` |
| List/filter skills | only `GET /skills/{id}` | `GET /api/skills?class=&category=&phase=&pack=&q=` |
| Version history | in `skill_versions`, unexposed | `GET /api/skills/{id}/versions` |
| Override read/write | CLI-only | `GET/PUT/DELETE /api/skills/{id}/override?layer=` (PUT validates + upserts; enforces locked fields + prose_invariants); `GET .../override/diff` |
| Signal evaluation | in-proxy only (CLI removed) | `POST /api/signal/evaluate` |
| Retrieval explain | in-process `RetrievalResult`, unexposed | `POST /compose` `debug=true` |
| Wired repos / state | `status --json` CLI | `GET /api/repos`; `GET /api/repos/{id}/gates` |
| Approvals | `approve` CLI | `GET /api/approvals`; `POST /api/repos/{id}/approve/{phase}` (response includes the auto-advance result) |
| Packs | CLI-only | `GET /api/packs`; `POST /api/packs/{name}/install\|verify` |
| Scaffold/validate | **rail not landed** | `POST /api/packs/scaffold`; `POST /api/packs/validate` |
| Reembed | CLI-only | `POST /api/reembed` (`dry_run`), `GET /api/reembed/status` |
| Doctor | CLI-only | `GET /api/doctor`; `POST /api/doctor/repair` |
| Profiles | CLI-only | `GET/POST/DELETE /api/profiles`; `POST /api/profiles/resolve` |
| Skill usage analytics | raw trace rows | `GET /api/telemetry/skill-usage` |
| Coverage v2 | hook-era endpoint removed | `GET /api/telemetry/coverage` — composed/passthrough by event_type per phase/repo/session |
| Telemetry clear | CLI-only | `POST /api/telemetry/clear` |

**Cross-cutting:** phase vocabulary and route values must be **fetched from the API**,
never hardcoded in the frontend — there are 10 backend declaration sites and Part A adds
an 11th value.

**Security:** localhost-only, no auth (unchanged rationale); mutating endpoints bind
loopback and require a custom header (CSRF) from day one; secrets never echoed
(`upstream_api_key` masked).

---

## 4. Sequencing

Decisions recorded 2026-07-01: the rail port happens in this line of work (not the
originating worktree); the `.agentalloy/custom-skills/` requirement is confirmed;
Coverage returns as a v2 aggregation (B1); the `proxy_router` `exclude_none` fix is
confirmed still needed and lands as its own fix branch.

0. **Port the rail** — port `aa4929b` (`new-skill-pack`, `validate-pack`,
   install-pack quality gate) from its v4.0.4 base onto 5.0.x. Storage APIs changed
   underneath it (LadybugDB → skill_store/fragment_store; dedup gate must query
   LanceDB). Blocks Part A step 4-6 prose and the B6 wizard.
1. **Part A** — the `add-skill` lane. Pure backend, independent of the UI; ship it with
   the rail.
2. **B0/B1** — web scaffold + MVP pages (config, telemetry×2 tabs, diagnostics, health).
3. **B2/B3** — skill browser/editor + playground (+ deep-trace fields in B1 land here if
   not already).
4. **B4** — repos dashboard + approval queue (immediately useful for spec/design/
   sdd-fast approvals even before Part A merges; add-skill appears automatically once
   the phase exists, provided phases come from the API).
5. **B5** — ops surfaces (CLI wrapping, all support `--json`).
6. **B6** — wizard (after 0+1), authoring console, eval, activity feed.

## 5. Open items

- Final `prose_invariants` set for `sdd-add-skill.yaml` (proposed above: validate-pack +
  approve; add `install-pack` if dogfooding shows prose drift).
- `add-skill` K/max-token values copied from sdd-fast — revisit after dogfood.
- MCP-server phase enum is outside the `test_config_consistency` tripwire (A1) — decide
  whether to extend the test or generate the enum from the `Phase` Literal.
- Doc placement: this file and its two web-ui siblings are untracked in this checkout;
  the add-skill v4 spec lives untracked in the sibling worktree. Consolidate onto one
  branch when the rail rebase happens.
