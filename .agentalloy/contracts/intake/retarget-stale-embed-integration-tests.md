---
phase: intake
task_slug: retarget-stale-embed-integration-tests
route: full
domain_tags: [testing, install]
scope:
  touches:
    - tests/test_v1_5_integration.py   # hardcodes LM_STUDIO_BASE_URL=http://localhost:11434 (ollama's port, LM-Studio naming) — 13 tests skip forever
    - src/agentalloy/config.py         # AuthoringConfig.lm_studio_base_url / lm_base_url defaults are ollama/LM-Studio-era fossils (11434/11435)
  avoids:
    - the runtime embed path            # Settings.runtime_embed_base_url (47951) is correct and live
    - adding new external service deps  # retarget to the EXISTING llama-server stack, don't invent another
success_criteria:
    - tests/test_v1_5_integration.py probes the llama-server embed endpoint (Settings.runtime_embed_base_url, port 47951) instead of localhost:11434; fixture/skip-message naming says llama-server, not LM Studio.
    - With the standard local stack up (llama-server + nomic-embed-text-v1.5), `uv run pytest -m integration` runs the 13 currently-dead tests instead of skipping them.
    - AuthoringConfig loses (or retargets + renames) its lm_studio_base_url / 11434-11435 defaults; grep for 11434/11435/"LM Studio" in src/ and tests/ comes back clean or deliberate.
    - No version bump if the change stays tests-only; bump per RELEASE.md §4 if AuthoringConfig (src/) changes ship.
related_contracts: []
created_at: 2026-07-06T21:09:08Z
---

# retarget-stale-embed-integration-tests

## What the user actually wants

The v1.5 integration tests must exercise the real embed stack. They currently
probe `http://localhost:11434` under LM-Studio naming — a port that belonged
to a since-removed ollama service (torn down 2026-07-06; LM Studio and ollama
were both retired when the installer standardized on llama-server,
ports 47950/51/52). The 13 tests have been silently dead since that
migration: while the zombie ollama ran they skipped with a confusing
"nomic... not loaded in LM Studio (have: [qwen3.5:0.8b, ...])"; now they skip
with "LM Studio not reachable" — either way, zero coverage.

## Context

- Discovered during QA of container-module-env-propagation (2026-07-06) when
  the skip messages named a stack the project killed ages ago.
- The correct target already exists: the llama-server embed endpoint on 47951
  (`Settings.runtime_embed_base_url`), OpenAI-compatible, nomic-embed-text-v1.5,
  768-dim, `search_query:`/`search_document:` prefixes.
- `AuthoringConfig` (config.py) still carries `lm_base_url` (11435) and
  `lm_studio_base_url` (11434) defaults from the same era — the authoring
  pipeline is being redesigned, so decide in spec whether to retarget or drop
  them with the redesign.

## Intent signals

- intent: fix-defect (test coverage silently lost; misleading naming)
- artifact_type: test rework + config default cleanup
- scope: small — one test module + config defaults; no runtime behavior change
- urgency: low — no shipped behavior affected; coverage gap only
