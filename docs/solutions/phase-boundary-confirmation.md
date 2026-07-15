# Phase-Boundary Confirmation

## Problem

Two phase boundaries relied on skip-able workflow prose for a human handoff, and
both failed in practice:

1. **Ship completion.** `sdd-deliver-and-ship.yaml` §6 said "stay in `ship`…
   never reset on your own initiative; wait for their go-ahead." A small model
   satisfies "stay in ship" and then goes *idle* — it never asks the user whether
   to reset to intake. The motivating incident: a session sat silent after
   shipping until the user prompted it.
2. **New-session resume.** The per-repo `.agentalloy/phase` file is contended by
   concurrent sessions, so a fresh session routinely resumes on a stale
   mid-`build`/`qa` phase and silently adopts it — no confirmation that the phase
   is even correct.

Prose is reinforcement; it is never the guarantee.

## Approach that worked

Compute both prompts as **deterministic signal-layer directives** and inject them
through the seam orientation already rides — not new prose, not a new marker
family.

The keystone was discovering the injection seam **already existed**: `SignalResult`
carries `advisories: list[str]` that `proxy_apply._compose_block` injects as an
`[agentalloy-eval]` block through the shared `apply_signal` seam — the same seam
all three proxy surfaces (`/v1/chat/completions`, `/proj/{token}/v1/messages`,
`/proj/{token}/v1/responses`) run. Adding a parallel `confirm_directives` field
that injects right beside it, under a **distinct `[agentalloy-confirm]` label**,
gave surface parity for free with clean telemetry separation.

- **T1 ship ask** — fires on `phase == "ship"` AND `docs/ship/*.md` exists (the
  same delivery-record artifact the ship exit-gate checks). Persist-until-reset:
  re-emits every ship turn, since ship never self-advances and the user may not
  act immediately.
- **T2 new-session confirm** — fires when the resolved session key is **absent**
  from the `(phase, session)` announce marker set (`not phase_changed`) and
  `phase != intake`.

## Decisions worth keeping

- **Reuse the announce marker for once-per-session, don't invent one.** `announce`
  is *always* true when `new_session` holds (it fires on "session key not yet
  oriented for this phase"), so the existing announce commit records the session
  and the confirm self-silences on the next turn. Zero new marker plumbing.
- **`not phase_changed` is load-bearing.** It distinguishes a *new session
  resuming a stale phase* (confirm) from *this same session having just advanced
  the phase itself* (a phase entry — oriented via Tier 1, not confirmed). Without
  it, every qa→ship advance would nag "confirm ship is correct."
- **Collapse the overlap into one directive.** A new session landing on `ship`
  with a delivery record satisfies both triggers. Emit a single combined message
  (confirm the phase, then ask about the reset) — never two conflicting MUST
  blocks. The helper returns one coherent list, precedence handled in-place.
- **Emit, never automate.** Both directives are pure reads. The proxy makes the
  human prompt louder/guaranteed; it never writes the phase file. The user still
  types `agentalloy phase set intake`. (This was an explicit product call: a
  green CI run or successful merge is *not* permission to reset.)

## Gotcha (cost a QA cycle)

The `raw_prose` prose-invariant guard requires load-bearing command tokens
(`agentalloy phase set intake`) to appear **verbatim and contiguous**. A YAML
literal block (`|`) preserves newlines, so wrapping the backtick-quoted token
across a line break (`` `agentalloy\n  phase set intake` ``) silently breaks
contiguity and fails `test_sdd_workflow_skills_pass_validation`. Keep such tokens
on a single source line when editing workflow-skill prose.

## Files

- `src/agentalloy/api/proxy_signal.py` — `CONFIRM_LABEL`, the `confirm_directives`
  field, `_boundary_confirm_directives()`, and the `new_session` computation.
- `src/agentalloy/api/proxy_apply.py` — the `[agentalloy-confirm]` block folded
  into the shared compose join.
- `src/agentalloy/_packs/sdd/sdd-deliver-and-ship.yaml` — §6 passive→emphatic;
  `pack.yaml` version 1.4.0 → 1.5.0.
- Tests: `tests/test_ship_completion_ask.py`, `tests/test_new_session_confirm.py`.
