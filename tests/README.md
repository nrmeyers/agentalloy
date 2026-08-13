# Test Suite

This directory contains the test suite for AgentAlloy, organized by product module to match the current architecture (v8.17.0+).

## Structure

The suite is organized into module-aligned packages that mirror `src/agentalloy/`:

### Core Runtime Surfaces

- **`proxy/`** — LLM proxy surfaces (OpenAI chat, Anthropic Messages, OpenAI Responses), embeddings passthrough, signal evaluation, injection, knowledge push, phase discipline enforcement
- **`composition/`** — Compose/retrieve orchestration, retrieval legs (system/domain), RRF fusion, rerank, LM assist, applicability filtering
- **`signals/`** — Signal layer units: predicates, gates, classifier, phase graph, skill loader, prefilter
- **`lifecycle/`** — SDD lifecycle flows through proxy+state+signals (phase confirmations, cursors, sessions, lessons, codify gates)

### Modules

- **`code_index/`** — Code index module (tree-sitter engine, ingest, stores, retrieval, routers)
- **`storage/`** — Storage engines (DuckDB skill/state/telemetry stores, Lance fragment store, stream-id)
- **`telemetry/`** — Telemetry writers and stores
- **`corpus/`** — Corpus ingestion, pack validation, corpus reduction, dedup, reembed
- **`contracts/`** — Task contract model and serialization
- **`api/`** — FastAPI routers (state, contracts, health, diagnostics, skills, module toggle, code-index gate)
- **`providers/`** — Harness provider registry and per-harness tests
- **`web/`** — Web UI backend APIs (config, skills, repos, wizard, ops)
- **`core/`** — Top-level app modules (config, runtime state, LM client, profiles, watch)

### CLI and Installation

- **`install/`** — CLI subcommands organized by concern:
  - `wiring/` — Harness wiring (per-harness proxy wiring, code-index wiring, worktree auto-wire)
  - `setup/` — Setup wizard, detection, customization, preflight
  - `service/` — Server lifecycle, service management, ports, backup/restore
  - `container/` — Container runtime, entrypoint readiness
  - `upgrade/` — Upgrade/update, install-state schema, release check
  - `doctor/` — Doctor, verify, CLI surface shape
  - `packs/` — Pack install/validate/gates, lessons, bundled pack manifests
  - `cli/` — Remaining per-subcommand coverage (phase, workflow, approve, contract, code, knowledge, stream, config, worktree, cleanup, wrap, models, MCP, uninstall)

### Environment-Gated Suites

- **`integration/`** — Integration tests requiring live embed runtime (marked `@pytest.mark.integration`)
- **`container/`** — Container tests requiring podman (marked `@pytest.mark.container`)
- **`harness_e2e/`** — End-to-end harness tests (marked `@pytest.mark.harness_e2e`)
- **`benchmarks/`** — Performance benchmarks and latency budgets

### Support

- **`eval/`** — Eval harness graders and benchmarks (tests for the `eval/` package, not product tests)
- **`fixtures/`** — Shared test fixtures (classifier calibration data)
- **`manual/`** — Manual testing scripts (not collected by pytest)
- **`conftest.py`** — Root conftest with hermeticity tripwires, app fixtures, dynamic marker assignment
- **`support.py`** — Shared test utilities (StubLMClient, fake_fragment, seed_phase)
- **`_wire_compat.py`** — Wire compatibility shim for deprecated `wire_harness()` API

## Test Markers

- **`integration`** — Tests requiring a live embed runtime (llama-server on `:47951`). Excluded from default runs.
- **`container`** — Tests requiring podman. Excluded from default runs.
- **`harness_e2e`** — End-to-end harness tests requiring real harness binaries. Excluded from default runs.
- **`xdist_group`** — Tests that must run serially (e.g., tests binding to `:47950`).

## Running Tests

```bash
# Default suite (excludes integration, container, harness_e2e)
uv run pytest

# With parallel execution
uv run pytest -n auto

# Specific markers
uv run pytest -m integration
uv run pytest -m container
uv run pytest -m harness_e2e

# Specific directories
uv run pytest tests/proxy/
uv run pytest tests/install/wiring/
```

## CI Gates

The CI quality gate runs:
```bash
uv run pytest -m "not integration and not container"
```

Plus the pack version bump guard:
```bash
uv run pytest tests/test_pack_version_bump_guard.py::test_pack_version_bump_guard
```

## Hermeticity

The root `conftest.py` contains incident-driven tripwires that protect the developer's real dogfooded install from test side effects. These guards are load-bearing and must not be removed without understanding the incidents they prevent (#87, #88, #114, #118).

## Design Principles

1. **Module-aligned structure** — One test package per product module, so a new reader can map tests to code.
2. **Preserved incident coverage** — Tests encode real product behavior and incident fixes; the restructure consolidated fragmented slices without discarding coverage.
3. **Coherence over count** — Merged parametrized near-duplicates and retired genuine staleness; kept healthy behavioral coverage.
4. **Dynamic marker compatibility** — File basenames are preserved so conftest's nodeid-substring marker assignment continues to work.
