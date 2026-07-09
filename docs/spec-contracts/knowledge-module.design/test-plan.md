# Knowledge Module — Slice 1 — Test Plan

> Runtime home: `docs/design/knowledge-module/test-plan.md` (git-ignored).
> Committed copy. Cases are behaviors (input → expected), each closing a spec
> acceptance criterion. Build turns these red first, then adds edges. Slice 1
> closes AC 1–5, 7–9; **AC 6 (injection) is slice 2 — its case is named but
> deferred, not implemented here.**

## Test Cases

- **TC1 — typed decision + links, zero DDL (AC 1).** Ingest a fixture repo with a
  decision doc whose chunk body fences a known symbol; without any `ALTER`/schema
  change, `governing_decisions(fqn)` reads back the decision (its `path::anchor`
  qn, heading, file_path) and its governed symbol. *(store + ingest unit.)*

- **TC2 — capture is composed, not re-invented (AC 2).** A guard test asserts the
  decision sources are only the allow-listed lifecycle paths
  (`docs/solutions/*.md`, `docs/design/*/approach.md`,
  `docs/spec-contracts/*.design/approach.md`); the feature writes **no** new
  authoring file and reads/writes **neither** `docs/architecture-decisions/`
  **nor** `CLAUDE.md`. *(grep/allow-list guard.)*

- **TC3 — links survive incremental re-index; edits/removes prune; siblings not
  collateral (AC 3).** Four sub-cases: (a) a **content-unchanged** re-index — and
  an unrelated **code** file's re-index — preserves the `GOVERNS` edge (proves the
  `file_path=doc` vs `delete_for_files`-by-file interaction); (b) editing a
  decision chunk to drop its reference prunes the now-stale edge (doc-granular
  re-derive); (c) removing the chunk prunes its edge; (d) **the regression the
  design was corrected for** — a doc holding removed chunk A **and** an unchanged
  decision chunk B (with its own links): after A's removal, **B's links survive**
  (are restored in the same doc-granular pass), not silently dropped. *(incremental
  -reindex unit, all four sub-cases.)*

- **TC4 — linkage is correct, not just present (AC 4).** A fixture decision whose
  body fences exactly one **known** code-shaped symbol links to **that** fqn and to
  nothing else. Negatives that must link **nothing**: (a) an **ambiguous** bare
  name (two symbols share it); (b) a non-symbol path (`pipeline.py`); (c) **a
  fenced common English word that matches exactly one symbol** (`` `run` ``,
  `` `build` ``) — the coincidental single-match false positive the code-shaped
  guard exists to reject; (d) a `path::anchor`-shaped span that resolves to a
  `MarkdownDoc` chunk — the `dst` is never another doc, never the decision's own
  chunk. *(precision unit on fixtures.)*

- **TC5 — "what decisions govern this symbol?" is answerable (AC 5).** Given a code
  `fqn` with a governing decision, `GET /code/search/structural?query=
  governing_decisions&fqn=…` returns a `DecisionView` list carrying the heading and
  snippet (not a bare `CallSiteView`); an unknown/ungoverned fqn returns `[]`; a
  missing `fqn` 400s (it's in `_FQN_QUERIES`). *(store unit + route test.)*

- **TC6 — pull verb exists (AC 7).** `agentalloy knowledge why <symbol>` prints the
  governing decisions (one `path::anchor  file:line  heading` per line) and exits
  0; ungoverned fqn prints nothing and exits 0. *(CLI test against the local
  service — distinct from AC 6's push, which is slice 2; distinct namespace from
  `code`, per DK7.)*

- **TC7 — no regression / determinism / no network (AC 8).** Decision text stays
  retrievable via `agentalloy code search` with the decision index built (no change
  to `code_index/engine/` or `_corpus/` in the diff); the decision index/query path
  makes **no** cloud/paid-LLM call — the embed base_url is localhost
  (`config.py:90`) and the live path imports no `engine.constants` provider enum.
  *(guard + no-network assertion.)*

- **TC8 — boundaries hold (AC 9).** The `GOVERNS` edges and decision rows live only
  in the code-index store; the decision path writes **no** skill row/vector to the
  corpus (no auto-install). *(structural guard over the write path.)*

- **TC9 — injection push (AC 6) — DEFERRED to slice 2.** Named for coverage
  completeness; **not implemented in slice 1.** When slice 2 lands: a composition
  test asserts a governed decision's rationale is pushed at design/build **without**
  the agent querying (a pull-only verb does NOT satisfy it), excluding `superseded`
  and deferring to a promoted skill.

**Coverage map (slice 1):** AC1→TC1, AC2→TC2, AC3→TC3, AC4→TC4, AC5→TC5, AC7→TC6,
AC8→TC7, AC9→TC8. **AC6→TC9 (slice 2, deferred).** TC3 and TC4 carry the two
correctness risks grounding surfaced: the `delete_for_files`-by-file-path edge
lifecycle incl. sibling collateral (TC3), and linkage precision vs. mere presence
incl. the coincidental single-word match (TC4). The known accepted gaps — edge
decay under code **rename**, and a DK2 tier-2 link frozen unambiguous at
doc-index time then made ambiguous by a later same-named symbol — are both the
"staleness frozen at doc-index time" family, explicitly out of slice-1 scope
(spec Out of Scope; DK6) and are **not** failing cases here.
</content>
