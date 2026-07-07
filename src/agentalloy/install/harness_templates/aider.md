## AgentAlloy — skill context

**Health-gate.** Verify: `curl -fs http://localhost:{port}/health`. If unreachable, skip.

**Session start — determine phase.** Check `.agentalloy/phase`. If it exists, use that phase. If not:
- SDD work -> pick the matching phase
- Non-SDD work -> skip AgentAlloy entirely

**When in an SDD phase, POST to `/compose/text` with `{"task": "...", "phase": "<phase from .agentalloy/phase>"}`. Read the response before generating code.

**Phase transitions.** Update `.agentalloy/phase` if activity shifts.

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (fast lane: `intake`, `sdd-fast`, `qa`, `ship`).