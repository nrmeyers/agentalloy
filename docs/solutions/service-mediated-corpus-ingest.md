# Service-Mediated Corpus Ingest (#390)

## Problem

`agentalloy lessons promote` / `install-pack` couldn't write the corpus while the
service was up: DuckDB is single-writer per file, so a running service excludes a
host CLI writer (native), and a container's corpus lives in a data volume a host
CLI can't reach at all. #391 made these fail *honestly*; #390 is the durable fix.

## Approach that worked

Route the write **through the running service** — the one process that legally
holds the writer, and that lives inside the container's volume when containerized.

The keystone was discovering the write is **already a shipped pattern**:
`POST /api/wizard/install` mutates the live corpus with
`store.released()` → `install_local_pack` → `refresh_runtime_cache`. The service
holds only a *read* handle after startup migrate; `store.released()` drops it so
an in-process (or subprocess) writer can grab the DuckDB lock, then reconnects.
The new `POST /corpus/ingest-pack` reuses that recipe, but takes pack **bytes**
(not a filesystem path) so it crosses the host→container boundary.

Three pieces: a secret-guarded endpoint (T1), a 3-way CLI router
`write_host | via_service | blocked` (T2), and a shared **ingest secret** minted
on the host and injected into the container via env at `podman run` (T3).

## Decisions worth keeping

- **The proxy `/proj/{token}` scheme is not auth** — the token is
  `base64url(realpath(project_dir))`, publicly derivable. Any new corpus-mutating
  endpoint needs a *real* secret, because the container publishes `0.0.0.0`. Don't
  reach for the token as a guard.
- **Host is source of truth for the secret; the container receives it via env at
  `podman run`.** This kept `entrypoint.sh` byte-identical (no drift-guard risk) —
  put run-time secrets in the `_run_container` env dict, not the bootstrap script.
- **Route at the CLI callers, not inside `install_local_pack`.** The endpoint
  *reuses* `install_local_pack` server-side, so routing there would recurse. A
  shared `install_or_route` helper in the CLI path keeps the primitive clean.
- **Dedup must run next to the writer.** A host-side probe can't see a container
  corpus; moving the probe server-side is the only way to keep AC-4 honest across
  both deployments.

## What didn't work / cost time

- **Live dogfood state bites tests.** Two existing tests routed to the real
  service on :47950 (405, since it predates the endpoint) because the new routing
  consults real deployment state. Any test exercising the `install_pack` wrapper
  must pin `decide_corpus_write_route` (force `write_host`) to stay hermetic.
- **Two bugs escaped the build and were caught in verify/QA, not unit tests:**
  (1) the endpoint hardcoded `strict=True`, silently dropping
  `install-pack --allow-lint-warnings` on the routed path; (2) a stopped-container
  route fell to `write_host`, writing a host corpus the container's volume never
  reads (and the docstring already claimed it blocked). Both are *cross-boundary
  behavior* — the kind of thing seam-mocked unit tests pass over and a live drive
  (or careful route-logic review) exposes. Lesson: for deployment-shape-dependent
  routing, drive the real surface and review each branch against *each* shape.
- **The strict authoring lint gate is exacting.** Fabricating a valid pack for the
  live test took several iterations (rationale + execution + verification
  fragments, each an exact `raw_prose` slice). Reuse a known strict-clean skill
  when you need a valid fixture.
