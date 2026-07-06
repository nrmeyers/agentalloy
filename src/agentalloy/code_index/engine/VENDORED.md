# Vendored parsing engine

This directory is a pruned, dependency-cleaned copy of the `codebase_rag`
package from the **codebase-indexer** repo (which itself vendored it from
[code-graph-rag](https://github.com/vitali87/code-graph-rag) — see `LICENSE`,
which travels with the code).

- **Upstream**: `codebase-indexer` repo, `codebase_rag/` package
- **Source commit**: `fb9d14c1ab7687aa266da909f68bc086fce16519`
- **Vendored**: 2026-07-05

Do not edit this code casually — keep diffs against upstream minimal so future
re-vendoring stays tractable. The only agentalloy module allowed to import
from here is `agentalloy.code_index.facade`.

## What was kept

- `parsers/` (all language subpackages: py, js_ts, java, rs, cpp, lua,
  class_ingest, handlers, plus the processor/resolver modules)
- `graph_updater.py`, `language_spec.py`, `parser_loader.py`
- `constants.py`, `models.py`, `schemas.py`, `types_defs.py`, `exceptions.py`
- `logs.py` (pure log-message string constants — no logging machinery)
- `decorators.py` (`recursion_guard` is used by the py/java analyzers)
- `utils/` (`fqn_resolver`, `path_utils`, `source_extraction`)
- `storage/docstring_format.py` → `docstring_format.py`
- `services/__init__.py` protocols (`IngestorProtocol` / `QueryProtocol`) →
  `services/__init__.py` (protocols only)
- The four module-removal Cypher constants `graph_updater` needs →
  `ingest_queries.py` (subset of upstream `cypher_queries.py`)

## What was dropped and why

- `mcp/`, `tools/`, `providers/`, `prompts.py`, `main.py`, `cli.py`,
  `cli_help.py`, `readme_sections.py` — interactive CLI / MCP / LLM surface;
  agentalloy only needs the deterministic parse pipeline.
- `graph_loader.py`, `schema_builder.py`, `cypher_queries.py` (rest of it),
  `config.py` — graph-DB service plumbing; agentalloy persists through its
  own storage layer.
- `embedder.py`, `vector_store*.py`, `centrality.py`,
  `services/contextual_prefix.py`, `utils/dependencies.py` — the in-process
  embedding stack. Embedding in agentalloy always happens externally, so the
  whole embedding pass was removed from `graph_updater.py` (see below);
  `contextual_prefix.py` and `utils/dependencies.py` were only referenced
  from that deleted branch.
- `logs.py` upstream also existed alongside a loguru setup; only the message
  constants survive (see loguru note below).

## Local modifications

1. **loguru → stdlib logging**: every `from loguru import logger` was
   rewritten to import the compat shim in `_logging.py`, which accepts
   loguru's brace-template/kwargs call forms and routes to
   `logging.getLogger("agentalloy.code_index.engine")`. No loguru dependency
   remains.
2. **Config decoupling**: `from .config import settings` (pydantic-settings,
   env-reading) was replaced by `engine_config.py` — a frozen `EngineConfig`
   dataclass with the upstream defaults (AST-cache limits, flush interval),
   passed explicitly through `GraphUpdater`/`BoundedASTCache`. The engine
   reads no environment variables or config files (exception:
   `parsers/parallel_calls.py` keeps upstream's opt-in `PARSE_PARALLELISM`
   env knob, default serial).
3. **Embedding branch removed**: `GraphUpdater._generate_semantic_embeddings`
   and its helpers (`_reconcile_embeddings`, `_extract_source_code`,
   `_build_embed_text`, `_parse_embedding_result`) plus the `skip_embeddings`
   flag are gone. `run()` ends after `flush_all()`; the engine never embeds.
4. **`cache_dir` parameter**: `GraphUpdater(cache_dir=...)` relocates the
   `.cgr-hash-cache.json` / `.cgr-stat-cache.json` sidecars out of the
   indexed repo. Default `None` keeps upstream behaviour (repo root).
5. **Dropped-dependency pruning**: `types_defs.py` lost the interactive-CLI
   types (`prompt_toolkit` Style, `AgentLoopUI`, `*_LOOP_UI`); `models.py`
   lost `rich.Console` and the CLI/MCP models (`SessionState`, `AppContext`,
   `ToolMetadata`).
6. **Deterministic symbol attribution**: `QueryCursor.captures()` returns
   per-capture-name lists that are neither positionally aligned across
   capture names nor stably ordered between cursor runs in one process.
   Upstream zipped `@method_name`/`@member_expr` against
   `@arrow_function`/`@function_expr` (`parsers/js_ts/ingest.py`), swapping
   closure attribution between enclosing scopes nondeterministically —
   replaced by structural pairing on the shared parent node
   (`_pair_captures_by_parent`). Additionally, capture lists that feed
   same-qualified-name emissions (Python `@property`/setter pairs, sibling
   callbacks inheriting one declarator name) are sorted into source order via
   `parsers/utils.py::sort_captures_by_position` (applied in
   `get_function_captures`, `parsers/class_ingest/mixin.py`, and
   `parsers/import_processor.py::parse_imports` — import order decides which
   import's `full_name` last-writes an external Module node's `path`) so
   last-write-wins MERGE semantics pick a stable winner. Relatedly,
   `parsers/stdlib_extractor.py` probed `hasattr(package, name)` to classify
   Python imports, which depends on which submodules are ALREADY loaded in
   the running process (and the extractor's own `import_module` calls mutate
   that state), flipping external-Module rows between parses — it now asks
   the import system for the full dotted name first. Regression tests:
   `tests/code_index/test_engine_determinism.py`.

## Runtime dependencies

stdlib + `tree_sitter` and grammar packages, `pydantic` (schemas), `toml` +
`defusedxml` (dependency_parser), `docstring-parser` (docstring_format).
All declared under the `[code-index]` optional extra in the repo
`pyproject.toml`.
