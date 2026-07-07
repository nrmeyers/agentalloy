# AgentAlloy — skill context

A local agentalloy service runs at http://localhost:{port}.

**Health-gate.** Verify: `curl -fs http://localhost:{port}/health`. If unreachable, skip.

**Session start — determine phase.** Check `.agentalloy/phase`. If it exists, use that phase. If not:
- SDD work (coding, testing, debugging, designing, spec writing) -> pick the matching phase
- Non-SDD work -> skip AgentAlloy entirely

**When in an SDD phase, before starting work, POST to `/compose/text` with `{"task": "...", "phase": "<phase from .agentalloy/phase>"}`. Read the response before generating code.

**Phase transitions.** Update `.agentalloy/phase` if activity shifts.

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (fast lane: `intake`, `sdd-fast`, `qa`, `ship`).