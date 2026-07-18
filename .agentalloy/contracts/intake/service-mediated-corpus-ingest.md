---
phase: intake
task_slug: service-mediated-corpus-ingest
route: full
domain_tags: [lessons-promote, corpus-ingest, service-endpoint, duckdb-single-writer, container-volume]
scope:
  touches:
    - src/agentalloy/install/subcommands/lessons.py          # _corpus_write_blocker → route to service path when reachable; promote_lesson install seam
    - src/agentalloy/install/subcommands/install_pack.py     # install_local_pack — the ingest+reembed the endpoint must invoke in-process (already runs inside the service's own writer)
    - src/agentalloy/api/                                     # NEW corpus-mutating router (ingest a generated pack) — mounted like skill_router/health_router; auth + bind posture TBD in design
    - src/agentalloy/app.py                                   # include_router for the new endpoint (gate it the way code-index is gated?)
    - src/agentalloy/install/server_proc.py                  # reuse resolve_deployment / port_reachable / DEFAULT_HOST=127.0.0.1 to decide native-vs-container and service reachability
    - tests/                                                  # endpoint unit tests + promote-routing tests (extend the lessons.py seam-injected tests from #391)
  avoids:
    - the Knowledge module (#380/#392)                       # separate context module; do not entangle with the typed-decision layer
    - changing the dedup-gate CLASSIFIER                      # same probe_lesson_duplicates semantics must hold; only WHERE it runs may move
    - exposing the corpus endpoint on 0.0.0.0 without an explicit auth decision  # container publishes 0.0.0.0:47950 to the LAN/tailscale
    - reworking the native lock-probe path when the corpus IS host-writable      # #391 fast-path stays; service routing is additive
  success_criteria:
    - "`agentalloy lessons promote` succeeds while the service is RUNNING (native), where today the DuckDB single-writer lock forces `install_blocked`."
    - "`agentalloy lessons promote` succeeds against a CONTAINER deployment, where today a host-side CLI can't reach the in-volume corpus at all."
    - The fail-closed dedup gate is preserved: duplicates are probed BEFORE install and a near-duplicate still blocks (unless --allow-duplicates); no partial/half-ingested skill is ever left in the corpus on failure.
    - The #391 `_corpus_write_blocker` preflight routes to the service path when the service is reachable, instead of returning `install_blocked` — and still fails honestly (clear error) when neither host-write nor service ingest is possible.
    - The security posture of a corpus-mutating endpoint on the service port is an EXPLICIT, documented decision (auth token? loopback-only? gated behind a flag?), not an incidental default.
    - No regression to the native host-writable fast path (service stopped, corpus unlocked → CLI writes directly as it does today post-#391).
related_contracts: [knowledge-module-delivery]
created_at: 2026-07-11T19:30:00Z
---

# service-mediated-corpus-ingest

## What the user actually wants

Make `agentalloy lessons promote` actually install into the live corpus while
the service is up — in both deployment shapes. This is the durable fix behind
the honest failures that #391 (v6.7.0) shipped: today promote can only *refuse*
in two situations, and this work item turns both refusals into successes by
routing the ingest **through the running service** (the one process that legally
holds the corpus writer).

Two failure modes, both from `_corpus_write_blocker` (lessons.py:74):

1. **Corpus lock (native, service running).** DuckDB is single-writer per file;
   even the service's read handle excludes a CLI writer, so a host-side ingest
   can never succeed while the service serves. #391 detects this and returns
   `install_blocked` telling the user to `server-stop` → promote → `server-start`.
2. **Container deployment.** The live corpus lives inside the container's
   `agentalloy-data:/app/data` volume. A host-side promote writes a *host* corpus
   the serving container never reads — so even stopping/starting doesn't help.
   #391 detects a configured+reachable container and returns `install_blocked`
   with "service-mediated ingest is tracked in #390."

The fix: a corpus-mutating endpoint **on the service** (the writer that already
owns the store, inside the container's volume when containerized). The CLI
generates the pack locally (as it does now), then hands the generated pack to the
service to ingest+reembed in-process via `install_local_pack`, which already runs
inside the service's own writer context.

## Why today's code can't do this

- `install_local_pack` (install_pack.py:598) is a **host CLI function** — it
  opens the DuckDB writer itself. That works only when nothing else holds the
  lock and the corpus is on the host filesystem. Neither holds for a running
  native service or any container.
- The service exposes read/compose surfaces (`compose_router`, `retrieve_router`,
  `skill_router`) and web-ops routers, but **no corpus-write endpoint**. There is
  no seam for an external caller to trigger an ingest inside the service process.
- `_corpus_write_blocker` is a pure block/allow oracle — it returns a *reason
  string* or `None`. It has no notion of "the service can do this for you," so it
  can only refuse, never delegate.

## Intent signals

- intent: change-existing (turn two honest refusals into working installs) +
  add-new (a service ingest endpoint).
- artifact_type: feature (service endpoint) + routing change (preflight) +
  a security decision.
- scope: medium-to-large — one new router + its auth/bind posture, an
  `install_local_pack`-in-service invocation path, a rerouting of the promote
  preflight, and tests across both. Touches the install subpackage + the api
  package; does not touch retrieval/composition.
- urgency: now — this is the #390 durable fix the merged #391 explicitly defers
  to; promote is currently unusable in the normal (service-up) case.

## Constraints carried from the work item (decide in spec/design, not here)

1. **Preserve fail-closed dedup-gate semantics.** Probe duplicates BEFORE
   install; leave no partial installs on failure. Whether the probe runs
   host-side (CLI embeds + compares) or service-side (endpoint does it) is a
   design choice, but the *guarantee* — no near-duplicate silently bloats the
   corpus, no half-ingested skill survives an error — must hold identically to
   the current `promote_lesson` flow.
2. **Reroute the #391 preflight instead of blocking.** When the service is
   reachable, `_corpus_write_blocker` (or its caller) should take the service
   ingest path rather than returning `install_blocked`. It must STILL fail
   honestly when neither host-write nor service-ingest is possible (service
   unreachable AND corpus unwritable).
3. **Security posture is an explicit decision.** A corpus-mutating endpoint is a
   new authority on the service port. Native binds `127.0.0.1` (server_proc
   `DEFAULT_HOST`), but the container publishes `0.0.0.0:47950` to the LAN /
   tailscale — so "just add a route" would expose corpus mutation to the network.
   The decision (proxy-token auth like `/proj/{token}`? loopback-only enforced in
   the handler? gated behind an env flag like the code-index module? some
   combination?) must be made and documented, not inherited by accident.

## Open questions to resolve in spec/design (not decided at intake)

1. **Wire protocol.** What does the CLI send the service — the generated pack
   directory contents (multipart/JSON-embedded YAMLs), or a path the service can
   read (only valid when host and service share a filesystem, i.e. native — NOT
   container)? For the container case the pack bytes must cross the boundary.
   Reconcile the two deployment shapes under one endpoint contract.
2. **Where dedup runs.** Host-side probe then service install (two round-trips,
   reuses `probe_lesson_duplicates` as-is), or push-to-service-and-let-it-probe
   (one call, endpoint owns the gate). Affects the embed seam (`_default_embed`)
   and the injected-seam test surface from #391.
3. **Auth / bind / gating** (the security decision above) — pick the mechanism
   and where it's enforced (router dependency vs. handler vs. app-mount gate).
4. **Reembed ownership.** `install_local_pack(run_reembed=True)` triggers an
   in-process bulk reembed. Confirm the service invocation runs exactly one
   reembed and doesn't fight the container restart lifecycle (`no_restart`).
5. **Failure surface.** How endpoint errors (ingest failed, dedup blocked,
   embed backend down) map back to the CLI's existing result-dict actions
   (`install_blocked`, `duplicate`, `install_failed`) so `_render_human` output
   stays stable.
