## AgentAlloy — workflow context (sidecar)

This block is managed by AgentAlloy. This harness is **not** proxy-intercepted:
your model traffic goes to its usual backend, and AgentAlloy context reaches
you only through this file.

**Current phase.** The project's SDD phase lives in `.agentalloy/phase`. Read
it at session start and match your behavior to that lifecycle stage.

**Keeping this block fresh.** `agentalloy watch start --harness <name>`
regenerates this block (phase-specific workflow guidance + contract context)
within ~1s of any phase or contract change. Without the watcher running, this
block is static — re-read `.agentalloy/phase` yourself when in doubt.

**Manual composition.** The AgentAlloy service runs at
`http://localhost:{port}` — `POST /compose` returns skill context for a task
+ phase on demand.

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship`
(fast lane: `intake`, `sdd-fast`, `qa`, `ship`).
