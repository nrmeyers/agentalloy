"""Local agent mode (v2) — a small local model driving the query protocol.

Opt-in module (``LOCAL_AGENT=on``; ``off`` by default): a question is
classified into one of 10 read-only actions, its arguments are filled under
the action's JSON schema, validated against the live index, executed in
process against the existing stores, and the loop repeats until ``none`` or
the step budget. The final answer is generated from the capped transcript.

Design: ``docs/local-agent-design.md``. Conventions followed: env-driven
config (LM_ASSIST pattern), fail-open LM-stage failure model, in-process
execution reusing the lifespan-scoped stores.
"""
