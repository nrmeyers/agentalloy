---
phase: spec
task_slug: claude-proxy-interceptor
route: full
domain_tags: [api-proxy, fastapi, anthropic-messages, sse-streaming, http-forwarding, phase-transition, workflow-corpus, pack-versioning]
scope:
  touches:
    # Workstream A — native Anthropic passthrough interceptor
    - src/agentalloy/api/proxy_anthropic_router.py
    - src/agentalloy/api/proxy_signal.py
    - src/agentalloy/api/proxy_injection.py
    - src/agentalloy/api/proxy_context.py
    - src/agentalloy/app.py
    - src/agentalloy/config.py
    - src/agentalloy/install/subcommands/wire.py
    # Workstream B — guarded phase-advance + workflow prose
    - src/agentalloy/install/subcommands/phase.py
    - src/agentalloy/signals/gates.py
    - src/agentalloy/_packs/sdd/sdd-spec-and-scoping.yaml
    - src/agentalloy/_packs/sdd/sdd-design-and-planning.yaml
    - src/agentalloy/_packs/sdd/sdd-build.yaml
    - src/agentalloy/_packs/sdd/sdd-verify-and-review.yaml
    - src/agentalloy/_packs/sdd/sdd-deliver-and-ship.yaml
    - src/agentalloy/_packs/sdd/sdd-fast.yaml
    - src/agentalloy/_packs/sdd/sdd-intake.yaml
    - src/agentalloy/_packs/sdd/pack.yaml
    - docs/proxy-architecture.md
  avoids:
    - src/agentalloy/api/proxy_router.py   # do not expand the OpenAI /v1/chat/completions bridge
    - src/agentalloy/profiles.py           # profile resolution semantics unchanged; not the proxy discriminator
    - the claude-code hook path             # stays default + intact; proxy is opt-in
success_criteria:
    - Claude Code wired via ANTHROPIC_BASE_URL gets the composed block injected with no Anthropic<->OpenAI translation; per-repo phase/lifecycle_mode resolved from a URL discriminator without reading cwd.
    - Compose/resolve failure soft-fails to an unchanged forward; caller x-api-key passes through to api.anthropic.com; streaming relayed byte-for-byte.
    - phase set guards forward transitions against the current phase exit_gates (advisory on unmet, --force bypass); backward/bail transitions stay unguarded.
    - All 7 sdd workflow skills carry reviewed handoff prose for forward self-drive; pack.yaml version bumped and corpus re-embedded so it is live.
related_contracts:
    - .agentalloy/contracts/intake/claude-proxy-interceptor.md
created_at: 2026-06-21T22:07:07Z
---

# claude-proxy-interceptor

## Scope in a sentence

Add a native Anthropic passthrough path so Claude Code wired via `ANTHROPIC_BASE_URL`
gets composed skills injected (no Anthropic↔OpenAI translation), and make
`phase set` guard forward transitions so the agent can self-drive the SDD lifecycle
on both hook and proxy paths — bounded to the proxy/Anthropic surface, the
`phase set` guard, and the SDD workflow prose; **not** the OpenAI bridge, profiles,
or the hook path's default behavior.

## Spec

Acceptance criteria and out-of-scope live in docs/spec/claude-proxy-interceptor.md.
