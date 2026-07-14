# Meta-Skill Corpus Delivery — Test Plan

The acceptance bar (spec AC 1) is "delivery is proven, not asserted" — so the
central test imports the **real production pack** (`_packs/sdd`) through the
**real production import path** (`install.importer.import_pack`) into a fresh
DuckDB store, then calls the **real production retrieval function**
(`retrieval.system.retrieve_system_fragments`). No fixture corpus, no mocks — this
is the actual shipped artifact exercised end to end.

## § Delivery proof (`tests/install/test_meta_skill_delivery.py`)

| Case | Asserts | AC / DK |
|------|---------|---------|
| both skills delivered on `add-skill` phase | `import_pack(ss, sdd_pack_dir)` then `retrieve_system_fragments(ss, phase="add-skill", category=None)` returns `sys-skill-authoring-rules` and `sys-skill-review-verdict` in `applied_skill_ids`, and each skill's guardrail fragment is in `candidates` | AC 1; DK1, DK2, DK3 |
| phase-scoping actually restricts, not just permits | same setup, `phase="build"` (or any non-`add-skill` phase) — **neither** skill appears in `applied_skill_ids` | AC 1; DK3 (proves this isn't accidentally `always_apply`) |
| raw_prose carried over verbatim | the delivered fragment's `content` matches the source `.md`'s post-header body byte-for-byte (diff against `_packs/meta/*.md`) | DK4 (no silent prose drift during conversion) |
| `requires` edges resolve | `import_pack` resolves `sdd-add-skill → sys-skill-authoring-rules` and `sdd-add-skill → sys-skill-review-verdict` as real `skill_dependencies` rows (no "target missing" warning) | AC 3; DK5 |
| pack manifest is internally consistent | `sdd/pack.yaml`'s two new entries' `file` fields resolve to real files; `fragment_count: 0` matches the actual (zero) `fragments:` key in each YAML | DK2, T3 convention |

## § Regression guard (existing suites, not new tests)

| Guard | How |
|-------|-----|
| The 5 pre-existing `sys/` pack skills' applicability is unaffected | `avoids: retrieval/**` in scope — no source changed there; full suite green is the proof |
| `sdd-add-skill`'s existing `exit_gates`/`prose_invariants` intact | unchanged in the T4 edit; existing sdd-pack consistency tests (if any) still pass |
| `test_pack_tier_registry_consistency.py` unaffected | `sdd` already has tier metadata; adding skills to an existing pack doesn't touch `PACK_TIERS`/`PACK_METADATA` |
| Full non-integration suite green, including `--extra code-index` | per repo convention — CI parity |

## Explicitly not tested here

- The 7 out-of-scope meta/conventions skills — not converted, nothing to test.
- `RETRIEVAL_GRAPH_EXPAND` behavior — untouched, Option B's territory.
- A live agent actually reading and following the delivered prose — the retrieval
  layer delivering the fragment is the boundary this feature owns; content
  effectiveness is out of scope (same boundary the install-pack-semantic-gate
  fidelity tests drew).
