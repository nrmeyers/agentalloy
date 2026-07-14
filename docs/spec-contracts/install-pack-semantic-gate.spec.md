# Install-Pack Semantic Gate — Architecture Spec

**task_slug:** install-pack-semantic-gate
**route:** full
**related:** `docs/install-pack-quality-gate-spec.md` (the deterministic Gate 1
this builds on), `docs/spec-contracts/compound-engineering-bridge.md` (the
authoring workflow that can produce the verdict), `src/agentalloy/_packs/meta/sys-skill-authoring-rules.md` (the R1–R8 contract the review evaluates).

## Context

The Python author-critic pipeline under `src/agentalloy/authoring/` is **retired**
(removed on the `nrmeyers` line; not imported by the runtime, not in the CLI, not
type-checked; `pyproject.toml` points corpus authoring at a separate
`agentalloy-authoring` package). Its `run_critic` — the only *semantic* quality
judge that ever existed — is dead code whose default config still points at an
uninstalled LM Studio (`qwen3.6-27b` @ `:11434`).

What guards a skill added today, via `install-pack` or the web add-skill lane:

1. **Gate 1 — deterministic well-formedness** (`pack_validation.validate_pack_skills`
   → `ingest._validate`/`_lint`): schema, controlled vocab, required
   rationale+verification fragments, mechanical tag lint. *(The adjacent
   `install-pack-quality-gate-spec.md` hardens this layer — `--strict` lint,
   auto-reembed + cross-pack cosine dedup. Assume it as the foundation.)*
2. **Human approval** — the web add-skill lane routes through `run_approve`.
3. **Advisory R1–R8 prose** — the meta packs tell the author LLM *how* to write a
   good skill, but nothing **enforces** that it did.

The gap, stated honestly: the deterministic gates guarantee a skill is
*well-formed, in-vocab, non-duplicative, versioned*. They do **not** check whether
the advice is *correct*, the example *runs*, or the API is *hallucinated*. That
judgment used to be `run_critic`'s job and is now unowned.

**What this gate does and does not guarantee (read first).** Gate 1.5 forces the
review *workflow to have run* and produces an auditable `checks` trail bound to the
exact bytes — it does **not** independently ensure the skill is correct. A verdict
is an LLM's claim; the backend can prove the claim is *well-formed, fresh, and
approving*, never that it is *honest*. Its guarantee is process + auditability, with
a human as the correctness backstop — see §4 and the CLI-vs-web asymmetry it names.

**This spec restores the semantic judgment without restoring the failure mode that
killed it.** The retired critic failed because it put a *backend-configured local
LLM* in a critical path (json_schema self-termination, multi-second latency,
config drift). The insight: when an LLM adds a skill, a **frontier LLM is already
in the room** — the operator's coding agent (Claude Code, Codex, …). Let *that*
model produce the verdict, in its own context, at its own cost; let the backend
**enforce the verdict deterministically** as an input artifact. The runtime keeps
its defining property — no LLM in path, fully local — and gains an
independent-quality semantic check for free.

**Explicitly rejected alternative (Option B):** a backend-configured judge model
(local `llama-server` or BYOK API) called from install-pack. It is the stronger
*independence* story but reintroduces exactly what was retired — an LLM as a hard
dependency of adding a skill, with the latency/reliability/config-drift baggage —
and breaks "fully local." Not adopted. See Out of Scope.

## Architecture *(grounding — not binding on acceptance)*

### 1. The verdict artifact — `review.yaml`

A skill draft carries a machine-checkable **review verdict** produced by the
authoring agent. Grounding shape (design owns the final schema):

- `schema_version` — int.
- `target` — binds the verdict to *exactly what was reviewed*: `pack`, `skill_id`,
  and `hash` = SHA-256 of the exact draft-YAML bytes Gate 1 validated. The hash is
  the anti-staleness mechanism: edit the draft after review and the verdict no
  longer matches.
- `verdict` — `approve | revise | reject` (reuses the retired critic's vocabulary;
  maps cleanly onto routing).
- `blocking_issues` — `list[str]`; MUST be empty when `verdict == approve`.
- `checks` — structured record of which R1–R8 rules were evaluated and each
  outcome. Evidence the review *happened* rather than a bare stamp.
- `reviewer` — provenance: model id + harness, and `mode: self | independent`
  (whether it was a fresh-context / second-model pass).
- `source_refs` — for R1: the authoritative docs consulted (URLs/paths), so the
  verdict is grounded, not recalled.
- `created_at`.

### 2. Gate 1.5 — deterministic enforcement (no LLM, no network)

Runs **after** Gate 1 (schema+lint), **before** the version gate and ingest. For
each skill in the pack, purely in Python:

