# Install-Pack Semantic Gate — Slice 1 — Task Plan

> Runtime home: `docs/design/install-pack-semantic-gate/tasks.md` (git-ignored).
> Committed copy. Each task is one dominant tech surface and becomes one build
> contract (`../install-pack-semantic-gate.build/NN-*.md`, ≤ 2 `domain_tags`).
> Slice 1 closes AC 1–6, the CLI half of AC 8, and AC 9–10. **AC 7 (review
> workflow) is slice 2; AC 8's web surfacing + class-scoped independence is
> slice 3 — neither is in this plan.**

## Tasks

1. **Verdict schema + validator** *(surface: pack-validation)* — In
   `pack_validation.py`: add a `ReviewVerdict` model (DK1 shape) and a pure
   `validate_review_verdicts(skills_entries, pack_dir, draft_hashes)` that loads
   `review.yaml`, matches each Gate-1-passing skill by `skill_id`, and records a
   finding when: the entry is **missing**, `target_hash` ≠ the skill's Gate-1
   `sha256:` (DK2), `verdict != "approve"` or `blocking_issues` non-empty, or
   `checks` is empty / has a `fail` (DK4). Returns findings in the **same
   aggregated shape** as `validate_pack_skills` (AC 10). No I/O beyond reading
   `review.yaml`; no `lm_client` import (AC 5). Closes **AC 1, AC 2, AC 3**;
   contributes **AC 4, AC 10**. Build contract `01`.

2. **Gate 1.5 wiring + escape hatch + CLI backstop** *(surface: install-pack
   subcommand)* — In `install_pack.py`: compute the per-skill `sha256:` over the
   exact bytes Gate 1 validated; call Task 1's validator **after** Gate 1 and
   **before** the version gate (DK3), reviewing only Gate-1 passers; aggregate
   failures into the existing exit-1 report (AC 10). Add `--allow-unreviewed`
   (per-invocation, records `gate_1_5.status = "bypassed"`; DK7) and the
   `AGENTALLOY_INSTALL_REQUIRE_INDEPENDENT_REVIEW` posture lever (DK6, default off
   → process-forcing + auditable; records `reviewer`/`mode`). Apply the same gate
   to the remote-pack path (DK8). Result contract always carries a `gate_1_5`
   block (`passed` / `failed` / `bypassed`, plus `mode` when present). Closes
   **AC 4, AC 6**, the **CLI half of AC 8**; contributes **AC 1, AC 5, AC 10**.
   Build contract `02`.

3. **`validate-pack` dry-run reporting** *(surface: validate-pack subcommand)* —
   In `validate_pack.py`: extend the Gate-1 dry-run to also report Gate 1.5 status
   per skill (present / fresh / approving) using Task 1's validator, preserving the
   **zero side effects** guarantee (no ingest, reembed, network, or corpus
   mutation). Reuses the contract-shaped result dict. Closes **AC 9**; contributes
   **AC 5**. Build contract `03`.

## Sequencing & boundaries

- **Order:** 1 → 2 → 3. Task 1 is the pure core both wiring sites call; 2 and 3
  are independent consumers of it and can be built in either order after 1.
- **Each task is one build contract** with ≤ 2 `domain_tags` (per the tag-focus
  rule): `01` = {pack-validation, verdict-artifact}; `02` = {install-pack,
  cli-subcommand}; `03` = {install-pack, validate-pack}.
- **Not in this plan:** the `review.yaml`-producing workflow (`_packs/**`, slice 2,
  AC 7) and web-lane verdict surfacing + class-scoped independence
  (`web/wizard_api.py`, slice 3, web half of AC 8). Both are in this contract's
  `avoids`.
