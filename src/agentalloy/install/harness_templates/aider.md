## AgentAlloy — skill context

**Health-gate.** Verify: `curl -fs http://localhost:{port}/health`. If unreachable, skip.

**Session start — determine phase.** Check `.agentalloy/phase`. If it exists, use that phase. If not:
- SDD work -> pick the matching phase
- Non-SDD work -> skip AgentAlloy entirely

**When in an SDD phase, POST to `/compose/text` with `{"task": "...", "phase": "<phase from .agentalloy/phase>"}`. Read the response before generating code.

**Phase transitions.** Ask the user before changing phase. The sidecar watcher keeps `.agentalloy/phase` in sync with the store — do not write the phase file directly; use `agentalloy phase set <phase>` or the proxy's `POST /state/phase` endpoint.

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (fast lane: `intake`, `sdd-fast`, `qa`, `ship`).
