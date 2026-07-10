# Code index module

The optional second context module (`src/agentalloy/code_index/`): a tree-sitter
symbol graph plus hybrid semantic/lexical search over the operator's own repos,
served under `/code/*` on the main service port (47950). No separate process,
no separate port — the routers register on the same FastAPI app as compose.

- **Toggle**: `CODE_INDEX_ENABLED=1` (default off). The setup wizard's module
  selection writes `COMPOSE_ENABLED` / `CODE_INDEX_ENABLED` into the user `.env`.
- **Dependencies**: behind the `[code-index]` extra (`uv tool install
  'agentalloy[code-index]'`) — tree-sitter + per-language grammars. The core
  wheel stays lean; with the toggle on but the extra missing, the service
  starts anyway and `/health` reports `modules.code_index == "unavailable"`.
  The container image ships the extra preinstalled, so
  `podman run -e CODE_INDEX_ENABLED=1 …` is all it takes there.
- **Engine**: the parsing engine is vendored under `code_index/engine/`
  (see its `VENDORED.md`); it keeps upstream style and is excluded from strict
  pyright, while the facade boundary (`facade.py`) and everything else in the
  module is strict-checked.
- **Embeddings**: reuses the service's embed client (nomic-embed-text-v1.5,
  768-dim, same llama-server as the skill corpus).

## Endpoints

All routes are prefixed `/code` and registered only when the module is enabled
(disabled module → 404, not 503).

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/code/index` | Start an async index job for a `repo_path` (202; 409 if a job for the repo is active) |
| GET | `/code/index/jobs` | List index jobs, newest first |
| GET | `/code/index/{job_id}/status` | One job's status |
| POST | `/code/index/{job_id}/cancel` | Request cancellation of an active job |
| DELETE | `/code/index/{repo_slug}` | Remove an indexed repo (store directory + registry row) |
| GET | `/code/repos` | List indexed repos |
| GET | `/code/repos/{slug}/stats` | Per-repo graph/vector stats (kind counts, top centrality, vector count) |
| POST | `/code/repos/{slug}/reindex` | Force a full reindex using the registry's stored repo path |
| POST | `/code/repos/{slug}/watch` | Enroll/unenroll the repo for file-watching (`{"enabled": bool}`); the running service starts/stops its observer immediately |
| GET | `/code/search/semantic` | Hybrid semantic search (dense + pagerank fusion + RRF/BM25) |
| GET | `/code/search/lexical` | BM25-only lexical search |
| GET | `/code/search/symbol` | Exact symbol lookup by fully-qualified name |
| GET | `/code/search/files` | List indexed file paths (prefix-filterable) |
| GET | `/code/search/centrality` | Top-pagerank symbols with locations |
| GET | `/code/search/structural` | Named graph queries: `callers`, `callees`, `transitive_callers`, `counts_by_kind` |
| GET | `/code/symbols/{fqn}` | One symbol's full graph row |
| GET | `/code/symbols/{fqn}/callers` | Call sites of a symbol (`depth` > 1 walks transitively) |
| GET | `/code/symbols/{fqn}/callees` | Symbols a function calls |
| POST | `/code/context-bundle` | Assemble a budgeted code context for a task (`budget_chars`) |

## CLI

`agentalloy code …` is a thin HTTP client for the endpoints above (it ships in
the core wheel — only the service needs the extra):

```
agentalloy code index [path] [--force] [--wait]     Start (and follow) an index job
agentalloy code status                              Indexed repos + active jobs + staleness
agentalloy code search <query> [--repo] [--lexical] [-k N]
agentalloy code symbol <fqn> [--repo]
agentalloy code callers <fqn> [--depth N]           Call sites (transitive with --depth)
agentalloy code callees <fqn>
agentalloy code bundle <task>                       Budgeted context bundle
agentalloy code remove [path]                       Remove a repo's index (confirms; --yes)
agentalloy code watch enable|disable [path]         Per-repo watch enrollment (live)
agentalloy code watch status                        Master switch + enrolled repos
agentalloy code watch start|stop                    How to flip the CODE_INDEX_WATCH master switch
```

## Storage layout

Per-repo stores live outside the skill corpus, under
`~/.local/share/agentalloy/code_index/` (override: `CODE_INDEX_DATA_DIR`):

```
code_index/
  jobs.sqlite                    # shared jobs / events / indexed-repos registry
  repos/{slug}/graph.duck        # DuckDB symbol graph (source of truth)
  repos/{slug}/vectors.lance     # LanceDB vector ANN + native BM25 (derived)
  repos/{slug}/cache/            # engine hash/stat sidecar caches
