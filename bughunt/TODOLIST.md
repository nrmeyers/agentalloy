# Bug-Hunt Remediation — Todo List

## Summary

- **Status:** P0 tier merged (PR #70). Pattern-A merged (PR #71). Pattern-B in progress (PR #73, contains B+C). Pattern-C in progress (PR #73).
- **Total tasks:** 91 (42 build + 42 review + 7 test suite checkpoints)
- **Order: P0 → Pattern A-H → P1 confirmed → Test integrity → Concurrency → Config/corpus → Low-sev**
- **PRs:** #70 (P0), #71 (Pattern-A), #73 (Pattern-B+C, pending review)
- **Branch:** `fix/pattern-c-non-transactional-batch`

## Tasks

### P0 Tier (MERGED — PR #70)

1. ✅ P0 #1 — Phase names passed as domain_tags (proxy_signal.py:154 → domain.py:402 → reads/active.py:107)
2. ✅ P0 #2 — AuthoringConfig double-prefix AUTHORING_AUTHORING_MODEL (config.py:17-34)
3. ✅ TEST SUITE — Run full suite after P0 tier

### Cross-Cutting Patterns (Pattern A — MERGED — PR #71)

4. ✅ Pattern A — Sentinel BEGIN/END order never validated → shared replace_marked_block()
5. ✅ Pattern A review — Verify fix applied to all call sites, shared helper tested
6. ✅ Pattern B — Silent JSON-decode → whole-file overwrite (cline, wire_harness, R-INSTALL-2)
7. ✅ Pattern B review — Verify all JSON decode sites handle errors without data loss
8. ⏳ Pattern C — Non-transactional batch writes leave partial state (ingest.py:449, reembed/cli.py:393 & 553, install partial-failure paths)
9. ⏳ Pattern C review — Verify transactional wrapping, rollback on failure
10. ⏳ Pattern D — Streaming/upstream httpx exceptions unhandled → raw 500s (upstream.stream ctx mgrs, non-streaming Anthropic path)
11. ⏳ Pattern D review — Verify error-SSE helper covers all streaming paths
12. ⏳ Pattern E — Idempotency cache returns success without verifying artifact exists (seed_corpus.py:296, start_embed_server ollama timeout, install_pack no_restart)
13. ⏳ Pattern E review — Verify artifact existence checks, proper error propagation
14. ⏳ Pattern F — subprocess.run(text=True) can raise UnicodeDecodeError not in except clause (container_runtime.py:590, server_proc.py:56)
15. ⏳ Pattern F review — Verify UnicodeDecodeError caught in all subprocess paths
16. ⏳ Pattern G — Dead/unwired CLI flags (--quiet 11× refs, --json on wrap unimplemented)
17. ⏳ Pattern G review — Verify flags removed or wired, no dead code remaining
18. ⏳ Pattern H — Cypher built by f-string interpolation of skill_class (reads/active.py:38/97/190)
19. ⏳ Pattern H review — Verify all Cypher queries parameterized, no injection risk
20. ⏳ TEST SUITE — Run full suite after Cross-cutting patterns tier

### P1 Confirmed (#3-#15)

21. ⏳ P1 #3 — query_traces SELECTs 19/27 cols (storage/vector_store.py:534-570)
22. ⏳ P1 #3 review — Verify all 27 columns selected, CompositionTrace hydrated correctly
23. ⏳ P1 #4 — _installed_pack_names filters isinstance(p, str) but records are dicts (install_packs.py:451)
24. ⏳ P1 #4 review — Verify dict access, picker shows [installed] markers
25. ⏳ P1 #5 — _openai_to_anthropic on choices null/empty → 200 with empty content (proxy_anthropic_router.py:203)
26. ⏳ P1 #5 review — Verify error returned for null/empty choices, not silent 200
27. ⏳ P1 #6 — System prompt dropped for content-block-list system (proxy_anthropic_router.py:161-165)
28. ⏳ P1 #6 review — Verify content-block-list system prompt preserved, isinstance check fixed
29. ⏳ P1 #7 — ProcessLookupError path returns without clearing AGENTALLOY_DB_LOCK_HELD (container_service.py:124-132)
30. ⏳ P1 #7 review — Verify sentinel cleared on ProcessLookupError, subsequent stops work
31. ⏳ P1 #8 — In-container lock check returns released without opening DB when LADYBUG_DB_PATH unset (container_service.py:270-278)
32. ⏳ P1 #8 review — Verify DB opened before lock check, LADYBUG_DB_PATH handled
33. ⏳ P1 #9 — cline settings data loss: decode error → {} → write proxy-only (providers/cline/install.py:50, wire_harness.py:1258, R-INSTALL-2 #2)
34. ⏳ P1 #9 review — Verify original settings preserved on decode error, no partial write
35. ⏳ P1 #10 — _reset_skill deletes override with no confirmation when --yes omitted AND stdin not TTY (customize.py:668-672)
36. ⏳ P1 #10 review — Verify TTY check before destructive operation, proper error
37. ⏳ P1 #11 — New .env written before original backed up (write_env.py:154-163)
38. ⏳ P1 #11 review — Verify backup before write, rollback on failure
39. ⏳ P1 #12 — _ingest_yaml subprocess.run(timeout=120) no try/except (install_pack.py:195)
40. ⏳ P1 #12 review — Verify TimeoutExpired caught, cleanup runs, proper error message
41. ⏳ P1 #13 — Partial enable: ollama enabled check=False, agentalloy enable can fail (enable_service.py:239-244)
42. ⏳ P1 #13 review — Verify atomic enable, rollback if second service fails
43. ⏳ P1 #14 — Seed from randomized hash() non-reproducible; choices[0] no bounds check (run_poc.py:129, cross_model.py:103)
44. ⏳ P1 #14 review — Verify deterministic seed, bounds check on choices access
45. ⏳ P1 #15 — if get_embed_client is not None: always truthy (hook_router.py:148)
46. ⏳ P1 #15 review — Verify dead guard removed or fixed, no functional regression
47. ⏳ TEST SUITE — Run full suite after P1 confirmed tier

### Test Integrity (#17-#18)

48. ⏳ P1 #17 — Asserts passed=True for returncode=1 + empty stdout (tests/install/test_preflight…)
49. ⏳ P1 #17 review — Verify test asserts correct behavior, not the bug
50. ⏳ P1 #18 — Multiple dead/broken-mock tests (T-ROOT-1 H1, T-ROOT-2 H1-H3, T-ROOT-3 H2)
51. ⏳ P1 #18 review — Verify mocks patched on correct module paths, tests exercise targets
52. ⏳ TEST SUITE — Run full suite after Test integrity tier (before runtime fixes)

### Concurrency (CC1-CC5)

53. ⏳ CC1 — Non-atomic stat-then-touch TOCTOU on container lock (install-packs)
54. ⏳ CC1 review — Verify atomic lock creation (set -C; : > lock or mkdir)
55. ⏳ CC2 — threading.Timer never cancelled on shutdown (watcher debounce)
56. ⏳ CC2 review — Verify Timer cancelled in finally, no late callbacks after stop
57. ⏳ CC3 — Can orphan uvicorn child on slow-start timeout (restart_service_in_container)
58. ⏳ CC3 review — Verify child process killed on timeout, no orphans
59. ⏳ CC4 — No in-flight guard on stale-cache revalidation (hook_router)
60. ⏳ CC4 review — Verify in-flight guard, thundering herd eliminated, timeout on bg thread
61. ⏳ CC5 — Low-sev TOCTOU/atomicity on PID/temp writes (watch.py:101, _write_phase_atomic, pull_models SSH-key triple-read)
62. ⏳ CC5 review — Verify atomic writes, no TOCTOU on PID/temp files
63. ⏳ TEST SUITE — Run full suite after Concurrency tier

### Config & Corpus (CFG1-3, YS1-2)

64. ⏳ CFG1 — AGENTIALLOY_PACKS injected but never read (typo + dead)
65. ⏳ CFG1 review — Verify env var wired correctly or removed
66. ⏳ CFG2 — uv pip install -e ".[dev]" bypasses uv.lock; two divergent dev groups (CI)
67. ⏳ CFG2 review — Verify CI uses uv.lock, dev groups consolidated
68. ⏳ CFG3 — asyncpg declared never imported; settings.log_level never applied to logging (deps)
69. ⏳ CFG3 review — Verify asyncpg removed or used, log_level applied to logging config
70. ⏳ YS1 — react-rendering-keys-and-memoization.yaml has no execution fragment (ingest hard-errors)
71. ⏳ YS1 review — Verify execution fragment added or ingest handles gracefully
72. ⏳ YS2 — sdd-build.yaml absent from sdd/pack.yaml; phase_scope vs applies_to_phases mismatch
73. ⏳ YS2 review — Verify pack.yaml includes sdd-build, phase_scope/applies_to_phases aligned
74. ⏳ TEST SUITE — Run full suite after Config/corpus tier

### Low Severity (LOW-1 through LOW-7)

75. ⏳ LOW-1 — round() banker's-rounding on VRAM (R-INSTALL-1)
76. ⏳ LOW-1 review — Verify math.ceil or explicit rounding mode for VRAM allocation
77. ⏳ LOW-2 — assert for runtime validation under -O (R-RETSTORE #11, R-INSTALL-1)
78. ⏳ LOW-2 review — Verify asserts replaced with if/raise for runtime checks
79. ⏳ LOW-3 — Dead code (gates._is_semantic, evaluate_gates, openclaw.runtime.env_builder, redundant import re)
80. ⏳ LOW-3 review — Verify dead code removed, no broken imports
81. ⏳ LOW-4 — Predicate ambiguities (eval_artifact_contains/eval_contract_has_tags all-vs-any; agent_intent_matches reads prompt not response)
82. ⏳ LOW-4 review — Verify predicate semantics clear, agent_intent_matches reads correct field
83. ⏳ LOW-5 — LadybugStore.close() doesn't close the handle (R-RETSTORE #6)
84. ⏳ LOW-5 review — Verify handle closed, no resource leak
85. ⏳ LOW-6 — GitLab subgroup repo-name regex (R-CORE #1)
86. ⏳ LOW-6 review — Verify regex handles subgroups, no false positives
87. ⏳ LOW-7 — word-count floor rejects short rationale/example (R-CORE #2)
88. ⏳ LOW-7 review — Verify word-count floor reasonable or removed for short fields
89. ⏳ TEST SUITE — Final full suite run after all tiers complete

## Workflow

Each task follows: build → review → (optional test suite at tier boundaries) → commit → push → PR → monitor → merge → next task.

## Notes

- All PRs target `main` from feature branches (e.g., `fix/p0-domain-tags-authoring-config`)
- CI: `pipx-smoke` + `quality` (lint + format)
- Full test suite runs in CI after merge (1887 tests)
- User confirms with short responses ("Do it", "total max at 3 is fine")