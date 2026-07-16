# Contract: Knowledge Module Dogfooding

## Objective
Verify that the AgentAlloy Knowledge module correctly identifies and links decisions made in documentation to specific symbols in the codebase.

## Requirements
- [ ] A markdown file containing a clear "Decision:" header and an associated code symbol.
- [ ] The symbol must be present in the codebase and documented in a way that the knowledge engine can associate them.
- [ ] The `agentalloy knowledge why <symbol>` command must return the correct decision snippet.

## Acceptance Criteria
- [ ] `agentalloy knowledge why <symbol>` returns a non-empty response containing the decision text.
- [ ] The response correctly references the source file.
- [ ] The query succeeds even if the indexing process was just triggered.

## Out of Scope
- Testing the Knowledge Graph configuration (this is a separate feature).
- Testing decision extraction from non-markdown files.
