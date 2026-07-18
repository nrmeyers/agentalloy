---
phase: intake
task_slug: announced-marker-commit-after-inject
route: full
domain_tags: [api-proxy, signals, orientation-cadence, fastapi, telemetry]
scope:
  touches:
    - src/agentalloy/api/proxy_signal.py            # evaluate_signal: stop writing markers at decision time; carry intent on SignalResult
    - src/agentalloy/api/proxy_passthrough_router.py # _maybe_inject + passthrough_anthropic_messages: commit markers AFTER confirmed inject
    - src/agentalloy/api/proxy_router.py            # compose_and_inject path (OpenAI/hook): same deferral — third injection site
    - src/agentalloy/api/proxy_injection.py         # inject_into_anthropic_messages / compose_and_inject: signal empty-vs-injected
    - src/agentalloy/signals/skill_loader.py        # _write_announced_atomic / _write_composed_atomic now called from the router
    - tests/test_proxy_signal.py
    - tests/test_proxy_integration.py
    - tests/test_proxy_passthrough_native.py
    - tests/test_session_orientation.py
  avoids:
    - the container install pre-flight work   # separate work item / PR (container-install-collision-preflight)
    - inject_into_anthropic_messages internals # the injection mechanics are fine; only the commit ORDERING changes
    - the _MAX_ANNOUNCED_SESSIONS LRU/concurrency model # shared-file per-session state is a known limitation, out of scope
  success_criteria:
    - The `.agentalloy/announced` marker is committed ONLY after a non-empty orientation block is confirmed injected into the outgoing request — never at signal decision time.
    - When compose degrades (embed down → "Tier 1 system compose failed") or `_maybe_inject` soft-fails to the original body, the session is NOT recorded as oriented and re-fires on the next turn.
    - The same deferral covers the Tier 2 `composed` cursor marker (`_write_composed_atomic`).
    - All three injection paths behave identically: Anthropic passthrough `_maybe_inject`, its `passthrough_anthropic_messages` handler, and the `proxy_router.compose_and_inject` path.
    - No duplicate or racing marker writes across the commit sites.
    - The now-false comment at proxy_signal.py (~337–342, "marking at decision time is safe…") is rewritten to describe deferred commit.
related_contracts: [container-install-collision-preflight]
created_at: 2026-06-24T02:00:40Z
---

# announced-marker-commit-after-inject

## What the user actually wants

Fix the latent bug that the live incident exposed: orientation prose can be
**recorded as delivered but never actually injected**, permanently burning a
session for that phase.

Root cause (verified in source): `evaluate_signal` writes `.agentalloy/announced`
(`_write_announced_atomic`) and `.agentalloy/composed` (`_write_composed_atomic`)
at **decision time** (proxy_signal.py ~lines 343–358), **before** the router
composes and injects. If `_compose_block` yields an empty block or
`passthrough_anthropic_messages` soft-fails to the original body
(proxy_passthrough_router.py ~lines 264–268), the client gets **no** orientation
— but the marker is already written. Next turn: `phase_changed=False`,
`announce=False`, Tier 1 never re-fires. Orientation is lost until the marker is
cleared by hand (which is exactly what we just did manually).

The fix: **commit the marker only after a confirmed, non-empty injection.**

## Intent signals

- intent: change-existing (correctness fix to the orientation cadence)
- artifact_type: bugfix
- scope: medium — small mechanism (defer + commit-on-success) but spans three
  injection paths and a SignalResult shape change, with test churn.
- urgency: now (caused a real, user-visible orientation miss this session)

## Open questions to resolve in spec/design (not decided at intake)

1. **Ownership invariant.** Today an in-code comment asserts "the signal layer
   owns all `.agentalloy/` state transitions." Deferring the commit to the router
   changes that. Decide the new contract: does the router own the commit, or does
   the signal layer expose a `commit()` the router calls? Candidate shapes:
   (a) add `pending_announce` / `pending_tier2` descriptors to `SignalResult` and
   have the router write them on success; (b) return a commit-callback;
   (c) pass the markers through `compose_and_inject`. Pick one that works for all
   three paths.
2. **Tier 1 vs Tier 2 reconciliation (the infinite-loop trap).** If both announce
   (Tier 1) and announce_cursor (Tier 2) fire on one turn and only Tier 2's
   compose fails — do we commit Tier 1 alone? If yes, and Tier 2 keeps failing,
   Tier 2 re-fires every turn forever; if no (all-or-nothing), a transient Tier 2
   failure re-announces Tier 1 too. Define all-or-nothing vs. per-tier commit and
   the failure semantics.
3. **"Confirmed injection" definition.** Is success = non-empty block AND payload
   mutated AND forwarded without the soft-fail except firing? Pin the exact
   predicate each path checks before committing.
4. **Three-path coordination.** `_maybe_inject` and its
   `passthrough_anthropic_messages` handler both touch the same request; ensure
   exactly one commit per request (no double write / race). Extract a shared
   commit helper if needed. Confirm the `proxy_router.compose_and_inject`
   (OpenAI/hook) path gets the same treatment.
5. **Telemetry.** Decide whether a "decided-to-announce but injection failed"
   event should be observable (mirrors the existing `dense_leg_degraded` /
   `phase_gate_embed_failed` honesty pattern) so a future silent miss is queryable
   rather than invisible.
6. **Test rewrites.** Existing tests assert markers are written *immediately* on
   `should_compose=true` (test_proxy_signal.py ~131–177, 280–305, 514–569). These
   invert under the fix → must assert deferral + commit-on-success + re-announce
   on failure, including a Tier 2 case and the `proxy_router` path (currently
   untested for this).
