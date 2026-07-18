---
phase: build
task_slug: 05-guards-and-docs
route: full
domain_tags:
  - tests
  - docs
scope:
  touches:
    - "tests/**"
    - "docs/**"
    - "README.md"
  avoids:
    - "src/agentalloy/code_index/engine/**"
    - "src/agentalloy/_corpus/**"
    - "src/agentalloy/api/proxy_injection.py"
created_at: 2026-07-09T00:00:00Z
---

# 05-guards-and-docs

## Task

The boundary/regression guards (dominant surface: tests) plus a trailing docs
note. No production code beyond tests and docs.

- **AC 2 guard:** decision sources are only the allow-listed lifecycle paths; the
  decision path reads/writes **neither** `docs/architecture-decisions/` **nor**
  `CLAUDE.md` (the negative task 02 does not itself assert).
- **AC 8 guard:** decision text stays retrievable via `agentalloy code search`
  with the decision index built; the diff writes nothing under
  `code_index/engine/` or `_corpus/`; the decision index/query path makes no
  cloud/paid-LLM call — embed base_url is localhost (`config.py`) and the live
  path imports no `engine.constants` provider enum.
- **AC 9 guard:** `GOVERNS` edges + decision rows live only in the code-index
  store; the decision path writes **no** skill row/vector to the corpus (no
  auto-install).
- **Docs:** a trailing README / `docs/code-index.md` note framing slice 1 as the
  Knowledge module's first indexed layer.

## Test cases

- TC2 (AC 2), TC7 (AC 8), TC8 (AC 9) from the design test-plan. `code_index/engine/`
  and `_corpus/` untouched by the whole slice-1 diff.
</content>
