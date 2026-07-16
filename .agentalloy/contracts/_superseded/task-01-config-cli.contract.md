# Build Contract: Task 01 - Configuration and CLI Management
**Task ID:** `TASK-01`
**Domain Tags:** `[cli, python]`
**Goal:** Implement the `config` command group and the `knowledge_graph_enabled` configuration flag.

## Success Criteria
- [ ] The `agentalloy config status` command returns the current state of `knowledge_graph_enabled`.
- [ ] The `agentalloy config enable/disable knowledge-graph` commands update the persistent configuration file.
- [ ] All existing configuration flags remain unchanged and accessible.

## Verification Steps
1.  **Unit Tests:** Run `pytest tests/test_config.py` (to be created) to validate the configuration model.
2.  **CLI Integration:** Execute `agentalloy config status` and verify the output matches the configuration state.
3.  **Persistence Check:** Manually inspect the configuration file to ensure changes are saved.
