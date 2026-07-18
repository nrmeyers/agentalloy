---
phase: build
task_slug: env-fwd-registry
route: full
domain_tags: [install, python]
scope:
  touches:
    - src/agentalloy/install/env_forwarding.py   # NEW module
    - tests/test_env_forwarding.py               # NEW: audit enforcement + parser + filter tests
  avoids:
    - src/agentalloy/config.py field changes      # audit reads Settings, never mutates it
    - container_runtime.py                        # integration is T2 (env-fwd-run-command)
success_criteria:
    - INTENT_KEYS / HOST_TOPOLOGY_KEYS frozen sets per approach.md classification, disjoint, with per-key rationale comments; MODULE_TOGGLES metadata (toggle → module name + enable hint).
    - read_env_file() tolerant parser (comments/blank/malformed/quoted; missing file → {}); forwarded_env(env_path) returns .env ∩ INTENT_KEYS.
    - test_settings_keys_all_classified enumerates Settings.model_fields and FAILS on any env key absent from both sets (spec AC 11).
    - Non-Settings intent keys included in INTENT_KEYS — the assist-stack group (LM_ASSIST, LM_ASSIST_DOC_CAP_CHARS/MAX_CANDIDATES/TIMEOUT_MS/MODEL/RERANK_URL, SIGNAL_INTENT_BACKEND/RERANK_URL/RERANK_MODEL) forwards as one coupled group, plus AGENTALLOY_RELEASE_CHECK; renderer-owned keys documented as never-forwarded.
    - URL_CLASS_UPSTREAM_KEYS set (UPSTREAM_URL, ANTHROPIC_UPSTREAM_URL) exported for the T2 loopback warning; rerank URLs deliberately excluded (in-container loopback is correct for them).
related_contracts:
    - .agentalloy/contracts/design/container-module-env-propagation.md
created_at: 2026-07-06T19:53:43Z
---

# env-fwd-registry

T1 of docs/design/container-module-env-propagation/tasks.md: the allowlist
classification registry and its enforcement test. Pure new module — no
behavior change until T2 wires it into the run-command renderer.
