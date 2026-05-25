## AgentAlloy — proxy mode

AgentAlloy is wired as a proxy at `http://localhost:{port}/v1`.

Your normal chat completions go through the proxy. Skill composition and
system message injection happen transparently — no manual `/compose` calls
needed.

**Session start — determine phase.** Check `.agentalloy/phase` for the current
phase. If it exists, the proxy uses it. If not, the proxy passes requests
through unchanged.

**Non-SDD work.** Casual chat and questions bypass skill injection
automatically — the proxy evaluates signals per request.

Phases: `spec`, `design`, `build`, `qa`, `ops`, `meta`, `governance`, `ship`.
