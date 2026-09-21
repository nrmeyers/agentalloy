# BUG: v2 steering/surface split — steering panel emits dead v1 URLs; approval gate unclosable over HTTP

Status: **fixed** · Filed: 2026-09-15 · Resolved: 2026-09-20 · Severity: **high** (agent-facing breakage; session API calls can ECONNREFUSED)

Resolution: RC-2 fixed by migrating `state_leg.py` action templates to the v2
surface (`POST /tool` + tool names + `project` field, `GET /status` / `GET /gates`,
`/sessions`); RC-3 fixed by `phase_advance(approved=true)` recording the approval
pinned to the exit artifact digest with the store hard-gating the advance
(commit 59c3281); RC-1 fixed by `resolve_base_url()` preferring
`AGENTALLOY_SERVICE_PORT` over install state, and by restarting both services
from `~/dev/agentalloy` on 2026-09-20 (server `:48950`, proxy `:48953`, env
preserved from the live process). Verified: AC-1 grep clean; `GET /status`
`api_version=2.0` with `phase`/`phase_set`/`next_gate`; `GET /gates` shows the
`spec→design` approval row (digest `912d602b8e330de4`); `POST /tool`
(`code_search`) ok; `resolve_base_url()` → `http://127.0.0.1:48950` under the
proxy's live env; 82 tests green (test_state_leg, test_server, test_phase_machine,
test_stores, test_acceptance, test_e2e_integration, test_sessions,
test_phase_aware_steering).

Reported from: TheForge wiring verification work item (`nrmeyers__TheForge`, SDD `spec` phase)

## Symptom

1. **Agents receive a steering block with a dead service URL.** The `AGENTALLOY-STATE`
   panel injected every turn advertises `scope.service: http://127.0.0.1:47950` and action
   hints for v1 routes that no longer exist:
   - `POST /state/advance` / `POST /state/approve-phase` / `GET /state/phase`
   - `PUT /state/artifact` / `GET /state/artifact`
   - `GET /contracts`
   - `GET /state/sessions/active` etc.
   - `?repo_root=<path>` / `?repo=<slug>` query scoping
   Following any of these against the v2 service 404s.
2. **Client sessions can fail outright with** `ECONNREFUSED 127.0.0.1:47950` — client
   profiles wired to the v1 proxy port (nothing listens there anymore; the v2 proxy
   listens on `:48953`).
3. **The `spec→design` approval gate cannot be closed over any HTTP/tool surface.**
   The unscoped store shows `requires_approval: true, approved: false` with the
   `spec-exit` digest, but there is no route/tool that records the machine approval row.

## Reproduce

```bash
# Panel advertises :47950; v1 routes 404 on the v2 service:
curl -s http://127.0.0.1:48950/state/advance        # 404
curl -s http://127.0.0.1:48950/contracts            # 404
curl -s http://127.0.0.1:48950/status | head -c 200  # phase lives here (api_version=2.0)
# Gate stuck: exit artifact recorded, approval unrecordable:
curl -s http://127.0.0.1:48950/gates                # spec→design: has_exit_artifact=true, approved=false
```

## Root causes

### RC-1 — Running steering proxy is a retired v1 checkout (runtime)

- Live proxy PID (2026-09-15): `~/dev/newagent/.venv/.../agentalloy proxy`, started 2026-09-12,
  listening on `:48953`.
- `~/dev/newagent` @ `4a60e54` — v1-era checkout ("v1-surface integration; v1 retired").
- Current source: `~/dev/agentalloy` @ `0eda5b8` ("v2 execution stack …; v1 retired").
- The proxy resolves its advertised base URL via `resolve_base_url()`
  (`src/agentalloy/api/state_client.py`), which reads `~/.config/agentalloy/install-state.json` →
  **port `47950`** (v1 leftover). The v2 service runs on `AGENTALLOY_SERVICE_PORT=48950` with
  state at `~/.local/share/agentalloy-instance/state.duck` (server env).

### RC-2 — v2 `state_leg.py` action templates are still v1-shaped (latent code defect)

`_add_actions()` in `src/agentalloy/api/state_leg.py` emits, verbatim:

| Template (line ~) | v2 reality |
|---|---|
| `POST {service}/state/advance` (≈329) | `POST /tool` with `{"name":"phase_advance",...}` |
| `POST {service}/state/approve-phase` (≈374) | no equivalent exists (see RC-3) |
| `PUT {service}/state/artifact` (≈316) | `POST /tool` with `{"name":"artifact_record",...}` |
| `GET {service}/state/phase` (≈397) | `GET /status` |
| `GET {service}/contracts` (≈334) | `POST /tool` with `{"name":"contract_detail",...}` |
| `/state/sessions/*` (≈418) | `GET/POST /sessions*` |
| `?repo_root=` / `?repo=` scoping | `project` field in `/tool` body |

The v2 server (`src/agentalloy/server.py`) implements none of the v1 routes. Full v2 route set:
`/health /status /chat /compose /chat/stream /tool /reindex /usage* /profiles /gates /sessions*
/analytics /lessons /dashboard`. `resolve_base_url()`'s default port (48950) is correct — only
the endpoint templates were never migrated off the v1 surface.

### RC-3 — Approval has no HTTP/tool surface; two divergent gate systems

- Gate system A (machine): LangGraph `PhaseMachine` + **unscoped** store
  (`project=""`; what `/status`, `/gates`, and the steering read). Approval is recorded only
  inside `_approval_gate_node` via `interrupt()` → `resume({"approved": true})` →
  `record_approval(transition, digest)` (`src/agentalloy/phase_machine.py`). Not exposed over HTTP.
