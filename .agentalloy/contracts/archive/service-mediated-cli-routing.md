---
phase: build
task_slug: service-mediated-cli-routing
route: full
domain_tags: [lessons-promote, install-pack]
scope:
  touches:
    - src/agentalloy/install/subcommands/lessons.py        # _corpus_write_blocker -> 3-way router; promote routing
    - src/agentalloy/install/subcommands/install_pack.py   # install-pack + install-packs routing
    - src/agentalloy/install/ingest_client.py              # NEW: bytes-upload HTTP client for /corpus/ingest-pack
    - tests/test_corpus_ingest_routing.py                  # NEW
  avoids:
    - the endpoint handler        # T1 (service-ingest-endpoint) — this task CALLS it
    - minting the secret          # T3 — this task READS it via the resolver to send the header
    - changing the dedup classifier or thresholds   # only WHERE the probe runs moves
  success_criteria:
    - "_corpus_write_blocker refactored to a 3-way decision: write_host | via_service | blocked(reason); reachability from resolve_deployment + port_reachable."
    - Service reachable → all three callers (promote / install-pack / install-packs) route bytes to /corpus/ingest-pack and report success (AC-1/AC-3).
    - Service down + corpus writable → write_host: today's direct install_local_pack runs, zero behavior change (AC-8).
    - Service down + corpus locked/container → blocked: the #391 honest-error string is preserved byte-for-byte (AC-6).
    - On the via_service path the host-side dedup probe is NOT run (no double gate; can't probe a container corpus anyway); it stays on the write_host path (AC-4).
    - install-packs re-run sends reembed=false on every pack except the last → one reembed for the batch (AC-9).
    - Endpoint HTTP failures map to existing result actions (install_failed/install_blocked/duplicate_refused); _render_human output unchanged; no raw HTTP error surfaces (AC-10).
related_contracts: [service-ingest-endpoint, ingest-secret-provisioning]
created_at: 2026-07-11T20:00:00Z
---

# service-mediated-cli-routing

Build T2 of `docs/design/service-mediated-corpus-ingest/`. Route the three
corpus-writing CLI callers through the service when it's up; preserve today's
host-direct and honest-failure paths when it isn't.

## The router (D5)

`_corpus_write_blocker` (lessons.py:74) stops being a block/allow oracle and
becomes:

- `write_host` — corpus locally writable, no service → today's `install_local_pack`.
- `via_service` — `resolve_deployment().deployment` reachable (`port_reachable`)
  → upload to `/corpus/ingest-pack`.
- `blocked(reason)` — neither → the exact #391 error (AC-6).

## Wiring
- `promote_lesson`: generation stays host-side (unchanged). Then route. On
  `via_service`, skip the host probe (lessons.py:206-269) — the endpoint owns it.
- `install-pack <dir>`: route an existing pack dir the same way.
- `install-packs` re-run: on `via_service`, loop packs through the client with
  `reembed=false` except the last (batch, AC-9).
- Result mapping: HTTP/endpoint failures → `install_failed`/`install_blocked`/
  `duplicate_refused` so `_render_human` (lessons.py:343) is unchanged (AC-10).

## Client (`ingest_client.py`)
Read the ingest secret from local config (T3 resolver), POST `{pack: {relpath:
content}, allow_duplicates, reembed}` with `X-AgentAlloy-Ingest-Token`, return the
result dict. Connection refused / non-2xx → a typed failure the callers map to
`install_failed`.

## Watch
- Double-gate: ensure exactly one dedup probe on each path (host probe only on
  `write_host`).
- `_INSTALL_OK_ACTIONS` (lessons.py) still governs the success set for the
  via_service result.
- Keep the #391 seam-injected test hooks (`write_blocker`, `install`, `embed`,
  `vector_store`) working — extend, don't break.
