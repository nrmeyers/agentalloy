# Install-Pack Semantic Gate — Slice 2 — Approach

> Runtime home: `docs/design/install-pack-semantic-gate-slice2/approach.md` (git-ignored).
> Committed copy. Resolves slice-2's decisions as **DK9–DK14**. Acceptance is fixed
> by the parent spec (AC 7) and not reopened. DK1–DK8 (slice 1) are frozen.

## What slice 1 left for the producer to fill

Slice 1's validator (`validate_review_verdicts`, shipped, frozen) blocks a verdict
unless **all** of these hold, per skill:

1. a `review.yaml` entry exists for the `skill_id`;
2. `target_hash` == `"sha256:" + sha256((pack_dir / entry["file"]).read_bytes())`;
3. `verdict == "approve"`;
4. `blocking_issues` is empty;
5. `checks` is a **non-empty** map;
6. **no** check has status `fail`;
7. (only if `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW=1`) `reviewer.mode == "independent"`.

**The load-bearing gap (this is the whole point of slice 2).** Predicate 5+6 accept
a checks map of **all `na`** — a single `{R1: na}` passes. DK4 deliberately made
*coverage* the producer's job, not the gate's, to avoid coupling the deterministic
backend to an evolving rule vocabulary. **So the producer skill is the only thing
standing between a lazy agent and a rubber-stamp that the backend cannot detect.**
The producer's quality bar *is* slice 2's substance — not the YAML mechanics.

## The artifact this produces (names disambiguated)

The producer emits **`review.yaml`** — the *verdict* artifact of the parent spec §1.
**Do not conflate** with the phrase "review YAML" in `sys-skill-transform-contract`,
which there means the *skill* YAML (the source→skill transform output). Different
file, different purpose. The producer skill and its prose must say "the **verdict**
artifact `review.yaml`" on first use and never call the skill YAML a "review YAML."

## Decisions

