---
phase: intake
task_slug: container-install-collision-preflight
route: full
domain_tags: [install, container-runtime, podman, port-reclaim, preflight]
scope:
  touches:
    - src/agentalloy/install/subcommands/simple_setup.py    # _run_container_flow — add native-holder pre-flight before _run_container
    - src/agentalloy/install/subcommands/container_runtime.py # _run_container / _list_conflicting_containers — collision-safe bind
    - src/agentalloy/install/subcommands/preflight.py        # run_preflight — where (which phase) the check belongs
    - src/agentalloy/install/server_proc.py                  # reuse find_listening_pid + reclaim_stale_port
    - src/agentalloy/install/runtime_artifacts.py            # reuse RUNTIME_PORTS signature + _agentalloy_container_running
    - tests/test_simple_setup_container.py
    - tests/test_container_edge_cases.py
    - tests/install/test_preflight.py
  avoids:
    - the announced-marker proxy fix    # separate work item / separate PR (announced-marker-commit-after-inject)
    - the native enable path            # enable_service._reclaim_port already covers native; do not regress it
    - killing FOREIGN processes         # only ever reclaim a holder whose cmdline matches ("uvicorn","agentalloy.app")
  success_criteria:
    - Starting a CONTAINER install while a NATIVE agentalloy uvicorn holds :47950 is detected BEFORE `podman run -p 47950:47950` — not surfaced as a cryptic "address already in use" or a silently-shadowed forwarder.
    - Detection identifies the holder by listening-PID + cmdline signature, NOT by a 127.0.0.1 socket-bind probe (which passes even when 0.0.0.0:47950 is taken).
    - A matching native holder is reconciled (reclaimed/killed) or the user is told exactly what to do; a non-matching/foreign holder is never killed.
    - An already-running agentalloy CONTAINER is distinguished from a native holder (don't reclaim our own rootlessport forwarder).
    - Non-interactive/CI runs fail fast with a clear message (or auto-reclaim under an explicit force flag) rather than prompting.
    - Existing native-enable reclaim behavior is unchanged.
related_contracts: [announced-marker-commit-after-inject]
created_at: 2026-06-24T02:00:40Z
---

# container-install-collision-preflight

## What the user actually wants

When setting up a **container** install, check for — and reconcile — two
pre-existing things before the container tries to own host `:47950`:

1. **An existing container install** (an `agentalloy` container already
   created/running).
2. **An existing native install** (a native `uvicorn agentalloy.app` bound to
   `127.0.0.1:47950`).

This is the exact failure we just hit live: a stale native uvicorn squatted
`127.0.0.1:47950`; the container's `0.0.0.0:47950` publish then failed to serve
(connection-refused on the LAN/tailscale IPs), and `localhost:47950` routed to
the degraded native process — which has no embed/reranker backend (those live
**inside** the container on `:47951/:47952`). Orientation/compose silently
degraded as a result.

## Why today's code misses it

- `_run_container_flow` (simple_setup.py) already detects+removes conflicting
  **containers** (`_list_conflicting_containers`, ~lines 1287–1324) but does
  **not** check for a native process before calling `_run_container` (~line
  1370).
- The port-reclaim that *would* fix this (`enable_service._reclaim_port`, using
  `runtime_artifacts.RUNTIME_PORTS` signature `("uvicorn","agentalloy.app")` for
  47950) runs **only** on the native-enable path — never on the container path.
- `preflight._check_port_free` probes `socket.bind(127.0.0.1, port)`, which
  **succeeds** even when `0.0.0.0:47950` is bound — so it can't see the
  collision the container will hit.
- The building blocks already exist and are unused on this path:
  `server_proc.find_listening_pid`, `server_proc.reclaim_stale_port` (foreign-safe:
  matches all cmdline substrings before killing), and
  `runtime_artifacts._agentalloy_container_running`.

## Intent signals

- intent: change-existing (harden the container install pre-flight)
- artifact_type: bugfix + hardening
- scope: medium — one new pre-flight detect/reconcile step + reuse of existing
  helpers + tests; touches the install subpackage only.
- urgency: now (reproduced live this session)

## Open questions to resolve in spec/design (not decided at intake)

1. **Where the check hooks in.** In `_run_container_flow` just before
   `_run_container` (caller-side, runs once), inside `_run_container` itself
   (makes every invocation collision-safe but risks seeing our own forwarder if
   it ever runs post-start), or as a new `run_preflight` phase (today it only has
   `early`/`runner`/`container`-less phases — `_check_port_free` is loopback-only
   and would need a holder-aware variant). Pick one; don't double-run.
2. **Detection mechanism.** Standardize on listening-PID + cmdline signature
   (reuse `RUNTIME_PORTS`/`reclaim_stale_port`), not socket-bind. Confirm whether
   `simple_setup` should import `RUNTIME_PORTS` directly or via a wrapper to
   avoid signature drift. Decide native-vs-container disambiguation: the critic
   flagged that `simple_setup` uses `container_runtime._check_container_running`,
   not `runtime_artifacts._agentalloy_container_running` — reconcile which is the
   source of truth.
3. **Reconciliation policy / fallback.** If a native holder is found and the user
   declines to kill it, what does setup do? There's a `_SWITCH_TO_NATIVE`-style
   path to consider vs. a hard fail. Define interactive vs. non-interactive/CI
   behavior and any `--force` semantics. Mirror the existing container-removal
   prompt UX.
4. **Orphaned native sidecars.** A native install also leaves native
   llama-servers on host `:47951/:47952`. Decide whether container pre-flight
   should also surface/reap those (they're harmless to the container but are
   stale) or leave them to the native uninstall path.
5. **Edge case.** `reclaim_stale_port` calls `stop(pid)` and returns `None` if the
   holder isn't a live, matchable process — define behavior for a half-dead
   port holder.
