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
#     "event": "UserPromptSubmit" | "PreToolUse" | "PostToolUse",
#     "prompt": "...",               # UserPromptSubmit only
#     "tool_name": "...",            # PreToolUse / PostToolUse
#     "tool_path": "...",            # PostToolUse only
#     "cwd": "..."                   # Optional working directory
#   }
#
# Exit codes:
#   0 — success (even if no block was emitted)
#   1 — fatal error (could not reach the hook endpoint)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Hook endpoint base URL — overridden by $AGENTALLOY_HOOK_URL if set.
HOOK_URL="${AGENTALLOY_HOOK_URL:-http://localhost:47950/v1/hook/user-prompt-submit}"
POST_TOOL_URL="${AGENTALLOY_HOOK_URL_POST:-http://localhost:47950/v1/hook/post-tool-use}"
PRE_TOOL_URL="${AGENTALLOY_HOOK_URL_PRE:-http://localhost:47950/v1/hook/pre-tool-use}"

# Timeout for the HTTP call (seconds).  Must be < Claude Code's 3-second
# hook timeout.  2.5s matches the SWR window so the hook script never
# outlives the cache revalidation cycle.
TIMEOUT=2.5

# ---------------------------------------------------------------------------
# Read stdin
# ---------------------------------------------------------------------------

INPUT="$(cat)"

# ---------------------------------------------------------------------------
# Dispatch by event type
# ---------------------------------------------------------------------------

EVENT="$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('event','UserPromptSubmit'))" 2>/dev/null || echo "UserPromptSubmit")"

case "$EVENT" in
    UserPromptSubmit)
        # POST to user-prompt-submit endpoint
        RESP="$(curl -sf --max-time "$TIMEOUT" \
            -H "Content-Type: application/json" \
            -d "$INPUT" \
            "$HOOK_URL" 2>/dev/null || echo "{}")"

        # Extract the composed block from the response
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

        # Emit the block to stdout (Claude Code reads this)
        if [ -n "$BLOCK" ]; then
            printf '%s\n' "$BLOCK"
        fi
        ;;

    PreToolUse)
        # POST to pre-tool-use endpoint
        RESP="$(curl -sf --max-time "$TIMEOUT" \
            -H "Content-Type: application/json" \
            -d "$INPUT" \
            "$PRE_TOOL_URL" 2>/dev/null || echo "{}")"

        # Extract system skills from the response
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
        # POST to post-tool-use endpoint
        curl -sf --max-time "$TIMEOUT" \
            -H "Content-Type: application/json" \
            -d "$INPUT" \
            "$POST_TOOL_URL" >/dev/null 2>&1 || true
        ;;

    *)
        # Unknown event — silently pass through
        ;;
esac

exit 0
