# Knowledge Module — Slice 2 (JIT Injection) — Task Plan

> Runtime home: `docs/design/knowledge-module-injection/tasks.md` (git-ignored).
> Committed copy. Each task is one dominant tech surface and becomes one build
> contract (`../knowledge-module-injection.build/NN-*.md`, ≤ 2 `domain_tags`).
> Slice 2 closes **AC 6** (boundary **AC 9**); it stacks on slice 1 (PR #379).

## Tasks

1. **Store: `decisions_for_files` join** *(surface: code-index graph store)* — In
   `graph_store.py` (+ `storage/protocols.py`): `decisions_for_files(file_paths)
   -> list[DecisionRow]` — one indexed join `edges(kind='GOVERNS') ⋈ symbols dst
   ON dst.file_path ∈ files`, hydrating the decision `src` chunk. The file-scoped
   analogue of slice 1's `governing_decisions`; no DDL. Closes part of **AC 6**
   (the data). Build contract `01`.

2. **Resolution + filters: `knowledge_push` helper** *(surface: decision-injection
   /symbol-linkage)* — New `src/agentalloy/api/knowledge_push.py`, lazy-importing
   code-index code behind the `code_index_enabled` check. Given a parsed
   `Contract`, repo slug, **and the already-composed tier-2 domain text**, it:
   turns `scope.touches` into `path_globs` via `code_index_query_params`
   (`contracts.py:385`), resolves those globs → files with `graph.list_files` +
   `fnmatch` short-circuited at `_MAX_TOUCH_FILES` (DK3/DK6); calls
   `decisions_for_files`; **defers** a `docs/solutions/<slug>.md`-sourced decision
   iff a `_sanitize_skill_id(<slug>)` fragment is already present in that composed
   text (DK4 — dedup against actual composition, so **no skill-store handle
   needed**); applies the **inert** `_is_superseded` filter (DK5); caps decisions
   and telemetry-notes truncation (DK6); returns a rendered "# Decisions governing
   this work" block (DK7) + a count, or `None`. Deterministic, no LLM. Closes
   **AC 6** (selection), **AC 9** (deferral). Build contract `02`.

3. **Compose wiring: fold the push into the Tier-2 block** *(surface: proxy compose
   /injection)* — Inside the `announce_cursor` branch of `_compose_block`
   (`proxy_apply.py:202-217`), reusing the contract parsed at `:207`: when
   `signal.phase ∈ {design, build}` and the code index is enabled+available, call
   the helper with the contract + the just-composed tier-2 text, and include its
   block as an element of the `_ComposedBlock.text` join (never the cached system
   field). Run it in **its own try/except**, independent of the domain-leg compose
   (neither can suppress the other); emit the push's telemetry fields (it runs
   outside `_merge_compose_telemetry`). Graceful no-op on any gate miss (DK2).
   Closes **AC 6** (the push actually occurs). Build contract `03`.

4. **Guards + tests + docs** *(dominant surface: tests; trailing docs note)* —
   The AC 6 composition test (push occurs at design/build with a governed symbol in
   scope — a pull-only path does NOT satisfy it); the promoted-skill **deferral**
   test; the superseded-filter **wiring** test (asserts the guard is placed, not
   that it excludes anything today — DK5); a no-cached-block guard; a degrades-when-
   code-index-off/unindexed guard; and a docs note (`docs/code-index.md` /
   README) on the JIT push. Closes **AC 6**/**AC 9** verification. Build contract `04`.

**Order & dependencies.** 1 → 2 (the helper needs the join). 2 → 3 (wiring calls
the helper). 4 is verification/docs, last. **Not here:** supersession authoring
(DK5 inert), a new injection surface, any embedding in the push, any slice-1
surface change — spec Out of Scope / deferred.
</content>
