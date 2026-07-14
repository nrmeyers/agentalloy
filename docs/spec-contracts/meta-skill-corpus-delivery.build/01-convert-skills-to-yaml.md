# Build 01 — Convert both skills from bootstrap-markdown to YAML

**Implements:** tasks T1, T2 · **Satisfies:** AC 1, 3 · **DKs:** DK2, DK3, DK4, DK6
**Design:** `meta-skill-corpus-delivery.design/approach.md`

## Do

1. **Create** `src/agentalloy/_packs/sdd/sys-skill-authoring-rules.yaml`:
   - Source: `src/agentalloy/_packs/meta/sys-skill-authoring-rules.md`.
   - `skill_id: sys-skill-authoring-rules`; `canonical_name` from the `.md`'s H1
     ("Skill Authoring Rules (R1–R9)").
   - `category: tooling`, `skill_class: system`.
   - `always_apply: false`, `phase_scope: [add-skill]`, `category_scope: null`.
   - `author`, `change_summary`: carry the `.md`'s values, append one dated
     sentence: "2026-07-14: converted from `_packs/meta/sys-skill-authoring-rules.md`
     to a phase-scoped system skill (meta-skill-corpus-delivery, DK4) — content
     unchanged."
   - `raw_prose:` the `.md`'s full body (everything after the `**change_summary:**`
     line), **verbatim, byte-for-byte** — no re-authoring, no re-wrapping.
   - No `fragments:` key (DK4).

2. **Create** `src/agentalloy/_packs/sdd/sys-skill-review-verdict.yaml` — identical
   procedure, source `_packs/meta/sys-skill-review-verdict.md`, same
   `phase_scope`/`category_scope`/`always_apply`.

3. Leave both source `.md` files in `_packs/meta/` untouched — not deleted, not
   referenced by any pack.yaml. (Cleanup/dedup is a separate, later call, not
   required for delivery.)

## Verify

- `uv run ruff check` clean; both YAMLs parse (`yaml.safe_load`).
- Diff each YAML's `raw_prose` against its source `.md`'s post-header body —
  must be identical (a script run once, not a shipped test — build 03's fixture
  test covers this going forward).

## Do NOT

- Edit or improve the prose during conversion (DK6 — no changes needed; carry
  verbatim). If a genuine content bug is spotted, note it separately — don't fix
  it silently inside a delivery-mechanism change.
- Add a `fragments:` list.
- Touch the source `.md` files.
