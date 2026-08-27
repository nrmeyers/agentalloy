## Context

Runtime bug-hunt fixes across the AgentAlloy `.py` surface (API, code-index engine, install, storage, web, retrieval). The entity-extraction feature itself already shipped in v9.1.0 (#622); several of these fixes patch bugs in that code.

## Runtime bug fixes

A bug hunt over the runtime `.py` surface (YAML excluded) found and fixed logic/correctness bugs. Highlights by area:

- **API / proxy** — corrected proxy/streaming + state routing bugs.
- **Code index** — entity dedup, lock-cancel orphan, qualified-name escaping; engine fixes for relative imports, cpp-include, nested-class calls, dunder trie, override detection; dedup of double-registered JS/TS fn-expr/arrow FUNCTION nodes (3.7-O).
- **Entity extraction** — bug fixes in the v9.1.0 entity-extraction code (`entity_extract.py`, `knowledge_push.py`).
- **Install / server** — container restart, env handling, state parsing, flock, health gates; clamp docker `--time` to whole seconds so a sub-second `--timeout` is honoured as ~1s instead of collapsing to `0` (4.10).
- **Storage** — closed lock/coordinate-space gaps in state store, retrieval, signals.
- **Web** — SPA traversal containment, env reload unset pass, tool-name guard, aider inner-sha.
- **Retrieval** — aligned the card-boost test with documented fused-position semantics.
- **Dead code** — removed the dead ship-watcher and manual install stub.

## Tests

- New regression tests for each V-verified fix (JS/TS fn-expr/arrow dedup, docker `--time` clamp, and the earlier HIGH/MEDIUM findings).
- Full suite green: 5595 passed, 3 skipped.

## Version

`fix` → patch. The Version Bump workflow re-derives from v9.1.0 (current main) to **v9.1.1**.
