# Knowledge Management Productionization Design

## Overview
Integrate the Knowledge Management (Knowledge Graph + Code Index) feature into the `agentalloy` CLI for easy setup, configuration, and management.

## Architecture
- **Configuration**: Handled via `.env` file using `KNOWLEDGE_GRAPH_ENABLED=True` and `CODE_INDEX_ENABLED=True`.
- **Services**:
    - `code-index`: Managed via `agentalloy config start/stop code-index`.
    - `knowledge-graph`: Managed via `agentalloy config start/stop knowledge-graph`.
- **Integration**:
    - `agentalloy setup` will prompt for these as modules.
    - `agentalloy upgrade` will provide a non-intrusive reminder if they are disabled.

## Detailed Design

### 1. Configuration Subcommand (`src/agentalloy/install/subcommands/config.py`)
- **Feature Mapping**:
    - `"knowledge-graph" -> "KNOWLEDGE_GRAPH_ENABLED"`
    - `"code-index" -> "CODE_INDEX_ENABLED"`
- **New Subcommands**:
    - `start <feature>`:
        1.  Ensures the feature is enabled in `.env`.
        2.  Launches the background service.
            - For `code-index`: Runs `agentalloy code-index --serve` (placeholder, need to verify).
            - For `knowledge-graph`: Runs `agentalloy knowledge-graph --serve` (placeholder, need to verify).
        3.  Reports status via `write_result`.
    - `stop <feature>`:
        1.  Stops the running service (using `runtime_artifacts.reap` or `systemctl stop`).
        2.  Reports status.

### 2. Setup Wizard (`src/agentalloy/install/subcommands/simple_setup.py`)
- **Module Options**:
    - `injector` (default)
    - `code-index`
    - `knowledge-graph`
    - `both` (maps to both being enabled)
- **Implementation**:
    - Update `_prompt_modules` to include `knowledge-graph`.
    - Update `_module_env_overrides` to map `knowledge-graph` selection to `KNOWLEDGE_GRAPH_ENABLED=1` and `COMPOSE_ENABLED=1`.

### 3. Upgrade Workflow (`src/agentalloy/install/subcommands/upgrade.py`)
- **Reminder Logic**:
    - At the end of the `upgrade_native` flow, check the `.env` file for `KNOWLEDGE_GRAPH_ENABLED=True`.
    - If missing/false, append a `[dim]Tip: Enable Knowledge Management with: agentalloy config enable knowledge-graph[/dim]` message to the summary.

## Implementation Steps
1.  **Identify service launch commands**: Use `terminal` to find the exact command to start the `code-index` and `knowledge-graph` services.
2.  **Update `config.py`**: Add subparsers, feature mappings, and implementation for `start`/`stop`.
3.  **Update `simple_setup.py`**: Update module prompting and environment variable mapping.
4.  **Update `upgrade.py`**: Implement the reminder logic.
5.  **Verification**: Run tests and manual CLI checks.
