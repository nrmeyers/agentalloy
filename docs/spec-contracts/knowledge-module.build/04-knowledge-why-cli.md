---
phase: build
task_slug: 04-knowledge-why-cli
route: full
domain_tags:
  - cli-subcommand
scope:
  touches:
    - "src/agentalloy/install/subcommands/knowledge.py"
    - "src/agentalloy/install/__main__.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/signals/**"
    - "src/agentalloy/api/proxy_injection.py"
created_at: 2026-07-09T00:00:00Z
---

# 04-knowledge-why-cli

## Task

Add the AC 7 pull front door as a **new top-level `knowledge` subparser group**
(DK7 — a distinct CLI namespace over the shared `/code` route holds the module
boundary; `code why` was rejected for burying a Knowledge verb in Code).

- New `install/subcommands/knowledge.py` exposing `add_parser(subparsers)`, a
  `knowledge` group with a `why <symbol>` verb (alias `for <path>`). The handler
  mirrors `code.py`'s `_run_structural`: resolve the repo slug, GET
  `/code/search/structural?query=governing_decisions&fqn=…` on the local service,
  and print one decision per line (`path::anchor  file:line  heading`).
- Register the module in `install/__main__.py` (import + `_SUBCOMMANDS`).

Pure httpx client of the local service (no direct store access), matching the
existing `code` verbs.

## Test cases

- TC6 (AC 7): `agentalloy knowledge why <symbol>` prints governing decisions and
  exits 0; ungoverned fqn prints nothing, exits 0. Distinct namespace from `code`.
</content>
