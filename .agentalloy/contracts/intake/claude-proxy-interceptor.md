---
phase: intake
task_slug: claude-proxy-interceptor
route: full
domain_tags: [api-proxy, fastapi, anthropic-messages, sse-streaming, http-forwarding]
scope:
  touches:
    - src/agentalloy/api/proxy_anthropic_router.py
    - src/agentalloy/api/proxy_signal.py
    - src/agentalloy/api/proxy_injection.py
    - src/agentalloy/api/proxy_context.py
    - src/agentalloy/app.py
    - src/agentalloy/config.py
    - src/agentalloy/install/subcommands/wire.py
    - docs/proxy-architecture.md
  avoids:
    - src/agentalloy/api/proxy_router.py  # do not expand the OpenAI /v1/chat/completions translation bridge
    - src/agentalloy/profiles.py          # do not change profile resolution semantics
    - the claude-code hook path            # stays intact + default; proxy is opt-in
success_criteria:
    - A Claude Code session wired via ANTHROPIC_BASE_URL receives the composed workflow/skill block in its requests with NO Anthropic<->OpenAI translation.
    - The proxy resolves the correct repo (phase + lifecycle_mode) per request despite the anonymous /v1/messages payload.
    - Compose/inject failure forwards the original request unchanged (soft-fail); the proxy-down failure mode is documented as the accepted tradeoff.
    - The hook path remains the default and is not regressed.
related_contracts: []
created_at: 2026-06-21T21:50:46Z
---

# claude-proxy-interceptor

## What the user actually wants

Make the proxy interceptor a viable path for Claude Code. Today, Claude Code is
wired via the hook path; the proxy's Anthropic endpoint (`/v1/messages`) is a
pure Anthropic->OpenAI translation shim that does NOT run signal/compose/inject
and forwards to an OpenAI-shaped upstream. The user wants a Claude Code session
pointed at the proxy (`ANTHROPIC_BASE_URL`) to get just-in-time skills/workflow
prose injected into its requests — **without** relying on the lossy
Anthropic<->OpenAI bridge the user distrusts.

Concretely: a **native Anthropic passthrough** — receive `/v1/messages`, run the
existing signal/compose pipeline, inject the composed block into the request
(leaning toward the last user message to dodge Claude Code's prompt-cached
system block), then forward the request **verbatim in Anthropic format** to
`api.anthropic.com`, passing through the caller's own `x-api-key`. No request or
response translation; streaming is a byte relay.

## Intent signals

- intent: new-build (a new forwarding path) + change-existing (wire step)
- artifact_type: feature
- scope: medium-to-large — touches the Anthropic router, signal/compose reuse, a
  native Anthropic forwarding client, and per-repo wiring; avoids the OpenAI
  bridge and profile-resolution internals.
- urgency: now (actively being explored)

## Open questions to resolve in spec/design (not decided at intake)

1. **Repo identity for an anonymous request.** `/v1/messages` carries no cwd. The
   only per-repo signal is the wired `ANTHROPIC_BASE_URL`. Decide between
   path-in-URL (`/proj/<encoded-abs-path>` — zero new storage) vs. key-in-URL +
   a global `key -> project_dir` registry written at wire time. Both handle
   multi-repo on one proxy; an in-project marker file canNOT route (circular).
2. **Inject location:** last user message vs. system block (cache-bust /
   idempotency tradeoffs).
3. **Forwarding client:** pass through caller `x-api-key` to `api.anthropic.com`
   vs. a configured upstream key; how it coexists with the existing OpenAI
   upstream client in `app.py`.
4. **Fail-open posture:** compose errors must forward unchanged; document that a
   down/slow proxy breaks every request (the hook's asymmetric fail-open is lost
   under base-URL wiring).

## Proposed route

**full** — new surface area (a forwarding path + wiring), real design choices
(items above), multiple components, and hard to reverse (it changes how harness
traffic is intercepted). The fast lane is inappropriate.
