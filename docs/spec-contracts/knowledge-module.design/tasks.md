# Knowledge Module — Slice 1 — Task Plan

> Runtime home: `docs/design/knowledge-module/tasks.md` (git-ignored). Committed
> copy. Each task is one dominant tech surface and becomes one build contract
> (`../knowledge-module.build/NN-*.md`, ≤ 2 `domain_tags`). Slice 1 closes AC 1–5,
> 7–9; **AC 6 (injection) is slice 2 and not in this plan.**

## Tasks

1. **Store: `GOVERNS` edge read/write + decision queries** *(surface: code-index
   graph store)* — In `graph_store.py` (+ `storage/protocols.py` protocol): add
   `symbols_by_name(name)` (`SELECT qualified_name, kind FROM symbols WHERE name=?
   AND kind!='MarkdownDoc'`), `governing_decisions(fqn)` (the `callers()` SQL with
   `kind='GOVERNS'`, reading `e.src` + `LEFT JOIN symbols`), and a doc-scoped
   `delete_govern_edges_for_doc(doc_path)` helper (`DELETE FROM edges WHERE
   kind='GOVERNS' AND file_path=?` — the doc-granular prune DK6 requires).
   `GOVERNS` is a free-form `kind` — **no DDL**.
   Closes **AC 1**; enables AC 4, AC 5. Build contract `01`.

2. **Ingest: `_index_decisions` phase (linkage extraction)** *(surface: code-index
   ingest / symbol-linkage)* — Make `_index_markdown` **return** its
   `changed`/`removed` sets (yields only a count today), then add a new phase in
   `pipeline.py` run **after** it in `run_index_job`. Compute **affected docs** =
   every allow-listed source path (`docs/solutions/*.md`,
   `docs/design/*/approach.md`, `docs/spec-contracts/*.design/approach.md`;
   `docs/ship`/`docs/qa` excluded by default) with ≥1 chunk in `changed ∪ removed`.
   For each affected doc, **doc-granular**: delete all its `GOVERNS` edges
   (`file_path = doc`) then re-derive over **every current decision chunk** — a
   candidate becomes a decision iff DK2 linkage yields ≥1 governed symbol. Linkage
   = inline-code spans → exact-fqn (non-markdown) then unambiguous *code-shaped*
   short name (DK2), dropping ambiguous/non-code-shaped/unresolved. Establishes the
   AC 2 allow-list *mechanism* (the negative guard is Task 5). Closes **AC 3,
   AC 4**; contributes **AC 1, AC 2**. Build contract `02`.

3. **Query route: `governing_decisions` structural + `DecisionView`** *(surface:
   code-index FastAPI router)* — Extend `_STRUCTURAL_QUERIES`/`_FQN_QUERIES` and
   the `search_structural._run` if-ladder with a `governing_decisions` branch
   returning a new `DecisionView` (`api/models.py`:
   `qualified_name, file_path, start_line, heading, snippet`). Closes **AC 5**.
   Build contract `03`.

4. **CLI: `agentalloy knowledge why <symbol>` pull verb** *(surface: CLI
   subcommand)* — Register a **new top-level `knowledge` subparser group**
   (`install/subcommands/` + `install/__main__.py` import/`_SUBCOMMANDS`), with a
   `why` verb (alias `for <path>`) whose httpx handler mirrors `code.py`'s
   `_run_structural`: GET `/code/search/structural?query=governing_decisions&fqn=…`
   and print one decision per line (`path::anchor  file:line  heading`). Distinct
   CLI namespace, shared route (DK7). Closes **AC 7**. Build contract `04`.

5. **Guards + docs** *(dominant surface: tests; trailing docs note)* — The
   **AC 2** allow-list guard (decision sources are only the allow-listed lifecycle
   paths; the path reads/writes **neither** `docs/architecture-decisions/` **nor**
   `CLAUDE.md` — the negative Task 2 doesn't itself assert); a no-network/
   no-paid-LLM guard (embed base_url is localhost; the decision index/query path
   imports no `engine.constants` provider enum); a read-path guard (decision text
   still returns from `agentalloy code search`; the diff writes nothing to
   `code_index/engine/` or `_corpus/`); a boundary guard (no corpus skill
   row/vector is written by the decision path — AC 9); and a trailing
   README/`docs/code-index.md` note framing slice 1 as the Knowledge module's first
   indexed layer. Closes **AC 8, AC 9**; delivers the **AC 2** guard. Build
   contract `05`.

**Order & dependencies.** 1 → 2 (edge writes + doc-granular prune need the store
methods). 1 → 3 (the route needs `governing_decisions`). 3 → 4 (the CLI is an HTTP
client of the route). 5 is verification/docs, last. Tasks 1–2 (write-side) and
3–4 (read-side) are otherwise independent and can proceed in parallel once 1 lands.
Spikes DK1–DK6 are resolved in `approach.md`. **Not here:** AC 6 injection, the
`search_similar` kind-filter, supersession/`status`, and rename re-resolution —
slice 2 / deferred per spec Out of Scope.
</content>
