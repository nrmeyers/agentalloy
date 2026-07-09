# Knowledge Module — Slice 2 (JIT Injection) — Test Plan

> Runtime home: `docs/design/knowledge-module-injection/test-plan.md` (git-ignored).
> Committed copy. Cases are behaviors (input → expected), each closing **AC 6**
> (boundary **AC 9**). Build turns these red first, then adds edges.

## Test Cases

- **TC1 — the why is pushed just-in-time, on work-item entry (AC 6, the core).** A
  wired repo at **design** (or build) on the **cursor-entry turn**
  (`announce_cursor` true) whose contract `scope.touches` covers a file holding a
  symbol governed by a decision: composing the Tier-2 block yields a
  "Decisions governing this work" section carrying that decision's heading +
  snippet — **without any pull/query verb invoked**. The same setup at **spec** (or
  with empty `scope.touches`) yields **no** decision section. *(compose-level test;
  push occurs and is phase-gated.)*

- **TC1b — non-entry turns compose no decision block, by design (DK2 cadence).** On
  a design/build turn where the work-item cursor did **not** change
  (`announce_cursor` false, `current_contract` None), composition yields no
  decision section — the push is a once-per-work-item front-load, not per-turn.
  *(seam-gate test, documents the cadence honestly.)*

- **TC2 — store join returns file-scoped decisions (AC 6 data).** `decisions_for_
  files([f])` returns the decisions governing symbols in `f` and nothing for an
  ungoverned/absent file. *(store unit.)*

- **TC3 — deferral to a promoted skill that actually injected (AC 6/AC 9).** A
  decision sourced from `docs/solutions/<slug>.md` is **omitted** from the push
  **iff a `<slug>-lesson` fragment is present in this turn's composed tier-2 text**
  (Instructions covered it here). Two arms that separate presence from mere
  existence: (a) the `<slug>-lesson` fragment **in** the composed text → deferred;
  (b) the promoted skill **exists but its fragment is absent** from this turn's
  composed text (tag/rank miss) → **pushed** (no silent gap — the D1 case). An
  `approach.md`-sourced decision is never deferred. *(helper unit passing a
  synthetic composed-text string; no skill-store handle.)*

- **TC4 — superseded filter is wired but inert (AC 6, honest).** The push site runs
  every decision through `_is_superseded`; today it returns `False` for all (no
  status exists), so nothing is excluded on that basis. The test asserts the guard
  is **called/placed** (e.g. via a monkeypatched `_is_superseded → True` dropping a
  decision), not that any real decision is superseded. *(helper unit — proves the
  seam, not fake activity.)*

- **TC5 — never touches the prompt-cached system block (AC 6 constraint).** After a
  push fires, the outgoing request's top-level `system` (Anthropic) /
  `instructions` (Responses) field is byte-identical to the un-pushed request; the
  decision block lands only in the last user message. *(injection guard.)*

- **TC6 — graceful degrade (AC 6 additive).** With the code index **disabled** or
  the repo **unindexed**, composition is byte-identical to today (no decision
  section, no error, no code-index import while disabled). *(gate guard.)*

- **TC7 — budget caps + no silent truncation (DK6).** With `scope.touches`
  resolving beyond `_MAX_TOUCH_FILES` / more than `_MAX_DECISIONS` governing
  decisions, the push injects at most the caps, in deterministic order, and
  logs/telemetry-notes the truncation. *(helper unit.)*

- **TC8 — boundaries (AC 9).** The push reads decisions only from the code-index
  store (no skill-corpus write); a pushed decision is rendered as a sourced fact,
  not installed as a skill; and the deferral guard (TC3) is asserted structurally.
  *(guard.)*

**Coverage map:** AC6 → TC1, TC1b, TC2, TC4, TC5, TC6, TC7; AC9 → TC3, TC8. TC1
carries the core risk (a pull-only path must NOT satisfy AC 6 — the test invokes no
verb); TC1b pins the once-per-work-item cadence honestly; TC3 arm (b) carries the
D1 risk (defer only when Instructions actually injected, never on mere existence);
TC4 carries the honesty risk (the superseded filter must be proven *wired*, not
faked active); TC5 carries the cache-safety risk.
</content>
