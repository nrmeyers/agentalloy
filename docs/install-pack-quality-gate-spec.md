# Spec: quality-gate the third-party skill ingestion path (`install-pack`)

## Problem

`agentalloy install-pack <path-or-name>` (`install/subcommands/install_pack.py`) is
today's only way to add a skill to the corpus outside the bundled `_packs/` tree. It
has two forms: a local directory containing `pack.yaml` (`install_local_pack`, no
network, no registry) and a remote pack name resolved against a hardcoded GitHub
manifest URL pattern (`_DEFAULT_MANIFEST_URL_PATTERN`, `navistone/skill-pack-{name}`,
flagged "TBD for v1" in the module docstring).

Both forms run mechanical schema validation
(`pack_validation.validate_pack_skills` → `ingest._validate`, `pack_validation.py:59`),
which already hard-enforces fragmentation shape: word-count floor/ceiling, contiguous
sequencing, heading-only-stub rejection, a required `execution` fragment, a valid
fragment-type enum, and a tag hard cap. That part works today, for both forms, with no
changes needed.

Three things are silently missing, all reachable today by running
`agentalloy install-pack ./my-pack`:

1. **Lint never blocks.** `_ingest_yaml()` (`install_pack.py:278`) shells out to
   `python -m agentalloy.ingest <yaml> --yes` and never passes `--strict`. Every
   `ingest._lint()` signal — missing `rationale`/`verification` fragment, all-`execution`
   monotony, fragment content drifting from `raw_prose`, code-fence-heavy `execution`
   fragments mislabeled, mechanical tag-rule violations — is computed and discarded.
   `--strict` exists precisely to promote these to errors (`ingest.py` argparse help:
   "Recommended for new authoring; off by default for compatibility with the legacy
   imported corpus") but nothing opts the pack-install path into it.

2. **New skills never get real vectors or a duplicate check unless someone remembers.**
   Ingest writes zero-initialized fragment embeddings (`ingest.py` module docstring);
   only `agentalloy reembed` populates real ones. `install-packs` (bulk bootstrap,
   plural — installs the bundled, in-repo corpus) calls `_bulk_reembed()` automatically
   (`install_packs.py:864`). `install_pack`/`install_local_pack` (singular — the
   third-party path) never does; nothing in `install_pack.py` calls reembed. Two
   consequences: newly ingested skills are invisible to dense retrieval until someone
   manually runs `agentalloy reembed`, and the already-shipped cross-pack near-duplicate
   detector (`dedup_gate.run_dedup_gate`, wired into `reembed/cli.py:965`, hard-blocks at
   similarity ≥ `settings.dedup_hard_threshold` = 0.92) never runs for this path either —
   a near-duplicate of an existing skill sails in undetected.

3. **The path that's actually usable today is undocumented.** `install-pack <local-dir>`
   needs no network or registry and is well tested
   (`tests/install/test_install_local_pack.py`, 555 lines). `INSTALL.md:542` only
   advertises the `<name>` remote-registry form, whose registry is the hardcoded
   placeholder above and has zero live-network test coverage
   (`tests/install/test_install_pack.py` mocks the network entirely, per its own
   docstring: "Network and subprocess paths are mocked").

Net effect: the enforced half of quality control is the least important one (mechanical
fragment shape), the skipped half is the one that actually protects the corpus (semantic
completeness + cross-pack duplication), and the entry point that works stands
undiscovered.

## Design

Three additive changes, scoped to `install-pack` only. `install-packs` (bulk bootstrap
of the bundled `_packs/` corpus) is untouched — it already reembeds automatically, and
its legacy skills aren't guaranteed `--strict`-clean, so retrofitting strict lint there
is a separate effort with its own migration cost.

### 1. Promote lint to a hard gate

- `install_pack.py:278` (`_ingest_yaml`): add `--strict` to the subprocess `cmd`
  unconditionally.
- Extend `pack_validation.validate_pack_skills` (`pack_validation.py:59`) to also call
  `ingest._lint(record, yaml_path)` per skill and fold non-empty output into
  `SkillValidationError.errors`. This makes Gate 1 (schema, pre-download-cost) reject
  with one aggregated report instead of the operator discovering lint failures one
  subprocess invocation at a time.
- Add `--allow-lint-warnings` (new CLI flag, mirrors the existing `--allow-duplicates`
  convention below) for the rare case an operator knowingly accepts a warning — e.g.
  importing a legacy-style pack. Default stays strict.

### 2. Wire `install-pack` into the existing reembed + dedup gate

- After a successful ingest loop in `install_pack()` / `install_local_pack()`, call the
  reembed pass in-process — the same thing `install_packs.py:888` already does via
  `_bulk_reembed()`. Promote that helper out of `install_packs.py` into a shared home
  (e.g. `agentalloy.reembed.run_bulk_reembed`) so both callers share one implementation
  instead of two copies.
- Surface the outcome in `install_pack`'s result dict (`dedup_exit_code`, plus the
  hard/soft match lists `DedupGateResult` already returns) and in `_render_human`
  (`install_pack.py:1159`), matching `install_packs.py:408`'s
  `"WARN: bulk reembed exited non-zero"` pattern.
