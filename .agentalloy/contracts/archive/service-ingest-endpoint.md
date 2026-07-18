---
phase: build
task_slug: service-ingest-endpoint
route: full
domain_tags: [corpus-ingest, service-endpoint]
scope:
  touches:
    - src/agentalloy/api/corpus_ingest_router.py   # NEW: POST /corpus/ingest-pack
    - src/agentalloy/app.py                          # mount alongside skill_router (compose/proxy group)
    - tests/test_corpus_ingest_endpoint.py           # NEW
  avoids:
    - CLI-side routing        # T2 (service-mediated-cli-routing)
    - secret provisioning     # T3 (ingest-secret-provisioning) — this task CONSUMES the resolver, doesn't mint
    - the web wizard router   # separate surface; do not entangle add-skill-lane logic
  success_criteria:
    - POST a valid pack (bytes map) + ingest token → 200, skill retrievable via compose on the same app (AC-1).
    - Write acquires the DuckDB writer under `store.released()` while the app holds its read handle — no lock error (AC-1/AC-2).
    - Server-side dedup probe runs BEFORE install; a hard near-duplicate returns `duplicate_refused` with NO install; `allow_duplicates=true` installs (AC-4).
    - An ingest failure leaves the corpus clean (existing install_local_pack rollback runs in-service) (AC-5).
    - Missing/wrong `X-AgentAlloy-Ingest-Token` → 401 with no corpus mutation; `AGENTALLOY_CORPUS_INGEST=0` → 404 (AC-7).
    - `reembed=false` skips the reembed pass, `reembed=true` runs exactly one; `refresh_runtime_cache` reloads so the skill serves without restart (AC-9).
    - Response is the `install_local_pack` result dict verbatim (+ probe outcome) so the CLI can map it to existing actions (AC-10).
related_contracts: [service-mediated-cli-routing, ingest-secret-provisioning]
created_at: 2026-07-11T20:00:00Z
---

# service-ingest-endpoint

Build T1 of `docs/design/service-mediated-corpus-ingest/`. New service endpoint
`POST /corpus/ingest-pack` that ingests a pushed pack into the live corpus from
inside the service process — the AC-1/AC-2 keystone.

## The recipe (proven by wizard-install)

Model on `web/wizard_api.py:182-229`, but bytes-based and secret-guarded:

```python
# in a thread (asyncio.to_thread), à la wizard:
tmp = materialize(body.pack)                       # {relpath: content} -> temp dir in service fs/volume
hits = probe_lesson_duplicates(fragment_texts, embed=..., vector_store=app.state.vector_store, ...)
if hits and not body.allow_duplicates:
    return duplicate_refused(...)                   # NO install
store = request.app.state.store
with store.released():
    result = install_local_pack(tmp, root=<service root>, no_restart=True,
                                strict=True, allow_duplicates=body.allow_duplicates,
                                run_reembed=body.reembed)
refresh_runtime_cache(request.app)
return result
```

## Must
- Guard: `X-AgentAlloy-Ingest-Token`, constant-time compare against the T3
  resolver (lazy-import; this task does not mint). 401 on mismatch/absent.
- Optional off-switch `AGENTALLOY_CORPUS_INGEST=0` → 404 (route absent/disabled).
- Mount with the compose/proxy router group in `app.py`, NOT the web group.
- Server-side dedup (D2): the probe runs here against the service's live
  `vector_store`, not on the client.

## Watch (resolve in build, don't paper over)
- `root=` for `install_local_pack`: server-side there is no host repo. Pass a
  service-scoped root (data dir) so `install_state` doesn't try to write host
  state. Confirm the pack-dir alone suffices for ingest; document the choice.
- Temp-dir cleanup on both success and failure paths.
- Concurrency: two overlapping ingests both calling `store.released()` — serialize
  (a lock) or document that the DuckDB writer lock already serializes and the
  read-handle reacquire is safe.

## Reuse
`store.released()` (storage/skill_store.py:160), `refresh_runtime_cache`
(web/runtime_refresh.py), `probe_lesson_duplicates` (lessons.py:41),
`install_local_pack` (install_pack.py:598), `_default_embed` (lessons.py:134).
