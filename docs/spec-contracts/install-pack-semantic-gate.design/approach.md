# Install-Pack Semantic Gate — Slice 1 — Approach

> Runtime home: `docs/design/install-pack-semantic-gate/approach.md` (git-ignored).
> Committed copy. Resolves the spec's Design-surface decisions as **DK1–DK8**.
> Acceptance is fixed by the spec (AC 1–10) and not reopened here.

## Shape of the change

Three touch points, all deterministic, no LLM, no network:

1. **`pack_validation.py`** — a `ReviewVerdict` model + `validate_review_verdicts(...)`
   pure function. Given the pack's parsed skill entries, the pack dir, and the
   per-skill SHA-256 that Gate 1 already computed over each validated draft, it
   loads `review.yaml`, matches per-skill entries, and returns findings in the
   **same aggregated shape** Gate 1 uses (`PackValidationResult`-like). No I/O
   beyond reading `review.yaml`.
2. **`install_pack.py`** — insert **Gate 1.5** between Gate 1 (`validate_pack_skills`,
   ~line 677) and Gate 2 (version gate, ~line 702). Only skills that passed Gate 1
   are reviewed. Thread a new `--allow-unreviewed` flag and record the gate outcome
   in the result contract.
3. **`validate_pack.py`** — extend the existing dry-run to report Gate 1.5 status
   (present / fresh / approving) with the same zero-side-effect guarantee.

The artifact:

```yaml
# review.yaml  (one per pack dir, sibling of pack.yaml)
schema_version: 1
reviews:
  - skill_id: fastapi-streaming-responses
    target_hash: "sha256:<hex of the exact draft-YAML bytes Gate 1 validated>"
    verdict: approve            # approve | revise | reject
    blocking_issues: []         # MUST be empty when verdict == approve
    checks:                     # evidence the review ran; see DK4
      R1: pass                  # pass | na | fail
      R3: pass
      # ...
    reviewer:
      model: "claude-sonnet-5"
      harness: "claude-code"
      mode: independent         # self | independent  (see DK5/DK6)
    source_refs:                # R1 grounding; may be empty for stable APIs
      - "https://fastapi.tiangolo.com/advanced/custom-response/"
    created_at: "2026-07-13T00:00:00Z"
```

## Decisions

### DK1 — Verdict location & granularity → **one `review.yaml` per pack, per-skill `reviews:` list**
Mirrors `pack.yaml`'s `skills:` list: one artifact to ship, discover, and hash-check.
A skill is matched by `skill_id`. Rejected: per-skill sibling files (N artifacts to
ship and correlate; worse ergonomics for the review workflow and for remote packs
that must ship the verdict alongside the pack).

### DK2 — Hash binding domain → **SHA-256 over the exact UTF-8 bytes Gate 1 read, no canonicalization**
`target_hash = "sha256:" + sha256(draft_bytes).hexdigest()`, where `draft_bytes`
are the exact bytes `validate_pack_skills` loaded for that skill. **Verified against
real pack structure:** each skill is its own file — `pack.yaml` lists
`skills: [{skill_id, file, fragment_count}, ...]` and Gate 1 reads
`yaml_path = pack_dir / entry["file"]` (`pack_validation.py:86`). So `draft_bytes =
(pack_dir / entry["file"]).read_bytes()` is a well-defined per-skill byte stream on
disk that both the reviewing agent and Gate 1.5 hash identically — **no
re-serialization, no canonicalizer to trust.** (Skills are *not* inline dicts in
`pack.yaml`; had they been, DK2 would have needed the canonicalization it rejects.)
Strictest and
simplest; a reformat legitimately re-triggers review (acceptable — a whitespace
diff is cheap to re-review and canonicalization adds a second, fallible normalizer
to trust). The algorithm prefix (`sha256:`) future-proofs the field.

### DK3 — Gate ordering → **1 → 1.5 → 2 → ingest → reembed/dedup; 1.5 runs only on Gate-1 passers; fail-fast, one aggregated report**
Gate 1.5 must follow Gate 1 (the hash binds to the bytes Gate 1 validated), and
must precede the version gate and ingest (heavier / mutating — no point versioning
or embedding a skill that fails review). A skill that already failed Gate 1 is
**not** reviewed (nothing valid to bind to) but both gates' failures appear in the
same exit-1 report (AC 10). No new exit code.

