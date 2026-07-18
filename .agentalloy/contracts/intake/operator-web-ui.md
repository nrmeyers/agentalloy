---
phase: intake
task_slug: operator-web-ui
route: full
domain_tags: [web-ui, fastapi, frontend, corpus, system-skills, telemetry, diagnostics, authoring]
scope:
  touches:
    # --- NEW: UI serving (mounted under a path prefix, NOT a root catch-all) ---
    - src/agentalloy/api/web_router.py          # NEW: serve built SPA via StaticFiles under /ui prefix; no root {path:path}
    - src/agentalloy/api/corpus_router.py        # NEW: read endpoints (browse skills/prose/versions) + scoped System-prose write/apply
    - src/agentalloy/app.py                       # register web + corpus routers AFTER existing routers; UI prefix-mounted, never shadowing proxy/passthrough
    - frontend/                                   # NEW: Vite + React + Tailwind SPA (dashboards + corpus browser + system-prose editor)
    # --- REUSED read surface (consumed, ideally unchanged) ---
    - src/agentalloy/api/skill_router.py          # inspect_skill + version detail already exist — corpus browser builds on these
    - src/agentalloy/api/telemetry_router.py      # GET traces/savings/coverage — dashboard consumes as-is
    - src/agentalloy/api/diagnostics_router.py    # GET runtime/corpus consistency — dashboard consumes as-is
    - src/agentalloy/api/health_router.py         # GET health/readiness — dashboard consumes as-is
    # --- Tier 2 corpus mutation (System prose only) ---
    - src/agentalloy/authoring/                    # ingest path for re-writing an edited skill version (deterministic; NO LM for prose edits)
    - src/agentalloy/reembed.py                    # re-embed the edited version into DuckDB
    - src/agentalloy/storage/vector_store.py       # lock-safe close/reopen on apply (file lock is real — see release-before-raise comments)
    # --- deploy/binding ---
    - src/agentalloy/install/container_service.py  # uvicorn host: today --host 0.0.0.0; UI must be loopback or auth-gated
  avoids:
    - config editing (the original web-ui-design.md feature)  # dropped: Settings reads process env, not .env — writes are inert; not worth building
    - adding NEW skills via the authoring rig                  # FUTURE tier: user-LLM draft -> rigor/QA gate, out-of-process job runner. Seam only, not built.
    - running the authoring LM inside the serving process      # architectural line: no LLM in the runtime path; any generation runs out-of-band
    - editing non-System skills                                # only sys-* governance prose is writable; all other skills are read-only in the UI
    - WebSocket / live-streaming telemetry                     # dashboards are poll-on-fetch (React Query); streaming is a later enhancement
  success_criteria:
    # Phase 1 — read-only operator console (Tier 1)
    - A web console is served by the existing FastAPI process under a path prefix (e.g. /ui) via StaticFiles — NOT a root {full_path:path} catch-all — and provably cannot shadow proxy or passthrough routes.
    - The console exposes read-only Telemetry (traces/savings/coverage), Diagnostics (runtime + corpus consistency), and Health/Readiness, all over endpoints that already exist (no new read APIs required beyond a corpus browser).
    - The console exposes a read-only Corpus Browser — list skills, read current prose, see version lineage and the store-vs-cache consistency report — built on skill_router.inspect_skill + version detail.
    - The UI is reachable only on loopback OR behind auth; it is never served unauthenticated on 0.0.0.0.
    - Missing/un-built frontend assets degrade gracefully (clear unavailable response or empty state). No `npx vite build` runs inside the server event loop; frontend is built in CI / Containerfile and dist is shipped (or absent -> graceful 501).
    # Phase 2 — System-prose editing (Tier 2, scoped)
    - Operators can edit the prose of System (sys-*) governance skills in the UI; non-system skills remain read-only.
    - Saving an edit creates a NEW SkillVersion (never mutates a version in place), preserving the rollback/telemetry lineage; the edit then re-ingests and re-embeds.
    - Applying an edit to the live corpus is gated on the consistency report and is safe against the live DuckDB file lock (controlled close/reopen or out-of-band apply + reload). A failed or aborted apply leaves the live corpus byte-for-byte unchanged.
    - System-prose editing is fully deterministic — no LM call, no GPU contention with the embed/rerank runtime.
  future_seam:
    - New-skill authoring (user LLM drafts -> rigor/QA gate via the authoring rig) as an out-of-process job the UI triggers and monitors. Leave room in the corpus_router + UI nav; do not implement.
