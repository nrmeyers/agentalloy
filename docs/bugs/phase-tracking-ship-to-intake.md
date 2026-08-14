# Bug: Phase tracking does not update from ship to intake on user command

**Filed:** 2026-02-XX  
**Severity:** High (blocks workflow progression)  
**Phase:** ship → intake transition  
**Component:** State panel / phase tracking system

## Description

When the user explicitly commands "reset to intake" from the terminal `ship` phase, the state panel does not update to reflect the phase change. The panel continues to display `ship` instead of advancing to `intake`.

Per the workflow instructions:

> **Ship is terminal.** The one transition out is back to intake, and it's the user's call. When the user says "go" — that's the go.

The user's verbal confirmation ("Reset to intake now") should trigger the phase transition, but the state panel remains unchanged.

## Impact

- **Blocks workflow progression:** The agent cannot proceed with new work items because it believes it's still in the terminal `ship` phase
- **Requires manual intervention:** User must find a workaround to reset the phase
- **Confusing UX:** The agent reports being "stuck" and asks the user to manually reset, which contradicts the expectation that the workflow is automated

## Steps to Reproduce

1. Complete a work item through the full workflow (intake → design → build → qa → ship)
2. Reach `ship` phase (terminal)
3. User says "Reset to intake now" or "go" or similar explicit command
4. Observe state panel

**Expected:** State panel updates to show `<phase>intake</phase>`

**Actual:** State panel remains at `<phase>ship</phase>`

## Root Cause (Hypothesis)

The state panel is injected into the agent's context as a system-generated XML block. The agent can read the phase but has no mechanism to update it. The phase tracking appears to be:

1. **Read-only for the agent:** The state panel is injected by the system, not written by the agent
2. **Not automatically updated:** There's no trigger that detects the user's "go" command and updates the phase
3. **Possibly manual:** The user may need to manually reset the phase in their UI, but this is not documented

## Workaround

User must manually reset the phase in their UI (if such a mechanism exists) or proceed with implementation despite the panel showing the wrong phase.

## Suggested Fix

1. **Automatic phase reset:** Detect when the user says "reset to intake" / "go" / similar from ship phase, and automatically update the state panel
2. **Agent-writable phase:** Provide the agent with a tool to update the phase (e.g., `update_phase(phase: str)`)
3. **Document the manual reset:** If the phase must be manually reset by the user, document this in the workflow instructions

## Related

- Workflow instructions state: "Ship is terminal. The one transition out is back to intake, and it's the user's call."
- State panel is injected as `<state_snapshot><phase>ship</phase>...</state_snapshot>`
- No state file found in `.agentalloy/` directory that the agent could update
