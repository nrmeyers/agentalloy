# Follow-ups

Deferred, non-blocking items. Each names the trigger context so it can be picked
up later without re-deriving it.

## Corpus ingest

- **install-packs is intentionally not service-routed.** The service-mediated
  ingest work (#390) routed `lessons promote` and `install-pack` through
  `POST /corpus/ingest-pack`, but left `install-packs` on its stop→ingest→restart
  container guard, which already writes the corpus with the service up. Rationale:
  install-packs is the bulk-bootstrap path (highest-stakes — a bug breaks the whole
  corpus), often runs before the service is serving or during an upgrade cycle, and
  the only gain from routing is removing a brief restart blip. If that blip ever
  becomes an operational complaint, route it per-pack with reembed batched to the
  final pack (mirror `install_local_pack(run_reembed=...)`), no restart. _Decided
  with user, 2026-07-11._

## Retrieval quality

- **Snowflake/Iceberg domain fragments leak into unrelated build injections.**
  Observed 2026-07-11 while composing for the `ingest-secret-provisioning` build
  work-item (a secret/config task with no data-warehouse surface) — the injected
  domain fragments were snowflake/iceberg. Off-topic corpus fragments should not
  surface for a task whose tags are `[install-secret, container-bootstrap]`. Likely
  the same free-text-compose / benchmark-corpus-pollution class tracked in the
  contract-path-dormant and benchmark-fidelity threads (every phase composes
  free-text with high filler; the deterministic tag-scoped contract path is
  effectively unwired in prod). Investigate after the service-mediated-corpus-ingest
  feature ships. _Deferred by user, 2026-07-11._

## SDD tooling

- **`contract validate` rejects every build contract authored at design.**
  `validate_contract` (`src/agentalloy/contracts.py`, "Phase match check")
  unconditionally compares a contract's frontmatter `phase` against live
  `.agentalloy/phase` and flags any difference. But design's whole job is to emit
  the build contracts, so validating one at design *always* reports
  `Contract phase 'build' does not match active phase 'design'` — noise on the one
  path the command exists to serve. Observed 2026-07-26 authoring the 12
  `contract-store-and-write-gating` build contracts: all 12 returned exactly
  `Issues: 1`, that one. The check should only fire when the contract's phase is
  behind the current phase, not ahead of it. Natural home is build slice
  **07-cli-and-mirror-retirement**, which already moves `contract validate` onto
  `StateClient` — validating a store row drops the file/phase coupling anyway.
  Not fixed at design (that is a `src/` change). _Logged 2026-07-26._

- **Design exit gate is half cursor-scoped, half repo-wide.** In
  `_packs/sdd/sdd-design-and-planning.yaml`, `build_contracts_cover_tasks` and
  `build_contract_tag_focus` are cursor-scoped to the active work-item (#378), but
  the six `artifact_exists`/`artifact_contains` nodes glob `docs/design/**/`
  repo-wide. So a *previously shipped* work-item whose docs deviate from the
  heading convention blocks a *current* work-item's advance, with no indication of
  which file is at fault. Hit 2026-07-26: `contract-store-and-write-gating` could
  not leave design because `phase-boundary-confirmation` and
  `service-mediated-corpus-ingest` titled their sections `# X — Approach` (H1) instead
  of `## Approach`. Worked around by adding conforming headings to those six files;
  the real fix is to scope the artifact nodes to `docs/design/{slug}/` like the
  contract nodes. Note the proxy orientation banner reports sections correctly
  (3/3) because *it* is cursor-scoped — banner and gate disagree.
  _Logged 2026-07-26._

## build/contract-store-and-write-gating — slice 07 debt

Slice 07 (`07-cli-and-mirror-retirement`, commit `e40a179`) landed **PARTIAL**. Two
distinct gaps; the second is the one the quality gates structurally cannot see.

- **Features not delivered.** Still owed against the contract: the `agentalloy
  resume` CLI subcommand; `contracts archive` over `StateClient`; deletion of
  `mirror_to_files`; the statusline port to `StateClient` with a service-down
  badge; deletion of the contract-watching path in `src/agentalloy/watch/watcher.py`
  (`_compose_from_contract`, lines 6/10/76/87); the uninstall store-row drop across
  recorded repos.

- **Delivered features are untested, and one existing test was weakened.** Test
  count went 4598 → **4598** across slice 07 — zero net tests for a slice that
  rewrote five CLI verbs (`init|show|validate|edit|supersede`) onto `StateClient`,
  added `validate_contract_from_dict`, and added `GET /state/resume`. The only test
  file touched, `tests/test_contract_init_scaffold.py`, was *narrowed*:
  `TestInitTemplateSubstitution` was refactored to stop exercising `_init` "which now
  requires the service," i.e. an existing test was hollowed out so it would pass.
  Compare slice 06 (+25 tests, two new files) and slice 10 (+3 tests, 235 test
  lines). A slice that writes no tests passes `pytest` trivially — the four gates
  give no signal here. The 07 finishing pass must write the test-plan cases for the
  five CLI verbs, `validate_contract_from_dict`, and `/state/resume`, and restore
  `_init` coverage against a stubbed service.

- **`mirror_to_files` is dead residue, not live behavior.** Present at
  `storage/state_store.py:442` and `storage/protocols.py:464`, with **no call sites
  anywhere in `src/`** — only tests call it (`tests/storage/test_state_store.py`,
  `tests/signals/test_skill_loader.py`, `tests/api/test_state.py`). qwen restored it
  against the contract's explicit deletion order, but it contradicts the spec
  decision ("store only; the degraded fallback is a bare harness") on paper, not at
  runtime. Delete it plus its test callers in the finishing pass.

- **Slice 06's legacy-glob tolerance is the live production path, and is only
  covered by synthetic fixtures.** Slice 11 (SDD pack rewrite) has not run, so the
  shipped pack YAML still carries glob args, not `phase`/`slug`:
  `sdd-design-and-planning.yaml` uses `contracts: .agentalloy/contracts/active/build/*.md`,
  and the intake gate uses `path: ".agentalloy/contracts/active/**/*.md"`. Verified
  by hand that `_derive_phase_from_glob` resolves the phase-scoped patterns
  (`.../build/*.md` → `build`, `.../spec/<slug>.md` → `spec`, `.../qa/<slug>.md` →
  `qa`) but returns **`None`** for intake's `**/*.md` — that one carries no phase
  segment to derive. Confirm intake's `artifact_exists` still behaves before
  restarting the live service against this branch. Covered in
  `tests/signals/test_predicates_store_migration.py` only via hand-built args, never
  against the real pack. _Logged 2026-07-27._
