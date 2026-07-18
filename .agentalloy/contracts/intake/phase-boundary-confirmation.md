---
phase: intake
task_slug: phase-boundary-confirmation
route: full
domain_tags: [sdd-workflow, signal-orientation, proxy-session, phase-banner, human-confirmation]
scope:
  touches:
    - src/agentalloy/_packs/sdd/sdd-deliver-and-ship.yaml   # §6 "Close it out" — make the ready-to-return-to-intake an EMPHATIC explicit ask, not passive "wait for go-ahead"; version bump (pack edits propagate only on bump)
    - src/agentalloy/api/proxy_session.py                    # orients once per (phase, session); the new-session-detection seam where a phase≠intake resume can trigger a confirm
    - src/agentalloy/api/proxy_injection.py                  # banner/marker injection — how a confirm directive is placed/stripped per turn
    - src/agentalloy/api/proxy_apply.py                      # Tier 1 orientation block assembly (where a deterministic confirm directive would ride)
    - src/agentalloy/signals/gates.py                        # phase graph; ship stays "ship" terminal (no self-advance) — reference, likely unchanged
    - tests/                                                 # session-start confirm + ship-ask coverage (hermetic proxy e2e)
  avoids:
    - auto-resetting ship → intake            # the user explicitly wants a HUMAN to confirm; do NOT make the reset deterministic/automatic
    - changing the interactive default posture # human-confirmed reset stays the default and is correct
    - the autonomous/headless exit problem     # separate, deferred item — no human to confirm; out of scope here
    - concurrent-session phase contention fix  # related (per-repo phase file races) but a distinct work item
  success_criteria:
    - At ship completion (delivery record written + merge healthy), the agent EXPLICITLY and emphatically ASKS the user "ready to return to intake?" — it does not merely report "shipped" and wait silently for the user to raise it.
    - When a new session starts and the repo phase is NOT intake, the agent CONFIRMS with the user that the current phase is correct before proceeding with phase work (resume vs. reset), rather than silently continuing on a possibly-stale phase.
    - The two asks are reliable enough to actually fire on small models — not just prose a tiny model skips (the drift between "prose says wait for go-ahead" and what happened this session is the motivating failure).
    - The new-session confirm fires at most once per session (no per-turn nagging); reuses the existing once-per-(phase,session) orientation marker rather than a parallel mechanism.
    - Ship stays terminal (no self-advance) and the reset remains a human action — this work makes the human prompt LOUDER and adds a resume-guard, it does not automate the transition.
related_contracts: []
created_at: 2026-07-11T22:20:00Z
---

# phase-boundary-confirmation

## What the user actually wants

Two explicit human-confirmation prompts at the SDD lifecycle's boundaries, so the
loop never closes (or resumes) on a silent assumption:

1. **Emphatic ready-to-return-to-intake ask at ship.** Today the ship skill says
   "stay in `ship`, tell the user it shipped, and wait for their go-ahead; never
   reset on your own initiative" (`sdd-deliver-and-ship.yaml` §6). That is the
   right *policy* — but it is passive: this session shipped, reported it, and then
   sat idle until the user had to ask "shouldn't we be back at intake?" The user
   wants the agent to **proactively and emphatically ASK** — "delivery's landed;
   ready to reset to intake for the next item?" — not wait to be prompted.

2. **New-session phase confirmation when phase ≠ intake.** When a fresh session
   starts and the repo is parked on some non-intake phase (left at `ship`, or
   mid-`build`, or on a phase another concurrent session set), the agent must
   **confirm with the user that the current phase is correct** before it starts
   doing that phase's work — resume, or reset. Not silently continue.

Both are the same principle: **phase transitions across a boundary are the human's
call, and the agent must surface the choice loudly, not assume it.**

## Why today's behavior misses it

- The ship-completion ask lives only in *prose* (`sdd-deliver-and-ship.yaml` §6).
  Prose guidance is probabilistic — a small model can (and here effectively did)
  satisfy the letter ("stay in ship, wait") while skipping the spirit (proactively
  asking). There is no deterministic nudge that *guarantees* the ask surfaces.
- There is **no new-session phase check at all.** `proxy_session.py` orients once
  per `(phase, session)` and `proxy_apply.py` assembles the Tier 1 orientation
  block, but neither asks "you're resuming at phase X from a prior session — is
  that intended?" A resumed session just adopts whatever phase the file holds —
  dangerous given the phase file is per-repo and contended by concurrent sessions
  (observed repeatedly this session: the phase file was cleared/changed underfoot).

## Intent signals

- intent: change-existing (strengthen ship prose) + add-new (session-start confirm).
- artifact_type: workflow-prose edit + signal/proxy-layer feature + tests.
- scope: medium — one pack skill (emphatic ask + version bump), a new session-start
  confirm on the orientation path, injection plumbing, tests. Touches the sdd pack
  + the proxy/signal orientation seam; no retrieval/composition change.
- urgency: soon — the motivating gap recurred live this session; low blast radius.

## Open questions for spec/design (not decided at intake)

1. **Prose vs. deterministic guarantee.** Is strengthening `sdd-deliver-and-ship`
   §6 enough, or does the ask need a deterministic directive on the orientation
   path (à la the phase-drift forceful banner) so a tiny model can't skip it?
   Likely both — decide the split. (See the phase-drift passive-first precedent.)
2. **How a proxy "asks."** The proxy can't block for user input — it injects a
   directive telling the agent to ask and stop. Nothing *enforces* the agent then
   waits. Define what "the agent asks" concretely means at the injection layer and
   how strong the directive is.
3. **New-session detection.** Reuse `proxy_session.py`'s once-per-(phase,session)
   marker so the confirm fires once, not per turn. What exactly counts as a "new
   session," and how does this interact with the carrier-gate / announce-marker
   races already documented (background harness requests sharing a session id)?
4. **Scope of the non-intake confirm.** Does it fire for *every* non-intake phase
   on a fresh session, or only for `ship` (the "did we forget to reset?" case)?
   Mid-`build` resume is common and legitimate — confirming every resume could be
   noise. Decide which phases warrant the guard.
5. **Interaction with concurrent-session phase contention.** A resumed session may
   see a phase a *different* live session set. The confirm should help here, but
   must not encourage clobbering another session's phase (honor-phase-protocol).
6. **Surface parity.** Both proxy surfaces (Anthropic passthrough + OpenAI
   responses) must carry the confirm identically (the shared apply_signal seam).
