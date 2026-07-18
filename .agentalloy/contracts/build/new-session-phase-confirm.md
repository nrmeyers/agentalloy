---
phase: build
task_slug: new-session-phase-confirm
route: full
domain_tags: [proxy-session, signal-orientation]
scope:
  touches:
    - src/agentalloy/api/proxy_signal.py     # compute the new-session confirm directive (reuses boundary_directive from T1)
    - src/agentalloy/api/proxy_session.py    # new-session detection: session key absent from the (phase,session) marker set
    - tests/test_new_session_confirm.py       # NEW
  avoids:
    - the directive-injection plumbing        # built in T1 (ship-completion-ask); this task only computes the trigger + text
    - firing on intake                        # only non-intake phases
    - changing the phase graph / auto-reset   # emits a prompt only
  success_criteria:
    - First orientation of a NEW session (session key not in the (phase,session) markers) with phase != intake → a resume-or-reset confirm directive that names the current phase.
    - New session resuming on `intake` → NO confirm (the happy resume).
    - Fires for ANY non-intake phase (spec/design/build/qa/ship), once per session — reuses the announce/(phase,session) marker so it does not re-fire on later same-session turns (AC-4).
    - When both this and the ship-ask are eligible (new session lands on ship with a delivery record) → exactly ONE coherent directive, not two conflicting MUST blocks.
    - A tool-less/background request sharing the session id does not fire or burn the confirm (honor the existing carrier gate).
    - Emitted identically on both surfaces; phase file unchanged (AC-5, AC-6).
related_contracts: [ship-completion-ask]
created_at: 2026-07-11T22:40:00Z
---

# new-session-phase-confirm

Build T2 of `docs/design/phase-boundary-confirmation/`. When a fresh session
resumes on a non-intake phase — common given the per-repo phase file is contended
by concurrent sessions — confirm with the user that the phase is correct before
doing its work, instead of silently adopting it.

## Must
- Detect new session: the request's resolved session key is absent from the
  `(phase, session)` marker set (same signal that fires `announce` for a session
  joining an existing phase — `proxy_session` / the announce marker).
- Gate on `phase != "intake"`; set `boundary_directive` (T1's field) to the
  resume-or-reset confirm naming the phase.
- Once per session (reuse the marker) — no per-turn nagging.

## Watch
- **Precedence** with the ship-ask when both apply — define a single winner or a
  composed message; never two MUST blocks. (Likely: new-session confirm on the
  first turn, ship-ask thereafter.)
- Carrier-gate / announce-marker races (orientation-carrier-request-race): a
  background quota/title request must not trigger or burn this.
- Intake resume stays silent.
