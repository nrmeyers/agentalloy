---
phase: ship
task_slug: stage-b-default-posture
route: fast
domain_tags: [llama-server, python]
scope:
  touches: []
  avoids: []
success_criteria:
    - v6.6.0 released (PR #370 -> release-cut -> ghcr image published) carrying #368 + #369.
    - Dogfood container upgraded and /health-verified (6.6.0, arbitrate, 0.05, 3000ms).
    - QA record at docs/qa/stage-b-default-posture.md (full bar green; external nightly validation tracked in flight).
related_contracts:
    - .agentalloy/contracts/sdd-fast/stage-b-default-posture.md
created_at: 2026-07-08T17:30:00Z
---

# stage-b-default-posture — ship

Released as v6.6.0 (minor: behavior change via overridable config defaults).
Delivery preceded the formal ship walk by user direction (time-crunch launch
day); this contract records it. Outstanding external check: manual nightly
28958810281 (fresh corpus, shipped posture). Follow-ups in memory:
upgrade-path posture notice; injected-token headroom; benchmark refresh
running in a worktree agent.
