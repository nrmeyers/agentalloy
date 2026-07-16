# Request Specification: Knowledge Module Dogfooding
## Phase: Intake

### 1. Objective
Verify the current implementation of the `knowledge` module by enabling it locally and performing end-to-end verification (capture $\rightarrow$ index $\rightarrow$ query).

### 2. Scope
- **Code Review:** Inspect `src/agentalloy/api/knowledge_push.py`, `src/agentalloy/code_index/ingest/markdown.py`, and `src/agentalloy/code_index/store/graph_store.py`.
- **Local Activation:** Use environment overrides (`CODE_INDEX_ENABLED=1`) to enable the module on the current host.
- **Verification:** 
    - Trigger `agentalloy code index`.
    - Confirm `agentalloy knowledge why <symbol>` returns correct decision text linked to the symbol.

### 3. Constraints
- **No Public Changes:** Changes must be local-only (environment variables or local `.env`) to avoid impacting other users.
- **No Code Changes (Initially):** Verification should rely on existing code; only if a bug is found should we move to the `spec` phase for a fix.

### 4. Success Criteria
- `agentalloy knowledge why <symbol>` returns a non-empty, relevant decision record.
- The indexing job completes successfully (0% $\rightarrow$ 100%).
