---
phase: intake
task_slug: codex-provider-toml-dependency
route: fast
domain_tags: [install]
scope:
  touches:
    - src/agentalloy/providers/codex/install.py   # BUG SITE: module-level `import toml` (line 28); uses toml.loads (line 56) + toml.dumps (line 74)
    - pyproject.toml                              # `toml>=0.10.2` lives only in the [code-index] extra — core wheel doesn't ship it
    - tests/                                      # regression guard: provider loading must not depend on optional-extra imports
  avoids:
    - src/agentalloy/code_index/engine/parsers/dependency_parser.py  # legitimately behind the [code-index] extra; not broken
    - swapping the codex config format                                # codex owns config.toml; we only read/merge/write it
  success_criteria:
    - On a bare install (`uv tool install agentalloy`, no extras), `agentalloy --v` prints the version with NO "provider 'codex' failed to load" warning, and the codex provider loads and wires.
    - Reading uses stdlib `tomllib` (py312); writing either uses a core-declared writer dep (e.g. tomli-w) or `toml` is promoted to core deps — one policy, decided at design, no dual path.
    - A test loads every provider in an environment without optional extras (or statically asserts provider modules import cleanly against core deps only), so an extras-only import can't ship again.
    - Shipped-surface change (src/ + possibly dep pins) → version bump + patch release per RELEASE.md.
related_contracts:
  - contracts/intake/container-module-env-propagation.md  # previous cycle; unrelated code, same release train
created_at: 2026-07-07T15:55:00Z
---

# codex-provider-toml-dependency

## What the user actually wants

`agentalloy --v` (and every CLI invocation) on a bare v6.3.0 install prints
`provider 'codex' failed to load: No module named 'toml'`. The codex provider
is dead on any install without the `[code-index]` extra.

Root cause: `providers/codex/install.py` does a module-level `import toml`,
but the `toml` package is declared only under the `[code-index]` optional
extra (added for `dependency_parser.py`). Core provider code silently grew a
dependency on an optional extra. The container image ships the extra, so this
never surfaced there — it bites exactly the native `uv tool install` path.

Fix the import so the provider loads on core deps alone, and add a guard so
no provider can ever depend on extras-only packages again.

## Reproduction

```
uv tool install agentalloy   # bare, no extras (the default native path)
agentalloy --v
# → provider 'codex' failed to load: No module named 'toml'
```

Observed live on ai-server after upgrading to 6.3.0.
