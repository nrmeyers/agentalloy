# Proxy Surfaces — Consolidation & Phase-Drift Adherence

> Status: roadmap doc for an in-flight 3-phase program (branch
> `feat/proxy-surface-consolidation`). Captures the research behind the program so the details
> don't fade. Updated as each phase's research lands. Companion to
> [proxy-architecture.md](proxy-architecture.md).

## Why this exists

A live test of the SDD workflow exposed a drift bug: AgentAlloy injected the phase orientation, the
model **saw** it, then drifted and jumped straight to building before the spec/design contracts were
written. The orientation mechanism is *one-shot and soft* — the full block is injected **once** when
the phase changes (`announce`), then decays under recency while the model's own "let me just
scaffold this" narration dominates the tail of the context. Small models (the product's target) are
the worst case.

Researching the fix surfaced two latent debts in the proxy's surface model. Rather than bolt the fix
on and leave the debt to be rediscovered later, the work is sequenced: **retire the debt first, then
land the fix on a healthy foundation.**

## The surfaces

The proxy exposes inbound HTTP surfaces in `src/agentalloy/api/`. After this program there are
**two** live ones:

| Surface | Endpoint | Router | Signal layer | Injection point | Markers |
|---|---|---|---|---|---|
| **Passthrough** | `POST /proj/{token}/v1/messages` | `proxy_passthrough_router.py` | yes | trailing **user** message | yes (announce/cursor) |
| **OpenAI** | `POST /v1/chat/completions` | `proxy_router.py` | yes | **system** message | yes (announce/cursor — parity achieved Phase 2; both surfaces share the `apply_signal` seam) |

Claude Code wires to the passthrough (`ANTHROPIC_BASE_URL=…/proj/<token>`). Every OpenAI-format
harness (aider, codex, cline, continue, cursor via sidecar, …) wires to `/v1/chat/completions`.

A third surface — the bare tokenless `POST /v1/messages` Anthropic→OpenAI **translation shim**
(`proxy_anthropic_router.py`) — is **removed** by this program (Phase 1); see below.

---

## Phase 1 — Remove the dead surface

### Verdict

The bare `/v1/messages` translate shim is **reachable but completely unwired**, and its code is
**fully self-contained**. It is safe to delete outright.

- **Unwired:** every `ANTHROPIC_BASE_URL` the install/wiring writes includes the `/proj/<token>`
  prefix (`providers/claude_code/runtime.py`, `install/subcommands/wire_harness.py`). No harness,
  profile, or preset ever resolves to bare `/v1/messages`. Docs describe it as a *"legacy bridge …
  kept for back-compat, untouched by the passthrough path."* Present since the first public commit;
  never wired.
- **Self-contained (verified directly):** `_openai_stream_to_anthropic_interleaved` and every other
  helper are referenced **only within `proxy_anthropic_router.py` itself**. The only reference to
  that module anywhere in `src/` is the registration in `app.py`. The live passthrough imports its
  own `AnthropicPassthroughClient` and relays `aiter_raw()` byte-for-byte
  (`anthropic_passthrough.py`, `proxy_passthrough_router.py` `_forward_streaming`) — it forwards to
  an Anthropic-format upstream, needs **no** translation, and depends on **nothing** in the deleted
  modules. The OpenAI→Anthropic interleaver dies with the file. **No code relocation required.**

> Note: an earlier research pass claimed the interleaver had to be extracted to a shared module
> first, and that half of `test_proxy_anthropic.py` had to be kept. Direct `grep` disproved both —
> recorded here so the (incorrect) caution isn't reintroduced later.

### Removal scope

**Delete whole:**
- `src/agentalloy/api/proxy_anthropic_router.py`
- `src/agentalloy/api/proxy_anthropic_models.py` (imported only by the dead router + its two tests)
- `tests/test_anthropic_router.py`
- `tests/test_proxy_anthropic.py`

**Edit:**
- `src/agentalloy/app.py` — drop the `proxy_anthropic_router` import and its `include_router(...)`.
- `tests/test_streaming_error_handling.py` — remove the two bare-path Anthropic classes
  (`TestAntrhopicStreamingErrorHandling`, `TestAnthropicNonStreamingErrorHandling`) + docstring refs.
- `README.md` — drop the `POST /v1/messages` translation-shim bullet.
- `docs/proxy-architecture.md` — collapse the "two distinct Anthropic paths" section to one live
  passthrough, drop the shim row from the endpoints table, and simplify the passthrough's
  "Unlike the translation shim" docstring/prose.
