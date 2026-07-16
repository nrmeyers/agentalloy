# Intake: Knowledge Management Productionization

## Request Overview
The Knowledge Management feature (Code Index and Knowledge Decision Graph) has been validated locally and now needs to be productionized within the `agentalloy` CLI.

## Core Requirements
1.  **Setup Integration**: Add `knowledge-graph` as a third option in the `agentalloy setup` interactive wizard.
2.  **Configuration Management**: Implement new `agentalloy config start/stop [OPTION]` subcommands to manage the lifecycle of the code index and knowledge decision graph services.
3.  **Upgrade UX**: Add a reminder at the end of the `agentalloy upgrade` command to notify users how to enable the feature if it is not currently active in their configuration.

## Context
- The feature includes a code indexer and a knowledge decision graph.
- The implementation must integrate with the existing `.env` configuration system.
- The `upgrade` reminder should only appear if the feature is disabled.