- a matching `review.yaml` entry exists and parses;
- `target.hash` equals the SHA-256 of the exact bytes Gate 1 read (freshness);
- `verdict == approve` and `blocking_issues` is empty;
- required `checks` are present and none is `error` (design sets the coverage bar);
- fail → the **same aggregated exit-1 report** Gate 1 emits (no new error surface).

Gate 1.5 **never calls an LLM.** It validates an artifact. That is what keeps the
backend deterministic and local.

### 3. The review workflow — the "whatever LLM they're using" half

`review.yaml` is produced *upstream, in the agent's context*, by a review
skill/workflow that: reads the draft **cold**, fetches authoritative docs per R1
(`sys-r1-tiered-sourcing` / `ctx_fetch_and_index`), evaluates R1–R8, and emits the
verdict. This is where the operator's own model does the semantic work — reusing
the compound-engineering-bridge authoring workflow rather than inventing a new one.
The install-pack backend never reaches for it; it only checks its output.

### 4. Independence & the honesty backstop *(the honest limit)*

Deterministic enforcement can verify the verdict is **well-formed, fresh, and
approving** — it **cannot** verify the judgment was **honest**. A self-reviewing
agent that hallucinated an API will often bless it; and `reviewer.mode` is a field
the agent writes, so "independent" is a *claim*, not a proof. Two mitigations,
neither perfect, both cheap:

- **Prefer independence:** the review is a fresh-context pass (ideally a distinct
  model). Gate 1.5 *may* require `mode: independent` for higher-blast-radius
  `system`/`workflow` skills while accepting `self` for `domain`.
- **Keep the human `approve` step** in the web add-skill lane as the honesty
  backstop. The semantic gate raises the floor; it does not remove the human.

**The CLI-vs-web asymmetry (must be resolved by design, not left implicit).**
`run_approve` exists **only** in the web add-skill lane. The CLI `install-pack`
path has **no human gate**. So on the CLI path, Gate 1.5 with a `self`-mode verdict
collapses to *the authoring agent rubber-stamping its own work* — it forces the
review workflow to run and leaves an audit trail, but buys **no independent
guarantee**. This is the crux design decision: either the CLI path gains its own
required-independence gate (e.g. reject `mode: self` there), or the spec commits to
"CLI installs are process-forcing + auditable only, human review deferred to the
operator running the command." Named in AC 8 and the Design surface.

### Boundaries (what keeps this gate honest about itself)

- The gate lives **only** in the `install-pack` / `validate-pack` / add-skill-lane
  ingest path. It does **not** touch `api/`, `orchestration/`, or the composition
  runtime — request serving stays LLM-free.
- It does **not** import, revive, or reference `authoring/`, `AuthoringConfig`,
  `lm_client`, or any LM Studio endpoint.

## Delivery slices

- **Slice 1 — Verdict schema + Gate 1.5 + dry-run reporting + escape hatch.**
  `review.yaml` schema, the deterministic enforcement in `install_pack.py`, parity
  reporting in `validate_pack.py`, and `--allow-unreviewed` (mirrors
  `--allow-lint-warnings`/`--allow-duplicates`, bypass recorded in the result
  contract). Backend-only, deterministic. **User-observable** (install is blocked
  without a valid verdict). Closes AC 1–6, 9–10.
- **Slice 2 — The review workflow.** The skill/workflow that produces `review.yaml`
  in the agent's context (the R1–R8 cold pass, R1-grounded). Built on the
  compound-engineering-bridge authoring workflow. Closes AC 7.
- **Slice 3 *(optional)* — Class-scoped independence + web-lane surfacing.** Require
  `mode: independent` for `system`/`workflow`; surface the verdict in the add-skill
  UI alongside the human `approve`. Closes AC 8.

## Acceptance Criteria

1. **No verdict → blocked.** `install-pack` on a pack whose skill has no matching
   `review.yaml` entry fails at Gate 1.5 (exit 1), *after* Gate 1, unless
   `--allow-unreviewed` is passed. Verifiable by a CLI/handler test.
2. **Stale verdict → blocked.** If `target.hash` ≠ the SHA-256 of the exact draft
   bytes Gate 1 validated (i.e. the draft was edited after review), Gate 1.5
   blocks. Verifiable by editing a fixture draft post-verdict.
3. **Non-approving verdict → blocked.** `verdict: revise|reject`, or `approve` with
   non-empty `blocking_issues`, blocks. Verifiable by fixture verdicts.
4. **Valid approve → ingests.** A well-formed `approve` verdict with matching hash
   and present `checks` passes Gate 1.5 and the skill ingests. Verifiable end-to-end.
5. **No LLM, no network in the gate.** The `install-pack` path makes no cloud/paid-
   LLM or judge-model call; Gate 1.5 is pure Python over the artifact. Verifiable by
   a no-network guard test and a grep guard that the path never imports `lm_client`
   / `authoring` / an LM Studio URL.