- `CLAUDE.md` — the layer-3 wording ("OpenAI-compatible and Anthropic Messages endpoints") →
  name the two surfaces explicitly (passthrough + OpenAI), not three.

**Verify:** `uv run pytest` green; app boots; `/proj/<token>/v1/messages` and `/v1/chat/completions`
still served; bare `/v1/messages` now 404; `ruff`/`pyright` clean.

---

## Phase 2 — Bridge passthrough↔OpenAI parity

**Resolved & implemented.** A shared `apply_signal` seam now lives in
`api/proxy_apply.py` (alongside the lifted `_compose_block`/`_ComposedBlock`); both routers call
it to run `compose → inject → commit_markers` with identical delivery-gated cadence. The OpenAI
surface now injects into the **last user message** (system block byte-identical, prompt-cache safe)
via a new typed `inject_into_openai_messages`, replacing the old system-message `compose_and_inject`
(removed). Marker keying uses the per-request project dir, which resolves on the OpenAI path via the
`/proj/<token>` discriminator (baked by the codex/openclaw env-builders) or the cwd/metadata
fallback — so `.agentalloy/{announced,composed}` keying needed no change. The
`openai-harnesses-tokenless` assumption is obsolete: the `/proj/{token}/v1/chat/completions` route
already exists.

### The gap

The OpenAI path (`proxy_router.py`) runs `evaluate_signal` but injects into the **system** message
(`proxy_injection.py`) and **commits no markers** by design (documented in `proxy_router.py`). So it
has no once-per-phase announce, no per-turn cursor, and no place to hang the Phase-3 banner/scold.
The passthrough has all of this. Parity = both surfaces run the identical
`evaluate_signal → compose → inject → commit_markers` cycle through **one shared helper**.

### Open research (resolve when we reach this phase)

- **Injection position** on the OpenAI shape — system message vs an appended trailing user message
  (recency vs prompt-cache trade-off; OpenAI has a first-class `system`/`developer` role).
- **Marker/announce state keying without the repo token** — OpenAI harnesses wire tokenless; resolve
  repo/cwd via the existing cwd/metadata chain and decide where the announce/cursor state lives.
- **Session-id parity** — `proxy_session.extract_session_header` for the carrier gate
  (`is_carrier = bool(request.tools)`) on the OpenAI path.
- **Refactor target** — lift the passthrough's inject + `commit_markers` flow into a shared helper
  both routers call, so Phase 3 has one injection seam, not two.

---

## Phase 3 — Phase-drift adherence (both surfaces)

Detection + correction, proxy-only, harness-agnostic. **No prevention** — the passthrough relays raw
bytes, so the proxy cannot intercept a write before it executes or synthesize a `tool_result`. The
bad write/advance has already landed; the proxy corrects on the **next** carrier turn and relies on
the model to obey (revert). The layers stack to raise adherence without a hard stop. Built on the
Phase-2 shared seam so they run on passthrough **and** OpenAI.

> **Scope decision (this PR): passive-first.** Only the cheap, zero-false-positive layers ship now —
> forceful MUST/MUST NOT corpus language, the predecessor self-check (Feature 5), the per-turn banner
> (Feature 1), and within-phase progress (Feature 4b). These directly attack the observed bug (a
> model that drifts despite seeing orientation) with no per-turn tool-use parsing and no false-positive
> surface. The **reactive** layers below — scold-after-stray (Feature 2), artifact-as-key
> (Feature 3), drift-intent escalation (Feature 4a), the `recent_tool_use` plumbing, and the
> `gate_strictness` config — are **DEFERRED to a tracked follow-up**, to be added only if real-world
> testing shows the passive layers are insufficient. The full design is retained below as the spec
> for that follow-up.

### The five layers

1. **Per-turn banner.** Compact 1-line phase reminder
   (`[PHASE design · produce docs/design/<slug>/*.md · src edits out of scope until build]`) injected
   into the trailing user message on **every** carrier turn — the recency anchor, distinct from the
   once-per-change full orientation. New `kind="banner"` marker family, strip-and-replace each turn
   (idempotent + always fresh). Derive the primary artifact from `_extract_gate_paths(exit_gates)`;
   optional pack `banner:` override.

2. **Scold-after-stray.** Parse the **previous** assistant turn's `tool_use` blocks out of
   `messages`; if a `Write`/`Edit`/`NotebookEdit` targeted a phase-forbidden path, inject a strong
   correction next turn. Fingerprint = `sha1(phase + sorted stray paths)` in `.agentalloy/scolded`
   (once-per-stray; re-fires on a new path; **clears on revert** since strays are recomputed each
   turn). Only the last assistant turn is scanned, so old writes aren't re-litigated.

