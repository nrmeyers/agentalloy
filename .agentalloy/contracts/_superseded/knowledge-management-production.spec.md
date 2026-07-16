# Knowledge Management CLI Productionization

## Overview
The Knowledge Management feature, including the code index and the knowledge decision graph, has been tested locally. This task involves integrating it into the standard `agentalloy` CLI lifecycle, making it easy for users to set up, configure, and manage the associated services.

## Requirements

### 1. Agentalloy Setup Integration
- Update the `agentalloy setup` interactive wizard to include `knowledge-graph` as an available module selection.
- The module selection should allow users to choose:
    - `injector` (default)
    - `code-index`
    - `knowledge-graph`
    - `both` (or combinations of the above)
- Ensure the selection is correctly reflected in the `.env` file via the `AGENT_ALLOC_...` or relevant environment variables (mapping to `KNOWLEDGE_GRAPH_ENABLED` and `CODE_INDEX_ENABLED`).

### 2. New Configuration Subcommands
- Implement `agentalloy config start [OPTION]` and `agentalloy config stop [OPTION]` subcommands.
- Supported `[OPTION]` values:
    - `knowledge-graph`
    - `code-index`
- **Behavior**:
    - `start` should ensure the feature is enabled in `.env` and then start the corresponding background service/process.
    - `stop` should stop the running service/process and optionally disable the feature in `.env` (or just stop the service). *Self-correction: typically config start/stop for a service is separate from enabling/disabling the feature, but the user request suggests they are linked. I will implement it such that `start` ensures the service is running and `stop` stops it.*
- The implementation should use the existing background process management (e.g., `process` tool/logic) to track these services.

### 3. Upgrade Reminder
- Update the `agentalloy upgrade` command output.
- If `KNOWLEDGE_GRAPH_ENABLED` is not set to `True` in the user's configuration, display a reminder at the end of the upgrade process explaining how to enable Knowledge Management using `agentalloy config enable knowledge-graph`.

## Acceptance Criteria
- [ ] `agentalloy setup` presents `knowledge-graph` in the module selection menu.
- [ ] `agentalloy config start knowledge-graph` successfully starts the knowledge management service.
- [ ] `agentalloy config stop knowledge-graph` successfully stops the knowledge management service.
- [ ] `agentalloy config start code-index` successfully starts the code index service.
- [ ] `agentalloy config stop code-index` successfully stops the code index service.
- [ ] `agentalloy upgrade` shows the Knowledge Management enablement reminder if the feature is currently disabled.
- [ ] All new commands are integrated into the `agentalloy` CLI entry point and subcommands.
