# AgentAlloy — local-agent mode (v2): handoff / reference

Reference for the **local agent mode (v2)** work done in the
`/home/nmeyers/dev/agentalloy` repo. This doc lives in `newagent` so the
"fresh start" repo can find and reuse it. Nothing here is applied — it is a
pointer plus the portable patch.

## What it is

A question goes to the agentalloy service; the **local LFM2.5-2.6B** model
(not the harness's cloud model) classifies it into one of 10 read-only actions,
fills the arguments under that action's JSON schema, the service executes the
query **in-process**, the model decides loop-or-stop, and finally generates an
answer from what was retrieved. Zero cloud tokens; the harness keeps its cloud
model for coding.

- Ships **off by default** (`LOCAL_AGENT=off`). When on, it adds only
  `POST /local-agent/ask` and `GET /local-agent/health`.
- Loop is a pure, model-client-injected state machine
  (classify → fill → validate → execute → loop|stop) with total termination
  (`none` / `step_budget` / `duplicate` / `*_failed`).
- Two failure postures: an **unavailable LM stage** latches a structured 503
  (v1 contract); an **answered-but-wrong stage** fails open with
  `degraded=true` and the raw step results, never fabricated — index-grounded
  validation rejects a hallucinated FQN/slug before it can execute.
- The 10 actions mirror the `agentalloy_query` MCP tool surface:
  `code_search`, `symbols`, `knowledge_why`, `knowledge_related`,
  `knowledge_entities`, `artifact_body`, `contract_detail`, `telemetry`,
  `get_skill_for`, `none`.

## Where the work lives (in the agentalloy repo)

- **Branch:** `feature/local-agent`
- **Commit:** `761b985`
  - `feat(local-agent): opt-in local-agent mode (v2) — 2.6B model drives the query protocol`
  - Based on `main` at `a9b3813`
    (`fix(install): single code-index wiring call site; no re-prompt on active job (#654)`)
  - 24 files changed, 5054 insertions(+), 5 deletions(-)
- `main` was **not** touched; it is still at `a9b3813` / `origin/main`.

### Files

New:
- `src/agentalloy/local_agent/` — `__init__.py`, `client.py`, `config.py`,
  `executors.py`, `loop.py`, `protocol.py`, `router.py`, `telemetry.py`,
  `validate.py`
- `tests/local_agent/` — `conftest.py` + `test_client.py`, `test_config.py`,
  `test_executors.py`, `test_loop.py`, `test_protocol.py`, `test_router.py`,
  `test_telemetry.py`, `test_validate.py`
- `docs/local-agent-design.md`, `docs/local-agent-plan.md`

Modified (additive):
- `src/agentalloy/app.py`
- `tests/api/test_module_toggle.py`
- `tests/composition/test_retrieval_domain.py`

## Portable patch (to carry into newagent / any fresh clone)

```
/home/nmeyers/dev/0001-feat-local-agent-opt-in-local-agent-mode-v2-2.6B-mod.patch
```

Standard `git format-patch` mbox (224 KB), carries the full commit + message,
based on `a9b3813`. To use:

- Keep it as the same commit: `git am /home/nmeyers/dev/0001-feat-local-agent-opt-in-local-agent-mode-v2-2.6B-mod.patch`
- Apply the diff onto a different base: `git apply /home/nmeyers/dev/0001-feat-local-agent-opt-in-local-agent-mode-v2-2.6B-mod.patch`
- Or read it as reference without applying anything.

Within the agentalloy repo you can also just: `git show 761b985` or
`git log feature/local-agent`.

## What is NOT done (deliberate follow-ups, per the design doc)

- **Installer / presets** — no service unit / model pull; no preset flips it `on` by default.
- **In-repo eval gate** (`eval/local_agent/`) — the 62-task gate suite is not in CI yet; the design flags it as the guard before any preset may enable the mode.
- **SSE streaming** — v2.0 is non-streaming JSON; the step trace makes the response inspectable in the meantime.

## Verification already done

- 364/364 local-agent tests pass; `ruff` and `mypy --strict` clean on the module.
- End-to-end smoke against the live `:50001` model: `none`→abstain,
  `code_search`→`symbols` two-step loop, and the dead-endpoint→503 path.
