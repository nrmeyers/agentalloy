# Build 02 — Pack registration + `requires` wiring

**Implements:** tasks T3, T4 · **Satisfies:** AC 1, 3 · **DKs:** DK2, DK5
**Design:** `meta-skill-corpus-delivery.design/approach.md`
**Depends on:** build 01 (the two YAML files must exist)

## Do

1. **Edit** `src/agentalloy/_packs/sdd/pack.yaml`:
   - Add two entries to `skills:`:
     ```yaml
     - skill_id: sys-skill-authoring-rules
       file: sys-skill-authoring-rules.yaml
       fragment_count: 0
     - skill_id: sys-skill-review-verdict
       file: sys-skill-review-verdict.yaml
       fragment_count: 0
     ```
     (`fragment_count: 0` — verified against `sys/pack.yaml`'s real system-class
     entries, e.g. `sys-ci`; the manifest counts authored `fragments:` entries, of
     which system-class skills have none.)
   - **Bump `version`** — currently `1.6.0` (after slice 2 of
     install-pack-semantic-gate) → `1.7.0`.

2. **Edit** `src/agentalloy/_packs/sdd/sdd-add-skill.yaml`:
   - Add `requires: [sys-skill-authoring-rules, sys-skill-review-verdict]` to the
     header (alongside the existing `domain_tags:` etc.).
   - Do **not** edit `raw_prose` — the existing prose references to both skill_ids
     already stand; `requires:` is additive provenance metadata (DK5), not a
     replacement for the sentence.

## Verify

- `uv run ruff check` clean; both YAMLs parse.
- `yaml.safe_load` on `sdd/pack.yaml` shows both new entries with correct `file`
  paths resolving to real files on disk.

## Do NOT

- Touch `retrieval/domain.py`, `RETRIEVAL_GRAPH_EXPAND`, or any graph-expand code
  — the `requires:` edge here is provenance under Option A, not load-bearing for
  delivery (system retrieval doesn't consult it).
- Remove or reword `sdd-add-skill.yaml`'s existing prose references.