- Gate system B (signals): `agentalloy approve <phase>` CLI reads the **project-scoped** store
  (keyed to the repo, e.g. `nrmeyers__TheForge`), digests `_APPROVAL_STORE_NAME_GLOB[phase]`
  (e.g. `spec.artifact`), calls `set_approval`, then `phase set`. It never touches the
  unscoped store — so `agentalloy approve spec` fails with
  "Cannot approve 'spec': current phase is 'intake', not 'spec'" while `/gates` (unscoped)
  sits at `spec` with `approved: false`.
- `_phase_advance` (`src/agentalloy/executors.py` ≈535) only **checks**
  `store.is_approved(transition, exit_digest)` — it never records an approval.
- Consequence: an approval-gated transition (`spec→design`, `design→plan`, `plan→build`) is
  unclosable over any surface an agent can reach. Manual workaround requires stopping the
  server (exclusive DuckDB RW lock) and calling
  `StateStore(project="").record_approval(...)` + `advance_phase(...)` in-process.

Supporting API facts (`src/agentalloy/state_store.py`): `record_approval` (422),
`is_approved` (438), `get_exit_artifact_digest` (456), `advance_phase` (380). Exit artifact
name convention for the machine: `{phase}-exit` (e.g. `spec-exit`).

## Fix scope

1. **Migrate `state_leg.py` action templates to the v2 surface** (RC-2):
   `POST /tool` with tool names (`artifact_record` / `phase_advance` / `phase_reset` /
   `contract_detail`), `GET /status`, `GET /gates`, `/sessions` routes, `project` field in
   the `/tool` body in place of `?repo_root=`/`?repo=`.
2. **Add a coherent approval surface for the machine store** (RC-3): either a new `/tool`
   tool (e.g. `approve_phase` → `record_approval(transition, digest)` + `advance_phase(target)`)
   or an explicit route (e.g. `POST /gates/{transition}/approve`). Must be project-scoped-
   optional like `/tool`, and must make the unscoped machine store and the
   `agentalloy approve` CLI view the same work item's phase (reconcile or retire system B).
3. **`resolve_base_url()` ordering** (RC-1): prefer live env/instance config
   (`AGENTALLOY_SERVICE_PORT` / instance state) over the shared
   `~/.config/agentalloy/install-state.json`; `agentalloy wire` must (re)write the correct
   port (`48950`) so panels stop advertising `47950`.

## Non-goals / constraints

- No port changes (service 48950, model 50001, proxy 48953, embed 48951).
- Server keeps the **sole exclusive RW handle** to `state.duck`; the proxy reads state only
  via `/status` (never opens the DB) — preserve.
- `resolve_base_url()` remains the single source of the advertised URL.
- Panel must stay byte-stable across turns where state is unchanged (prompt-cache friendly).
- No change to phase ordering or the approval-gate set (`spec→design`, `design→plan`, `plan→build`).

## Acceptance criteria

- **AC-1** — `rg 'state/advance|state/approve-phase|state/artifact|state/phase|/contracts|repo_root' src/agentalloy/api/state_leg.py`
  returns nothing; artifact/advance/contract hints use `POST /tool` + tool names + `project`
  field; a v2-emitted panel advertises `scope.service` = `http://127.0.0.1:48950`.
- **AC-2** — With `phase=spec` + recorded `spec-exit`, an approval issued via the new surface
  records the unscoped approval row and advances to `design`
  (`GET /status` → `phase=design`; `GET /gates` → `design→plan has_exit_artifact=false`);
  `agentalloy approve` (project-scoped) and the unscoped machine agree on the work item's phase.
- **AC-3** — `~/.config/agentalloy/install-state.json` reports port `48950`; after a proxy
  restart from `~/dev/agentalloy`, the injected panel advertises `:48950` and no panel URL 404s
  against the live v2 service.
- **AC-4** — No regression: `GET /health` ok; `GET /status` `api_version=2.0`; `POST /tool`
  (`artifact_record`, `phase_advance`, `code_search`, `contract_detail`), `POST /compose`,
  `GET /gates` unchanged; test suite green.

## Operator follow-up (runtime, not code)

1. Restart the steering proxy from the current checkout:
   kill PID `3803279` (and its `uv` parent `3803276`); relaunch
   `uv run agentalloy proxy` from `~/dev/agentalloy` (listens `:48953`).
2. Re-run `agentalloy wire` so `~/.config/agentalloy/install-state.json` carries port `48950`,
   and re-point any client profiles still set to `http://127.0.0.1:47950` (they must use the
   proxy on `:48953`).
3. One-off manual advance already applied for the TheForge work item on 2026-09-15:
   unscoped store advanced `spec→design` via in-process `record_approval("spec→design",
   "912d602b8e330de4")` + `advance_phase("design")` after a brief server restart (DuckDB
   exclusive lock). The project-scoped TheForge store was left at `intake` — reconcile per RC-3.
4. `~/.local/share/agentalloy-v2-instance/env.sh` is STALE — it describes an older instance
   (upstream `http://0.0.0.0:60005`, model `qwen3.8-27b`, repo `~/dev/newagent`, state under
   `agentalloy-v2-instance/`). Do NOT source it for server env; only `V2_UPSTREAM_KEY` is
   valid. The live server's authoritative env (captured 2026-09-15): upstream
   `http://100.81.56.59:8000`, model `Qwen3.8-27B-FP8`, repo `~/dev/agentalloy`, state under
   `~/.local/share/agentalloy-instance/` (state.duck / usage.duck / index), ports 48950/48953/50001/48951.