6. **Explicit escape hatch, recorded.** `--allow-unreviewed` bypasses Gate 1.5 and
   the bypass is recorded in the result contract (not silent). Off by default.
   Verifiable by a flag test asserting the contract field.
7. **A review workflow produces a passing verdict.** Running the review
   workflow against a fixture draft emits a `review.yaml` that Gate 1.5 accepts, and
   its `checks` cover R1–R8. Verifiable by a workflow test on a fixture.
8. **Human approval preserved where it exists; CLI backstop resolved.** The web
   add-skill lane still requires `run_approve` *in addition to* a valid verdict (the
   verdict is surfaced, not substituted for the human) — verifiable by a wizard-API
   test. On the CLI path, which has **no** human gate, the chosen backstop is
   enforced and tested: either `mode: self` is rejected for CLI installs (forcing an
   independence claim), or the process-forcing-only posture is asserted and the
   result contract records that no human review occurred. Verifiable by a CLI test
   matching whichever posture design selects.
9. **Dry-run parity, zero side effects.** `validate-pack` reports Gate 1.5 status
   (present/fresh/approving) with no ingest, reembed, network, or corpus mutation —
   mirroring its Gate 1 dry-run guarantee. Verifiable by a validate-pack test.
10. **Reuses Gate 1's report shape.** Gate 1.5 failures appear in the same
    aggregated exit-1 report as schema/lint failures — no new error surface or exit
    code. Verifiable by asserting report structure.

## Out of Scope

- **A backend-configured judge model** (local `llama-server` or BYOK API called
  from install-pack) — Option B. Reintroduces the retired failure mode and breaks
  "fully local / no LLM in path." Deliberately not adopted.
- **Reviving `authoring/`, `AuthoringConfig`, `run_critic`, `lm_client`, or the LM
  Studio (`:11434`, `qwen3.6-27b`) config** in any form. The gate must not import or
  reference them.
- **Guaranteeing the verdict is *honest*.** Impossible deterministically; the human
  `approve` step is the backstop, and this spec does not claim to remove it.
- **Generating the verdict *inside* install-pack.** That would require the backend
  to hold an LLM — the exact thing rejected above. The backend enforces; the agent
  authors.
- **Semantic review of the remote-pack path.** A remote pack must **ship** its own
  `review.yaml`; producing a verdict for a third party's pack is deferred (v1 either
  requires a shipped verdict or exempts the remote path — design's call).
- **Changing the deterministic Gate 1 / dedup / version gates** — those are the
  adjacent `install-pack-quality-gate-spec.md`'s scope; this gate composes with them.
- **A new authoring ritual or UI** beyond the review workflow reusing the existing
  meta packs + compound-engineering-bridge.

## Design surface (hand-off to the design phase)

Open "how" decisions design must resolve; recorded so design starts grounded, not
to constrain acceptance:

- **Verdict location & granularity.** One `review.yaml` per pack with a per-skill
  list (mirrors `pack.yaml`), vs a per-skill sibling file. *(Lean: per-pack file,
  per-skill entries — one artifact to ship and hash-check.)*
- **Hash binding domain.** Exact draft bytes vs a canonicalized/whitespace-
  insensitive YAML normalization. *(Lean: exact bytes of the file Gate 1 reads —
  simplest and strictest; a reformat re-triggers review, which is acceptable.)*
- **Gate ordering.** Confirm `1 → 1.5 → 2 (version) → ingest → reembed/dedup`. Does
  a Gate 1.5 failure short-circuit before the version gate? *(Lean: yes — fail fast,
  aggregate with Gate 1.)*
- **`checks` coverage strictness.** Require all R1–R8 keys evaluated (resists
  rubber-stamping) vs only require `verdict` + empty `blocking_issues` (looser but
  decoupled from an evolving rule vocabulary). Trade-off is rigidity vs coupling.
- **Independence enforcement — is it worth it?** The backend can only trust the
  self-declared `reviewer.mode`; it cannot *prove* independence. Decide whether to
  enforce `mode: independent` for `system`/`workflow` at all, or treat `mode` as
  pure provenance metadata surfaced to the human approver.
- **CLI backstop (the crux).** `run_approve` is web-lane only; the CLI `install-pack`
  path has no human gate, so a `self`-mode verdict there is a self-rubber-stamp.
  Pick the posture: (a) reject `mode: self` on the CLI path (require an independence
  claim), or (b) declare CLI installs process-forcing + auditable only and record in
  the result contract that no human review occurred. This determines whether Gate 1.5
  buys anything on the CLI path at all.
- **Escape-hatch scope.** Is `--allow-unreviewed` per-pack only, or also per-skill?
  Should it be blocked entirely for `system`/`workflow` class skills?
- **Remote-pack policy.** Require a shipped `review.yaml` for remote packs in v1, or
  exempt the remote path and gate only local `install_local_pack`.
