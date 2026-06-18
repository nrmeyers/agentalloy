#!/usr/bin/env bash
# agentalloy-hook-claude-code.sh — Claude Code hook script for the AgentAlloy
# synchronous hook API.
#
# Reads a JSON event from stdin, POSTs it to the hook endpoint, and emits the
# composed block to stdout (Claude Code reads this to inject context).
#
# Usage (from Claude Code's hooks configuration):
#   /path/to/agentalloy-hook-claude-code.sh < event.json
#
# The script expects JSON on stdin with these fields:
#   {
#     "hook_event_name": "UserPromptSubmit" | "PreToolUse" | "PostToolUse",
#     "prompt": "...",               # UserPromptSubmit only
#     "tool_name": "...",            # PreToolUse / PostToolUse
#     "tool_input": {...},           # PreToolUse / PostToolUse (path extracted from it)
#     "cwd": "..."                   # Optional working directory
#   }
# (The legacy field name "event" is also accepted for the event type.)
#
# ---------------------------------------------------------------------------
# FAIL-OPEN CONTRACT (the reason the hook is the DEFAULT claude-code wiring)
# ---------------------------------------------------------------------------
# This script ALWAYS exits 0 and NEVER emits anything other than a composed
# block on success. If the hook endpoint is unreachable, times out, or returns
# a non-2xx status, the script degrades silently: exit 0, no stdout. A down or
# missing AgentAlloy service therefore leaves Claude Code behaving exactly like
# vanilla Claude — it does not break the harness. This is the asymmetry that
# makes the hook safer than proxy wiring (where a down service breaks every
# request).
#
# Exit codes:
#   0 — always (success OR silent degradation). The script never exits non-zero.
#
# Timeouts (kept well under Claude Code's per-hook budget and aligned with the
# hook_router stale-while-revalidate window so the call never outlives the
# server-side cache cycle):
#   connect: 0.2s   (200ms — fail fast when nothing is listening)
#   total:   1.0s   (1s — a slow compose run must not stall the turn)

set -uo pipefail   # NOTE: intentionally NOT -e — every failure path is fail-open.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Hook endpoint base URLs — overridden by env vars baked in at wire time.
HOOK_URL="${AGENTALLOY_HOOK_URL:-http://localhost:47950/v1/hook/user-prompt-submit}"
POST_TOOL_URL="${AGENTALLOY_HOOK_URL_POST:-http://localhost:47950/v1/hook/post-tool-use}"
PRE_TOOL_URL="${AGENTALLOY_HOOK_URL_PRE:-http://localhost:47950/v1/hook/pre-tool-use}"

# Tight fail-open timeouts. connect=0.2s fails fast when the service is down;
# total=1.0s caps the whole turn-blocking call.
CONNECT_TIMEOUT="${AGENTALLOY_HOOK_CONNECT_TIMEOUT:-0.2}"
MAX_TIME="${AGENTALLOY_HOOK_MAX_TIME:-1.0}"
# PostToolUse triggers a domain-skill compose (embed retrieval), heavier than
# the signal-eval paths — give it a longer budget, still under the 5s hook cap.
POST_MAX_TIME="${AGENTALLOY_HOOK_POST_MAX_TIME:-4.0}"

# A curl that cannot run at all (missing binary) must also fail open.
if ! command -v curl >/dev/null 2>&1; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

INPUT="$(cat)"

# ---------------------------------------------------------------------------
# Self-gate: do nothing unless THIS repo is activated (.agentalloy/phase)
# ---------------------------------------------------------------------------
# The hook is installed once, globally (a single entry in ~/.claude/settings.json),
# so it fires in EVERY Claude Code session. A repo is "activated" only when it has
# an .agentalloy/phase file — written by `agentalloy wire`. Without it, exit 0
# immediately: no python3, no curl, no POST, and the prompt never leaves Claude
# Code. This is what keeps one global hook inert-by-default and free in every
# unwired repo; the service stays authoritative for activated repos (it re-reads
# the phase from the request cwd). Pure-bash check on purpose — the whole point is
# to spend nothing here when the repo isn't wired.
_AA_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
if [ ! -f "$_AA_DIR/.agentalloy/phase" ] && [ ! -f "$PWD/.agentalloy/phase" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Dispatch by event type
# ---------------------------------------------------------------------------

# Accept both the modern "hook_event_name" field and the legacy "event" field.
EVENT="$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('hook_event_name') or d.get('event') or 'UserPromptSubmit')
except Exception:
    print('UserPromptSubmit')
" 2>/dev/null || echo "UserPromptSubmit")"

# Shared curl flags. -sf makes curl exit non-zero (and emit nothing) on a
# non-2xx status; combined with the `|| echo {}` fallback this is fail-open.
_CURL=(curl -sf
    --connect-timeout "$CONNECT_TIMEOUT"
    --max-time "$MAX_TIME"
    -H "Content-Type: application/json"
    -d "$INPUT")

case "$EVENT" in
    UserPromptSubmit)
        RESP="$("${_CURL[@]}" "$HOOK_URL" 2>/dev/null || echo "{}")"

        BLOCK="$(printf '%s' "$RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    block = d.get('composed_block', '')
    if block:
        print(block)
except Exception:
    pass
" 2>/dev/null || true)"

        if [ -n "$BLOCK" ]; then
            printf '%s\n' "$BLOCK"
        fi
        ;;

    PreToolUse)
        RESP="$("${_CURL[@]}" "$PRE_TOOL_URL" 2>/dev/null || echo "{}")"

        SKILLS="$(printf '%s' "$RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for skill in d.get('system_skills', []):
        print(skill)
except Exception:
    pass
" 2>/dev/null || true)"

        if [ -n "$SKILLS" ]; then
            printf '%s\n' "$SKILLS"
        fi
        ;;

    PostToolUse)
        # Writing a contract triggers a domain-skill compose server-side; inject
        # the result via PostToolUse additionalContext. Use a longer timeout than
        # the shared curl (compose is heavier than signal-eval).
        RESP="$(curl -sf \
            --connect-timeout "$CONNECT_TIMEOUT" \
            --max-time "$POST_MAX_TIME" \
            -H "Content-Type: application/json" \
            -d "$INPUT" \
            "$POST_TOOL_URL" 2>/dev/null || echo "{}")"

        # Plain stdout is NOT injected for PostToolUse (unlike UserPromptSubmit),
        # so emit the hookSpecificOutput.additionalContext envelope. Only when a
        # composed_block came back; json.dumps handles escaping.
        printf '%s' "$RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    block = d.get('composed_block', '')
    if block:
        print(json.dumps({
            'hookSpecificOutput': {
                'hookEventName': 'PostToolUse',
                'additionalContext': block,
            }
        }))
except Exception:
    pass
" 2>/dev/null || true
        ;;

    *)
        # Unknown event — silently pass through.
        ;;
esac

exit 0
