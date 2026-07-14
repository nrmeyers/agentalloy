---
phase: build
task_slug: 01-verdict-schema-validator
route: full
domain_tags:
  - pack-validation
scope:
  touches:
    - "src/agentalloy/pack_validation.py"
    - "tests/**"
  avoids:
    - "src/agentalloy/api/**"
    - "src/agentalloy/orchestration/**"
    - "src/agentalloy/lm_client.py"
    - "src/agentalloy/config.py"
created_at: 2026-07-13T00:00:00Z
---

# 01-verdict-schema-validator

## Task

Add the pure core of Gate 1.5 to `pack_validation.py`: a `ReviewVerdict` model
and `validate_review_verdicts(...)`, returning the **same** `PackValidationResult`
/ `SkillValidationError` shape as `validate_pack_skills` so failures aggregate
into one report (AC 10). No LLM, no network, no import of `lm_client` (AC 5).

- `validate_review_verdicts(pack_dir, skills_entries, *, require_independent=False)
  -> PackValidationResult`:
  - Load `pack_dir / "review.yaml"`. Missing/unparseable → every skill fails with
    a clear "no review verdict" error (AC 1). Reuse `SkillValidationError`.
  - Index its `reviews:` list by `skill_id`. For each **manifest** skill entry
    (`skills_entries`), require a matching verdict entry; missing → fail (AC 1).
  - **Freshness (AC 2):** compute `sha256:` over `(pack_dir/entry["file"]).read_bytes()`
    — the same per-file bytes `content_hash` already hashes — and require it to
    equal the verdict's `target_hash`. Mismatch → fail. (Skip a skill whose file
    is absent, mirroring `validate_pack_skills`.)
  - **Verdict (AC 3):** `verdict == "approve"` and `blocking_issues` empty;
    `checks` non-empty with no entry whose status is `fail`. Otherwise fail with
    the offending detail (DK4 — do NOT hardcode which R-ids must be present).
  - **Independence lever (DK6, off here):** when `require_independent` is True,
    a `reviewer.mode != "independent"` verdict fails. Default False.
- `ReviewVerdict`: a small dataclass/parse helper mirroring the DK1 shape
  (`skill_id, target_hash, verdict, blocking_issues, checks, reviewer{model,
  harness,mode}, source_refs, created_at`); tolerant of missing optional keys.
- Keep it pure (no DB/state), like the rest of the module. `created_at` is
  recorded, never evaluated (determinism).

Closes **AC 1, AC 2, AC 3**; contributes **AC 4, AC 5, AC 10**. Tests per the
design test-plan (fixture pack + mutators; mutation-test each predicate).
