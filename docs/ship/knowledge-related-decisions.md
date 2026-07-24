# knowledge-related-decisions — Ship

## Summary

Phase 2 JIT push — merges related (thematic) decisions into the design/build
decision block via `related_decisions(task_title)` alongside the existing
GOVERNS-edge walk.

## What changed

| File | Change |
|------|--------|
| `src/agentalloy/api/knowledge_push.py` | `build_decision_block()` accepts `state/slug/task_title`, calls `related_decisions()`, dedups, sets `related_count` |
| `src/agentalloy/api/proxy_apply.py` | `_compose_decision_push()` reads orchestrator, passes state through |
| `src/agentalloy/app.py` | Wires `CodeIndexState` onto orchestrator in `lifespan()` |
| `src/agentalloy/config.py` | Adds `knowledge_related_enabled: bool = True` (env: `KNOWLEDGE_RELATED_ENABLED`) |
| `src/agentalloy/orchestration/compose.py` | `ComposeOrchestrator` accepts `settings` and `state` |
| `src/agentalloy/install/env_forwarding.py` | Registers `KNOWLEDGE_RELATED_ENABLED` in `INTENT_KEYS` |
| `tests/test_knowledge_push.py` | 4 new tests: merge, skip, dedup, graceful degradation |
| `tests/test_proxy_decision_push.py` | Updated for new `_compose_decision_push` signature |
| `tests/test_proxy_decision_push_cadence.py` | Updated for new params |

## Delivery

- **Commit:** `aceb9ba` feat(knowledge): phase 2 JIT push
- **Tests:** 4274 passed, 2 skipped, 0 failed
- **Lint:** ruff check + ruff format clean
- **Feature flag:** `knowledge_related_enabled=True` (measured ~4 ms median overhead)
- **QA:** PASS — all acceptance criteria met (AC1–AC6)

## Rollback

Set `KNOWLEDGE_RELATED_ENABLED=False` (or revert `aceb9ba`). The GOVERNS path
(phase 1) is unaffected — this is purely additive.