3. **User-override via artifact-as-key.** The unlock decision keys off whether the exit **artifact**
   exists (heading-matched via `_section_present`), not the phase label. If the prior assistant turn
   ran `agentalloy phase set <forward-phase>` as a `Bash` tool_use while the current phase's exit
   artifact is absent → scold ("advanced without the design contract; revert or ask the user"). A
   user running `phase set` from their **own terminal** never appears in proxy traffic, so it is
   silently honored — **that asymmetry is the user-only boundary** (no origin header needed).

4. **Intent-drift + within-phase progress.**
   - *Drift:* new `implementation_drift` reference-phrase set scored via `_topic_similarity`
     (the topic path, threshold 0.56 — **not** added to the transition-intent registry, which would
     trip the startup validator) against the prior assistant narration in pre-build phases
     (`intake`/`spec`/`design`; **excludes `sdd-fast`**, which builds). On fire → `banner_strong`
     escalates the banner. Embed-down → UNKNOWN, no false escalation.
   - *Progress:* `section_completeness(path_glob, required_sections, project_root)` reusing
     `_section_present` to surface "design doc: 2/3 sections (missing: Risks)" in the banner —
     channels eagerness into completing the artifact.

5. **Predecessor-contract self-check (corpus).** Pure prompt reinforcement, no runtime code. Prepend
   each phase's `raw_prose` first line with a self-verify directive naming the predecessor artifact
   ("Confirm `docs/design/<slug>/{approach,tasks,test-plan}.md` exist before writing any code — if
   not, you skipped design; stop and go back"). The proactive front line pairing with #2's reactive
   scold.

### Cross-cutting plumbing

- **Populate `PredicateContext.recent_tool_use`** from the last assistant turn's tool_use — the field
  exists (`predicates.py`) but the proxy passes `None` today. This un-deadens the existing
  `tool_use_*` predicates too. Underpins #2 and #3.
- **`SignalResult`** gains `banner`, `banner_strong`, `scold`, `scold_fingerprint`, `pending_scold`
  (mirroring the `pending_announce` decide-now/commit-after-delivery pattern); **`commit_markers`**
  gains `scold_emitted` + the `.agentalloy/scolded` state.
- **`gate_strictness`** config (`enforce|warn|off`, hand-parsed like `_read_lifecycle_mode` — never
  `yaml.safe_load`, which coerces `off`→bool). Tone only; no layer prevents.
- **`_DEFAULT_WRITE_GATES`** per phase (intake/spec/design forbid `src/**` + `tests/**`; build/qa/ship
  /sdd-fast unlocked; `docs/**` + `.agentalloy/**` always allowed) + optional pack
  `write_gate:`/`banner:` overrides.
- **Marker composition:** three independent families in the trailing user message (workflow / banner
  / scold), each strip-replaced only within its own family; none touch the top-level `system` field
  (prompt-cache invariant preserved).
- **Pack changes require a `pack.yaml` version bump + re-ingest** (`test_pack_version_bump_guard.py`);
  loaders fall back to `_DEFAULT_WRITE_GATES` + default banner so the features work pre-bump.

### Reused helpers (do not reinvent)

`_topic_similarity` / `_cosine` (`signals/classifier.py`); `_section_present` /
`eval_artifact_contains` (`signals/predicates.py`); `_extract_gate_paths` (`signals/prefilter.py`,
add a sibling `_extract_gate_sections`); `decide_transition` / `exit_gates_for_phase`
(`signals/gates.py`, `signals/skill_loader.py`); `inject_into_anthropic_messages` +
marker helpers (`api/proxy_injection.py`).

---

## Roadmap / status

| Phase | Scope | Status |
|---|---|---|
| 0 | This doc | done |
| 1 | Remove dead bare `/v1/messages` surface | done |
| 2 | Passthrough↔OpenAI injection/marker parity | done |
| 3 | Phase-drift 5-feature design on both surfaces | pending |

**Delivery:** one branch, commits sequenced 1 → 2 → 3, a single PR carrying all three + this doc,
squash-merged + tagged `vX.Y.Z` + container build + GitHub release.

**Test strategy:** hermetic e2e via the `tests/test_proxy_passthrough_native.py` harness
(`create_app(use_default_lifespan=False)` + `httpx.MockTransport` + `TestClient`, stub embed client
on `app.state.embed_client`), mirrored against the OpenAI path once Phase 2 lands. Whole-suite green
incl. `test_pack_version_bump_guard.py` (after the bump) and `test_tc11_sse_relay_byte_for_byte`
(injection stays inbound-only).
