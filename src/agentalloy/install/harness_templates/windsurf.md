---
description: Fetch skill context before starting any SDD coding task
trigger: always_on
---

# AgentAlloy — skill context

A local agentalloy service runs at `http://localhost:{port}` with a curated corpus of engineering skills.

**Health-gate.** Before using, verify: `curl -fs http://localhost:{port}/health`. If unreachable, ignore this block.

**Session start — determine phase.** Check `.agentalloy/phase` for the current phase. If it exists, use that phase. If not, evaluate user intent:
- SDD work (coding, testing, debugging, designing, spec writing) -> pick the matching phase
- Casual chat, questions, non-SDD work -> skip AgentAlloy entirely

**When in an SDD phase, before starting work:**
```bash
curl -s -X POST http://localhost:{port}/compose/text \
  -H 'Content-Type: application/json' \
  -d '{"task": "<one sentence describing what you are about to do>", "phase": "<phase from .agentalloy/phase>"}'
```

**Phase transitions.** If the user's activity clearly shifts to a different SDD phase, update `.agentalloy/phase` and call `/compose` with the new phase.

Phases: `intake`, `spec`, `design`, `build`, `qa`, `ship` (fast lane: `intake`, `sdd-fast`, `qa`, `ship`).