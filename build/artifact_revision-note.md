# R1 — Revision: the interpreter surface is MCP-shaped tool-calling (user-approved 2026-09-03, during plan)

## What changed
User correction (confirmed: "Whole surface, one loop"): state and contract management — and the ENTIRE interpreter surface — are **tool calls the LFM makes** (MCP-shaped tools), not the spike's two-stage classify→fill JSON protocol.

- **Whole surface, one loop:** all 10 read tools + 4 state tools (contract_add, contract_read, artifact_record, phase_advance) are tools, each with an MCP-standard JSON-Schema `inputSchema` (ported from the spike's validated `ARG_SCHEMAS`) + a per-tool description.
- **Transport:** the `:50001` chat-completions `tools` array — llama.cpp compiles the schema to a GBNF grammar server-side, i.e. the SAME constrained-decoding mechanism the spike validated with strict `json_schema` (arg_fill 1.000 is direct evidence it works on this model).
- **Loop:** one tool-calling agent loop (LangGraph ToolNode pattern): model-node (LM + tools) → tool-node (dispatch/execute) → tool-result message back → repeat, until the model emits **no tool call** (= final answer; the old `none`) or a named exit fires (`answer`, `step_budget`, `duplicate`, `tool_failed`, `validation_failed`). The two-stage classify→fill is **retired**.
- **A literal MCP server endpoint** (for the harness's big model) remains M2, as scoped in the spec — R1 is about the LFM's own tool interface, not a new endpoint.

## Why this is a plan-phase artifact, not a re-recorded approach.artifact
The design approval was recorded at the design→plan advance (digest stamped on `approach.artifact`); re-recording the design artifact would **void** that approval. R1 is a refinement of design §4/§5/§6; the authoritative revised wording lives in `tasks.artifact` (T4/T5), `test-plan.artifact` (TC-4/TC-5), and build contracts 04/05. If the user wants the design document itself amended, `approach.artifact` is re-recorded and the design re-approved (flagged at plan presentation).

## Affected items
- `tasks.artifact`: T4 (state-action → **tool-calling** reliability, + multi-turn scenarios), T5 (two-stage → **tool-calling loop**).
- `test-plan.artifact`: TC-4.x (re-scoped; new TC-4.4 multi-turn), TC-5.x (exits remapped to the tool-calling model; new TC-5.6 transport).
- Build contracts: 04 (→ tool-calling reliability) and 05 (→ tool-calling loop) bodies carry the revised wording.

## New risk added by R1
**Multi-turn tool-calling** (the LFM sees a tool result, then decides next tool vs final answer) is NOT covered by the spike, which validated single-turn classify/fill only. T4 + TC-4.4 exist specifically to prove it before the phase machine (T8) is built on the loop.

---

# R2 — Revision: `contract_read` dropped — the interpreter surface is 12 tools (user-approved 2026-09-03, at plan review)

## What changed
After R1 flattened the surface into one tool list, STATE's `contract_read` duplicated READ's `contract_detail` (design §4 itself noted it "mirrors contract_detail; grouped as state for symmetry"). Two near-identical tools in one flat list is exactly the routing confusion T4 measures on the 2.6B model. Dropped.

- **Surface is now 12 tools:** 9 READ (`code_search`, `symbols`, `knowledge_why`, `knowledge_related`, `knowledge_entities`, `artifact_body`, `contract_detail`, `telemetry`, `get_skill_for`) + 3 STATE (`contract_add`, `artifact_record`, `phase_advance`).
- Contract reading stays demonstrated: `contract_detail` is a read action the LFM invokes directly (AC-4's read group). AC-4's demonstration requirement (the LFM drives a contract add and a phase advance itself) is unchanged.
- The earlier "10 read tools + 4 state tools" wording counted `none` as a tool and `contract_read` twice over; under R1 (`none` = no tool call) + R2, the literal `tools` array has 12 entries.

## Companion plan clarification (not a design change)
T5's state executors target a store *interface* + fakes; the real DuckDB stores are wired at T7/T8; new **TC-7.5** proves interpreter-driven `contract_add`/`artifact_record` persist (and rejected state ops never apply). This closes the T5→T7 sequencing gap without making T5 depend on T7.

## Affected items
- `tasks.artifact`: R2 header note; T4 (12-tool surface + harness dir name supersedes design §6's); T5 (9+3 tools; store-interface executors).
- `test-plan.artifact`: TC-5.6 (12-tool array), TC-7.5 (new), TC-2.3/TC-7.2 clarifications.
- Build contracts 04/05 carry the revised wording when recorded.

Same treatment as R1: design §4's tool inventory is amended by this note + the plan artifacts; `approach.artifact` is NOT re-recorded (re-recording would void the design approval).
