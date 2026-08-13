## AgentAlloy — workflow context (sidecar)

This block is managed by AgentAlloy. This harness is **not** proxy-intercepted:
your model traffic goes to its usual backend, and AgentAlloy context reaches
you only through this file.

**Current phase.** Check the state panel at the top of this block for the
current SDD phase and lifecycle state. Match your behavior to that stage.

**Keeping this block fresh.** `agentalloy watch start --harness <name>`
regenerates this block (phase-specific workflow guidance + contract context)
within ~1s of any phase or contract change. Without the watcher running, this
block is static — re-read the state panel when in doubt.

**Skill composition.** The AgentAlloy service runs at
`http://localhost:{port}` — `POST /compose/text` returns skill context for a
task + phase on demand. Use the `agentalloy_query` tool (if available) or
`curl` to fetch skills.

**Recording artifacts.** Include artifact markers in your response to record
them: `<!-- agentalloy:artifact name=<name> -->...<!-- /agentalloy:artifact -->`

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship`
(fast lane: `intake`, `sdd-fast`, `qa`, `ship`).
