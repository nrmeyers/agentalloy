# Build 03 — Delivery-proof test

**Implements:** task T5 · **Satisfies:** AC 1, 2, 3 · **Design:** `meta-skill-corpus-delivery.design/test-plan.md`
**Depends on:** builds 01, 02

## Do

Create `tests/install/test_meta_skill_delivery.py`:

1. **Setup**: a fresh temp DuckDB store (`storage.skill_store.open_skill_store` +
   `.migrate()`, matching the pattern in `tests/conftest.py::corpus_template` but
   a fresh, test-local store — do NOT reuse or mutate the shared session
   `corpus_template`/`corpus_dir` fixtures, which back many unrelated tests).
   Import the **real** `_packs/sdd` directory via
   `install.importer.import_pack(ss, Path(agentalloy.__file__).parent / "_packs" / "sdd")`,
   then `install.importer.resolve_edges(ss, stats["edges"])` to resolve the new
   `requires` edges.

2. **Delivery on `add-skill`**: call
   `retrieval.system.retrieve_system_fragments(ss, phase="add-skill", category=None)`.
   Assert `sys-skill-authoring-rules` and `sys-skill-review-verdict` are in
   `applied_skill_ids`, and their guardrail fragments (`{skill_id}-v1-f1`) are in
   `candidates`.

3. **Phase-scoping restricts**: same call with `phase="build"` (or any phase not
   `add-skill`). Assert neither skill_id appears — proves `phase_scope` is
   actually restrictive, not accidentally `always_apply`.

4. **Verbatim carryover**: for each of the two skills, read
   `_packs/meta/<skill_id>.md`, extract the body via `skill_md.parser.parse_file`
   (its `raw_prose`, which the parser `.strip()`s), and assert it equals the
   delivered fragment's `content` **after `.strip()`ing both sides** — a YAML `|`
   block scalar keeps a trailing newline the parser's `.strip()` already
   discarded, so compare stripped-vs-stripped, not raw-vs-raw (a mismatch there is
   whitespace noise, not real prose drift). This is the regression guard against
   silent *content* drift during any future edit to either the YAML or the source
   `.md`.

5. **`requires` edges resolved**: query `skill_dependencies` (or the equivalent
   read helper) for `sdd-add-skill` and assert both targets are present with
   `rel_type='requires'`.

6. **Manifest sanity**: `sdd/pack.yaml`'s two new entries' `fragment_count == 0`
   and their `file` fields exist on disk.

## Verify

- `uv run pytest tests/install/test_meta_skill_delivery.py -q` green.
- `uv run pytest -m "not integration" -q` green (full suite — regression guard for
  the 5 pre-existing `sys/` pack skills and `test_pack_tier_registry_consistency.py`).
  Run with `--extra code-index` synced for CI parity.
- `uv run pyright` 0 errors.

## Do NOT

- Reuse or mutate the shared `corpus_template`/`corpus_dir` fixtures — this test
  needs the real `sdd` pack, not the synthetic fixture corpus those back.
- Assert on retrieval behavior for the 7 out-of-scope meta skills — nothing to
  test, they weren't converted.
