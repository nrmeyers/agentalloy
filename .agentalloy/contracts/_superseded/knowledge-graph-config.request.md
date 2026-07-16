# Request: Productionize Knowledge Graph Configuration

## Description
Add the "Knowledge Graph" as a third option in the agentalloy installation/configuration process. Users should be able to toggle this feature on or off via the CLI without performing a full re-installation.

## Goals
- **Third Install Option:** Add "Knowledge Graph" to the installation prompt/menu.
- **Togglable State:** Implement a mechanism to enable/disable the Knowledge Graph feature in the local configuration.
- **CLI Interface:** Provide a command (e.g., `agentalloy config knowledge enable/disable`) or similar to manage this state.
- **Non-Destructive:** Enabling/disabling the feature should not trigger a full re-installation of other components (Instructions, Code Context).

## User Story
As a developer, I want to turn on the Knowledge Graph feature to leverage decision-driven context during my work, but I want to be able to disable it quickly if I encounter issues or need a leaner environment, without re-running the entire setup process.

## Acceptance Criteria
1. **Installation Update:**
    - [ ] The `agentalloy install` (or equivalent subcommand) includes a third option for "Knowledge Graph".
    - [ ] Selecting this option updates the local configuration to enable the feature.
2. **Configuration Management:**
    - [ ] A user can check the current status of the Knowledge Graph feature.
    - [ ] A user can enable the Knowledge Graph feature via the CLI without a full install.
    - [ ] A user can disable the Knowledge Graph feature via the CLI without a full install.
3. **Implementation:**
    - [ ] The "Knowledge Graph" state is persisted in the local configuration (similar to how `CODE_INDEX_ENABLED` is handled).
    - [ ] The internal service/indexing logic respects the new configuration state.

## Out of Scope
- Automatic re-indexing upon enabling the feature (this is a separate, subsequent task).
- UI changes to the web frontend (this task focuses on CLI/Configuration).
- Migration of existing indices for users who enable the feature post-install.