### DK9 — Producer form → **a dedicated meta skill `sys-skill-review-verdict.md`, referenced from `sdd-add-skill` step 3**
The verdict format is a mechanical contract, exactly like the sibling meta skills
(`sys-skill-transform-contract`, `sys-skill-tagging-rules`). Author it as a peer:
one authoritative place for the schema + hash procedure + coverage bar, **reusable
by both lanes** — the web add-skill workflow *and* a human hand-authoring a pack for
CLI `install-pack`. `sdd-add-skill.yaml` step 3 (currently "self-critique against
R1–R9", prose-only) is rewired to "evaluate R1–R9 **and emit** `review.yaml` per
`sys-skill-review-verdict`". Rejected — **2b, inline in `sdd-add-skill`**: the review
procedure would then be reachable only via the add-skill lane, not by a CLI-install
author, contradicting the spec's "whatever LLM they're using" including CLI. Rejected
— **extend `sdd-verify-and-review`**: that skill governs the *code* SDD verify phase,
a different lifecycle; overloading it blurs two review concepts.

### DK10 — Anti-rubber-stamp coverage bar → **per-rule verdict with a one-line justification; `na` must be justified, never silent**
The producer MUST emit a `checks` entry for **every** rule R1–R9, each `pass | na |
fail`, and MUST record a one-line justification per rule in its working notes
(mirrors `sdd-add-skill` step 3's "explicit verdict per rule, don't silently skip").
Rules for a hand-authored, non-sourced skill are legitimately `na` (e.g. R1 tiered
sourcing when nothing external is cited; R5 date-stamping with no version claim; R9
deprecation for a new skill). But `na` is a *claim the reviewer defends*, not a
default. A rule that is neither `na` nor `pass` is `fail` → and a `fail` is
incompatible with `verdict: approve` (predicate 3+6), forcing another authoring
round. This is the mechanism that resists the all-`na` bypass the backend can't see.
*(Note: the gate does not read the justifications — they live in the agent's context
and, optionally, `blocking_issues`/`source_refs`. The bar is enforced by the producer
prose + the AC-7 fidelity test's coverage assertion, not by Gate 1.5.)*

### DK11 — Rule set → **R1–R9 (nine), keyed off `sys-skill-authoring-rules`, not the spec's stale "R1–R8"**
The parent spec and slice-1 docs say "R1–R8"; the live `sys-skill-authoring-rules`
has **R1–R9** (R9 = deprecation workflow, added later). The producer keys its
`checks` off the rules doc, so it enumerates R1–R9. R9 is `na` for a
non-deprecating new skill (the common case). The producer must **not** hardcode the
count — it references "every rule currently in `sys-skill-authoring-rules`" so a
future R10 is covered by re-reading, not by editing the producer. (Slice-3 note:
the *stricter* gate mode DK4 deferred — "all currently-defined R-ids present and
non-`fail`" — would read that same id set from one source of truth; DK11 keeps the
producer forward-compatible with it.)

### DK12 — `target_hash` computation → **an exact copy-paste one-liner, run after the read-only validate step**
Hand-computing a hash is exactly what produces confusing "stale review" blocks. The
producer ships the *exact* command that emits the prefixed form predicate 2 expects,
to be run **after** the skill YAML is final and `validate-pack` has passed (validate
is read-only — slice 1's `test_zero_side_effects` proves it does not rewrite the
file, so the bytes are stable):

```bash
printf 'sha256:%s\n' "$(sha256sum <pack-dir>/<skill-file>.yaml | cut -d' ' -f1)"
```

The `<skill-file>.yaml` MUST be the file named in `pack.yaml`'s `skills[].file`
(what Gate 1 reads), not a guess. Rejected — **prose telling the agent to "compute
the sha256"** (invites a wrong/normalized hash) and **a backend CLI helper**
(`agentalloy review-scaffold`) — scope creep into the backend slice 1 just closed
and versioned. *Candidate future ergonomic, flagged for the user, NOT built here:*
`validate-pack` could print the expected per-skill hash in its Gate-1.5 dry-run
report, removing even the one-liner.

### DK13 — `reviewer` provenance → **producer writes it honestly from its own runtime; `mode: independent` only when it truly is a fresh-context / distinct-model pass**
`reviewer.model` + `reviewer.harness` are whatever agent runs the producer;
`reviewer.mode` is `self` when the same context that authored the skill also
reviewed it, `independent` when a fresh context / second model did. The producer
prose states plainly: **claiming `independent` when you authored the skill in the
same context is dishonest and defeats the gate's only independence signal.** The
backend cannot prove this (DK5) — the producer's honesty is the guard, backed by the
human `approve` step in the web lane.

### DK14 — Fixture & test strategy → **a golden `review.yaml` fixture that closes the slice-1↔slice-2 contract loop, no LLM in the test**
AC 7 ("the review workflow produces a passing verdict whose checks cover R1–R9") is
verified *without* invoking an LLM: commit a fixture skill draft + a golden
`review.yaml` authored **to the producer skill's exact prescription**, then assert
(a) slice 1's `validate_review_verdicts` accepts it (format fidelity — the producer
emits what the validator accepts), and (b) its `checks` cover **every** R1–R9 id
(coverage fidelity — resists the all-`na`/partial-map rubber-stamp). This is a
*contract* test between the two slices, not a model test. A model-in-the-loop
end-to-end is out of scope (and would be non-deterministic in CI).

## What stays untouched (boundary guards)

- **Slice 1's validator is frozen.** `pack_validation.py` and the install/validate
  wiring are not edited — if the producer and validator disagree, the *producer*
  moves (the validator shipped and is tested). A guard: the AC-7 test imports the
  real `validate_review_verdicts`, so producer drift fails CI.
- **No backend, no serving runtime, no `config.py`** — in `avoids`.
- **The flag stays off.** Slice 2 does not set `AGENTALLOY_INSTALL_REQUIRE_REVIEW`.
  Enabling it is a separate, later decision once the producer is in the shipped
  corpus and the web lane (slice 3) surfaces the verdict.