### DK4 — `checks` coverage strictness → **require a non-empty `checks` map with no `fail`; do NOT hardcode which R-ids must appear (v1)**
The gate enforces: `checks` present and non-empty, and no entry has status `fail`.
It does **not** hardcode that every R1–R8 id must appear — coupling the
deterministic gate to an evolving rule vocabulary is brittle (the R-rules live in
`sys-skill-authoring-rules` and change independently). Completeness of *coverage*
is the review workflow's job (slice 2, its prompt enumerates R1–R8), not the
gate's. A stricter "all currently-defined R-ids must be present and non-`fail`"
mode — reading the required id set from a single source of truth, not a hardcoded
list — is a **slice-3 tightening**, noted so it isn't silently dropped.

### DK5 — Independence enforcement → **`mode` is provenance metadata in v1, not a hard gate (except DK6)**
The backend can only read the self-declared `reviewer.mode`; it cannot prove
independence. So `mode` is recorded (result contract) and surfaced to the human
approver (slice 3), but is not itself a pass/fail criterion — **except** as the
CLI backstop lever in DK6. Hard `mode: independent` enforcement for
`system`/`workflow` skills is a slice-3 option, not v1.

### DK6 — CLI backstop → **default: process-forcing + auditable; `mode: self` rejection available via config (OPEN — confirm at review)**
`run_approve` exists only in the web add-skill lane; the CLI path has no human
gate. Two postures:
- **(a) Reject `mode: self` on the CLI** — forces an independence *claim* for CLI
  installs. Still gameable (the field is self-declared), but demands a second-pass
  review and makes rubber-stamping explicit.
- **(b) Process-forcing + auditable *(chosen default)*** — the operator who ran
  `install-pack` is the approver; the gate guarantees a review ran, is fresh, and
  is recorded in the result contract (including `mode` and `reviewer`). No
  independence claim is required.

**Chosen: (b) as default, with (a) available via `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW=1`.**
Rationale: invoking `install-pack` on a pack is itself a deliberate operator act of
vouching; requiring `mode: independent` on the CLI buys a gameable claim at the
cost of friction, not a real guarantee. The honest framing — "the gate ensures a
review ran and is auditable; you are the approver" — is stated in the result
contract and CLI output, so nothing reads as a guarantee it isn't. **This is the
one product-guarantee decision; it is flagged for design-review sign-off before
build.**

### DK7 — Escape-hatch scope → **`--allow-unreviewed`, per-invocation, recorded, CLI-only**
Mirrors `--allow-lint-warnings` / `--allow-duplicates` (both invocation-level).
Bypasses Gate 1.5 for the whole `install-pack` run and writes
`gate_1_5: {status: "bypassed", reason: "--allow-unreviewed"}` to the result
contract — loud, never silent. Not exposed in the web add-skill lane (which always
requires a verdict + human approve). Per-skill granularity and a
`system`/`workflow` hard block are deferred to slice 3.

### DK8 — Remote-pack policy → **remote packs must ship `review.yaml`; same gate, same escape hatch**
The remote-pack path (`install_pack` name-resolved from GitHub) is gated
identically: the fetched pack must contain a `review.yaml`; if absent, it's the
same Gate 1.5 failure the operator can override with `--allow-unreviewed`.
Producing a verdict *for* a third-party pack is out of scope — the pack author
ships it. Consistent with treating the verdict as a first-class pack artifact.

## What stays untouched (boundary guards)

- **No LLM, no network in the gate.** `pack_validation.py` and the Gate 1.5 wiring
  must not import `lm_client`, reference an LM endpoint, or make an HTTP call. A
  guard test asserts this (AC 5).
- **Serving runtime, `config.py`, `web/wizard_api.py`, `_packs/**` untouched** —
  in `avoids`. Web surfacing + the review workflow are later slices.
