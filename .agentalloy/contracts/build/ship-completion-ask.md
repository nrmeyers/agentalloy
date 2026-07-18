---
phase: build
task_slug: ship-completion-ask
route: full
domain_tags: [sdd-ship, signal-orientation]
scope:
  touches:
    - src/agentalloy/api/proxy_signal.py               # add boundary_directive to SignalResult; compute ship-ask when phase==ship & docs/ship/<slug>.md exists
    - src/agentalloy/api/proxy_apply.py                # inject boundary_directive through the shared compose→inject→commit seam (surface parity)
    - src/agentalloy/api/proxy_injection.py            # NEW marker family for the directive (mirror banner strip-and-replace; don't reuse banner family)
    - src/agentalloy/_packs/sdd/sdd-deliver-and-ship.yaml  # §6 emphatic explicit ask + version bump
    - tests/test_ship_completion_ask.py                # NEW
  avoids:
    - the new-session confirm            # T2 (new-session-phase-confirm) reuses this plumbing
    - auto-resetting the phase           # directive emits a prompt ONLY; never writes the phase file
    - the banner marker family           # distinct family; must not collide with strip-and-replace
  success_criteria:
    - phase==ship AND docs/ship/<slug>.md present → an emphatic "MUST ask the user whether to reset to intake" directive is injected; verified by driving the orientation path, not by reading the skill.
    - Ship ENTRY with no delivery record yet → NO directive (don't prompt mid-delivery before the PR exists).
    - The directive persists across ship turns until the user resets (ship never self-advances) and never double-injects within a turn (strip-and-replace).
    - Emitted identically on both proxy surfaces via the shared apply seam (AC-6).
    - The phase file is byte-unchanged after the directive fires (no automation, AC-5).
    - §6 prose rewritten passive→emphatic ask; pack version bumped.
related_contracts: [new-session-phase-confirm]
created_at: 2026-07-11T22:40:00Z
---

# ship-completion-ask

Build T1 of `docs/design/phase-boundary-confirmation/`. The deterministic
guarantee that, once delivery has landed, the agent proactively asks the user
whether to reset to intake — rather than the passive prose that let this session
sit idle until prompted.

## Must
- `boundary_directive: str | None` on `SignalResult`; computed in `proxy_signal`.
- Trigger: `phase == "ship"` AND `docs/ship/<slug>.md` exists (delivery-complete
  signal — reuse the ship exit-gate's `docs/ship/*.md` slug source).
- Inject via `proxy_apply` (shared seam) with a NEW `proxy_injection` marker
  family; forceful MUST block; persist-until-reset; strip-and-replace per turn.
- Rewrite `sdd-deliver-and-ship.yaml` §6 (emphatic ask) + bump version.

## Watch
- Marker family must not collide with banner/workflow/system; must not burn on an
  undelivered turn (no user message / dropped content).
- Directive text still says "never reset on your own initiative" — it makes the
  ASK louder, it does not authorize the agent to reset.
- Lay the injection plumbing cleanly — T2 reuses `boundary_directive` + the family.