- `_run` (`install_pack.py:1194`) returns non-zero when `dedup_exit_code == EXIT_DEDUP` —
  same severity tier as an ingest failure, since a hard cross-pack duplicate is a
  quality failure, not a warning. Forward `--allow-duplicates` (existing `reembed` flag)
  through the CLI for the legitimate-overlap case. Note: per `_report_dedup`'s existing
  contract, vectors are written either way — the gate reports, it doesn't roll back;
  the operator differentiates the prose or deprecates one skill via the existing
  `superseded_by` mechanism.

### 3. Document `install-pack <local-dir>` as the supported new-skill path

- `INSTALL.md:542` currently shows only `agentalloy install-pack <name>`. Add the
  local-directory form alongside it, pointing at
  `docs/skill-authoring-and-overrides-spec.md`'s `pack.yaml` / skill-YAML schema section
  for the format a hand-authored pack must follow.
- State explicitly that this path — not the `authoring/` author-critic pipeline
  (excluded from the wheel, under redesign) — is the shipped, usable way to add a
  custom skill without the redesigned tooling.

## Non-goals

- Not resurrecting or exposing `src/agentalloy/authoring/` (separate effort, tracked by
  `docs/skill-authoring-and-overrides-spec.md`).
- Not building a real signed-manifest registry for the remote `install-pack <name>` form
  — already flagged "TBD for v1" in `install_pack.py`. This spec only hardens the
  local-directory form and the shared ingest/dedup gates both forms pass through.
- Not touching `install-packs` (bundled-corpus bootstrap).

## Tests

- Audit `tests/install/test_install_local_pack.py`'s `_write_skill_yaml` fixture helper:
  it likely omits a `rationale`/`verification` fragment, which would now fail
  `--strict`. Update fixtures before flipping the default, or the suite starts failing
  on lint instead of asserting the behavior it's meant to test.
- New, in `tests/install/test_ingest_gates.py` (`TestInstallLocalPackGatesIntegration`,
  `test_ingest_gates.py:322`, the existing home for local-pack gate integration tests):
  a fixture skill missing a `rationale` fragment → Gate 1 rejects with lint output in
  `errors`, no ingest subprocess spawned.
- New: two-pack fixture where pack B's skill is a near-paraphrase of pack A's already
  installed skill (cosine ≥ `dedup_hard_threshold`) → non-zero exit, hard match in the
  JSON result; with `--allow-duplicates` → exit 0, match still reported.
- New: `--allow-lint-warnings` bypasses the lint gate but still fails on `_validate`
  hard errors.
- Regression: `tests/install/test_install_pack.py`'s mocked-network suite still passes
  with `--strict` + reembed wiring added (mock `reembed.cli.main` /
  `run_bulk_reembed` the same way it already mocks subprocess/network).

## Verify

`pytest tests/install/test_install_pack.py tests/install/test_install_local_pack.py tests/install/test_ingest_gates.py -q`
+ ruff format/check + pyright.