related_contracts: []
created_at: 2026-06-25T01:56:50Z
---

# operator-web-ui

## What the user actually wants

A web operator console for AgentAlloy, served by the existing FastAPI process,
replacing the unfocused config-editor proposal in
`docs/web-ui-design.md` (a separate, older checkout). Two phases the user has
explicitly scoped, plus one deferred future tier:

1. **Tier 1 — read-only console.** Visual telemetry / diagnostics / health
   dashboards *and* a corpus browser: read current skill prose, look up what
   skills exist, see versions and consistency. Low risk; mostly surfaces
   endpoints that already exist (`inspect_skill`, telemetry/diagnostics/health).

2. **Tier 2 — System-prose editing only.** The governance (`sys-*`) skills are
   the prose operators need to tune **per environment**, so corpus editing is
   deliberately restricted to them. Editing existing prose is a deterministic
   hand-edit: write → bump version → ingest → re-embed → consistency-gated apply.
   No LLM, no GPU contention.

3. **Future (not now) — add new skills.** Even if the user's own LLM drafts the
   skill, it must pass a rigor/QA gate. This is the authoring rig as an
   out-of-process job. We leave a seam for it and build nothing.

The config editor from the old doc is **dropped on purpose**: `Settings`
(config.py:50) explicitly has no `env_file` and reads only the process
environment, so writing `.env` from a UI is inert until a process restart
re-sources it — the feature can't do what it claimed.

## Intent signals

- intent: build-new (greenfield operator UI + a scoped, deterministic corpus
  write path)
- artifact_type: feature (two shippable phases)
- scope: large — new FastAPI routers, a new frontend app, and a lock-safe
  corpus apply/reload protocol. Phase 1 is days; Phase 2 is its own effort.
  These should be separate PRs / spec sections, not one drop.
- urgency: normal — no incident driving it; operator-ergonomics + governance
  self-service.

## Why this scoping is the right architecture

- **No LLM in the serving process.** AgentAlloy's coherence is "deterministic
  between agent and embed model." Editing existing System prose needs no model,
  so Tier 2 stays on the right side of that line. New-skill generation (which
  *does* need a model) is deferred to an out-of-process tier.
- **System prose is the safe write target.** `sys-*` governance skills are
  hand-authored, not distilled from vendor `llms.txt`, so editing them does not
  collide with any corpus-regeneration/distillation pass (CONFIRM in spec).
- **UI as a prefix mount, not a root catch-all.** The app already ends with
  proxy + passthrough routers; a root `/{full_path:path}` would fight them and
  turn 404s into 200-HTML. A `/ui` StaticFiles mount sidesteps both.

## Open questions to resolve in spec/design (not decided at intake)

1. **System-skill source of truth.** Confirm `sys-*` skills are hand-authored
   governance (not `llms.txt`-distilled). Define how a UI prose edit becomes the
   authoritative source and survives any future regeneration pass. If they *are*
   distilled, Tier 2 needs a merge story, not a clobber.

2. **Apply + reload protocol.** How does an edited single skill go live against a
   DuckDB the running service holds under a file lock? Options: in-process
   close/reopen, a separate writer process + reload signal, or an out-of-band
   apply job the service then picks up. Define the exact reload primitive — the
   old doc's `app.state.settings` "hot reload" does NOT exist for the corpus;
   reuse the store-vs-cache consistency machinery to invalidate + verify.

3. **Incremental vs full re-embed.** Re-embed just the edited version, or run the
   full `reembed`/`install-packs` pass? Incremental is faster but must not drift
   the corpus dim/consistency guard (EMBEDDING_DIM=768).

4. **Version-bump UX + rollback.** How a prose edit maps to a SkillVersion bump,
   what the operator sees, and whether rollback is exposed in the UI (the lineage
   exists; surfacing it is a choice).

5. **UI serving + binding + packaging.** Prefix mount + StaticFiles; loopback
   bind vs token auth (reconcile with the container `--host 0.0.0.0` default);
   build frontend in CI/Containerfile and ship `dist` (or graceful 501); pnpm +
   mise per repo conventions (NOT npm/apt-get).

6. **Read API gaps.** Does the corpus browser need any new read endpoint beyond
   `inspect_skill` (e.g. list-all-skills, prose-by-version), or can it compose
   existing ones? Pin the minimal read surface.

7. **Future seam shape.** Where the new-skill authoring tier plugs in
   (corpus_router endpoint stub + UI nav slot) without being built now.