```

`{slug}` is canonical (`code_index/slug.py`): a repo whose single `origin`
remote is a github.com URL slugs to `{org}__{repo}`; anything else falls back
to the directory basename (filesystem-safe charset enforced).

## Incremental indexing

Every symbol row carries a SHA-1 content hash of its embed text. A non-force
re-index re-parses the tree but skips embedding for symbols whose hash is
unchanged, and diffs the symbol sets to delete removed rows — so a re-run on a
lightly-changed repo is cheap. `--force` (or `POST /code/repos/{slug}/reindex`)
rebuilds from scratch.

### Freshness: watch and staleness

Two mechanisms, both opt-in, neither auto-reindexes behind your back:

- **Watch** — two switches must both be on: `CODE_INDEX_WATCH=1` (the master
  switch, service-level env) and per-repo enrollment (`agentalloy code watch
  enable [path]`, persisted on the repo's registry row). Enrolling/unenrolling
  reaches the running service immediately; on startup the service watches all
  enrolled repos. Changes trigger a debounced incremental reindex.
- **Staleness nudge** — `agentalloy code status` compares each repo's stored
  `head_sha` against its current `git rev-parse HEAD` and shows
  `[stale — N commits behind; run agentalloy code index <path>]`; the service
  logs one INFO line per stale repo at startup. Nothing reindexes
  automatically — watch is the opt-in for that.

## Knowledge layer (decisions)

A typed decision layer rides on the same graph — no separate store, no new
process. When a repo is indexed, heading-chunks of its Markdown design/solution
docs are ingested as `MarkdownDoc` symbols (id `path::anchor`), and each one that
names a code symbol gets a `GOVERNS` edge to it. `GOVERNS`/`MarkdownDoc` are
free-form kinds — no DDL or enum change; the decision layer is metadata over the
existing symbol/edge tables.

- **Query** — `agentalloy knowledge why <fqn>` (a distinct CLI namespace from
  `code`) surfaces the decisions governing a symbol, via the
  `query=governing_decisions` structural query on `/code/search/structural`.
- **JIT decision push** — at **design/build**, when a work-item contract's
  `scope.touches` covers a governed file, the governing decisions' rationale is
  folded into the composed context on the **cursor-entry turn** (once per
  work-item, same cadence as the Tier-2 domain leg). It lands in the last user
  message like every other tier — never the prompt-cached system block. The path
  is strictly additive and fail-closed: if the module is off, the repo unindexed,
  the phase wrong, or the scope empty, composition is byte-identical to before.
  A promoted lesson skill already present in the turn's composed text dedups its
  own decision out (no double-inject); an `approach.md` decision (which is never
  promoted) always pushes.

## Harness wiring

`agentalloy add` (or the deprecated `agentalloy wire`) writes a second sentinel block —
`<!-- BEGIN agentalloy code-index --> … <!-- END agentalloy code-index -->`,
independent of the main install block — into the repo's agent-instruction file
(`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.clinerules`, or a dedicated
`.cursor/rules/agentalloy-code-index.mdc`). It is written only when the module
is enabled AND the local service reports `modules.code_index == "enabled"`;
`unwire`/`uninstall` sweep it, and a legacy standalone `codebase-indexer`
block is migrated in place.

Beyond the static wiring block, the `sys-code-index` system skill injects the
same pull guidance just-in-time through the proxy at design/build/qa — only
when the request's repo has a completed index in the indexed-repos registry
(fail-closed: module off, unindexed repo, or an unreadable registry all mean
no injection).

Wiring an unindexed repo (via `agentalloy add` or `wire`) offers to index it on
the spot (`[Y/n]`; `--yes` and non-TTY submit by default — the job runs async,
wiring never waits).
`unwire` asks whether to also remove the repo's index and defaults to **keep**
(indexes are expensive to rebuild; removing the block is not a statement about
the data). Pass `--remove-index` to remove it non-interactively; removal is
refused while an index job is active.
