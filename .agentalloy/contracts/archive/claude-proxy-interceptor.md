---
phase: design
task_slug: claude-proxy-interceptor
route: full
domain_tags: [api-proxy, fastapi, anthropic-messages, sse-streaming, http-forwarding, httpx, header-forwarding, oauth-passthrough, phase-transition, workflow-corpus, pack-versioning, direnv]
scope:
  touches:
    # Workstream A — native Anthropic passthrough
    - src/agentalloy/api/anthropic_passthrough.py   # new
    - src/agentalloy/api/proxy_anthropic_router.py
    - src/agentalloy/api/proxy_signal.py
    - src/agentalloy/api/proxy_injection.py
    - src/agentalloy/api/proxy_context.py
    - src/agentalloy/app.py
    - src/agentalloy/config.py
    - src/agentalloy/install/subcommands/wire_harness.py
    # Workstream B — guarded advance + prose
    - src/agentalloy/install/subcommands/phase.py
    - src/agentalloy/signals/gates.py
    - src/agentalloy/_packs/sdd/*.yaml
    - src/agentalloy/_packs/sdd/pack.yaml
    - docs/proxy-architecture.md
  avoids:
    - src/agentalloy/api/proxy_router.py   # OpenAI /v1/chat/completions bridge — untouched
    - src/agentalloy/api/proxy_anthropic_router.py  # the _anthropic_to_openai translation shim stays; new path is separate
    - src/agentalloy/profiles.py           # not the discriminator
    - the claude-code hook path             # mutually exclusive at wire; removal is a follow-on
  success_criteria:
    - Auth-transparent native passthrough (forward caller credential verbatim; wiring sets only ANTHROPIC_BASE_URL) — validated against live account-auth traffic.
    - Per-repo /proj/<base64url-path> discriminator resolves phase/lifecycle_mode without cwd; denylist header forwarding; raw SSE relay; soft-fail to unchanged forward.
    - Guarded forward phase set (deterministic exit_gates only, --force bypass) + reviewed prose across all 7 sdd skills + pack bump/re-embed.
    - direnv-if-present-else-hint per-repo carrier; proxy/hook mutually exclusive; configurable upstream enables proxy chaining.
related_contracts:
    - .agentalloy/contracts/intake/claude-proxy-interceptor.md
    - .agentalloy/contracts/spec/claude-proxy-interceptor.md
created_at: 2026-06-21T23:12:31Z
---

# claude-proxy-interceptor

## Scope in a sentence

Build a native Anthropic passthrough proxy path (auth-transparent, per-repo
`/proj/<token>` discriminator, compose+inject into the last user message, raw
forward/stream to a configurable Anthropic upstream) plus guarded forward `phase set`
and self-drive prose across all SDD skills — modeled on Headroom for the passthrough,
built fresh for the AgentAlloy-specific discriminator/compose/phase machinery.

## Design

Approach, task plan, and test cases live in docs/design/claude-proxy-interceptor/
(approach.md, tasks.md, test-plan.md).
