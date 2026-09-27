#!/usr/bin/env bash
# Pre-PR model gate — run on the dev server before opening a PR that can move
# skill selection (interpreter, prompts, tools, skill_engine, packs, corpus).
#
# Needs the interpreter model the service uses in production (MiniCPM5-2B on
# the dev server's GPU sidecar, :50001). Starts an ISOLATED `agentalloy serve`
# from this checkout (own port, temp state, empty repo root, no embed server),
# then runs:
#   1. the v2 integration suite against it (tests/integration, with /compose)
#   2. the skill-selection eval (eval/skill_selection.py)
#   3. the baseline check (eval/check_selection_regression.py)
# and tears the service down. The live service on :48950 is never touched.
#
# Usage: scripts/pre-pr-eval.sh
# Env:   MODEL_PORT          interpreter port (default 50001)
#        AGENTALLOY_UPSTREAM_KEY  model API key; else read from
#                            ~/.config/agentalloy/serve-v2.env when present
#        EVAL_PORT           isolated service port (default 48961)
# Exit:  0 = green, 1 = a check failed, 2 = could not run (no model, port busy)
#
# Complements scripts/local-ci.sh (lint, types, unit tests — no model). The
# corpus integrity test needs no model and runs in both local-ci and CI.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

MODEL_PORT="${MODEL_PORT:-50001}"
EVAL_PORT="${EVAL_PORT:-48961}"
ENV_FILE="${HOME}/.config/agentalloy/serve-v2.env"
if [ -z "${AGENTALLOY_UPSTREAM_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  AGENTALLOY_UPSTREAM_KEY="$(grep -E '^AGENTALLOY_UPSTREAM_KEY=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
KEY="${AGENTALLOY_UPSTREAM_KEY:-sk-local}"

die() { echo "pre-pr-eval: $*" >&2; exit 2; }

curl -sf -m 5 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$MODEL_PORT/v1/models" >/dev/null \
  || die "no interpreter model on :$MODEL_PORT — run this on the dev server (or set MODEL_PORT)"
if ss -ltn 2>/dev/null | grep -q ":$EVAL_PORT "; then
  die "port $EVAL_PORT is busy (set EVAL_PORT)"
fi

uv sync --frozen -q

WORK="$(mktemp -d -t agentalloy-pre-pr-XXXXXX)"
mkdir -p "$WORK/repo" "$WORK/xdg/config" "$WORK/xdg/data"
echo "# pre-pr eval" > "$WORK/repo/README.md"
TOKEN="$(openssl rand -hex 24)"
SERVICE_PID=""

cleanup() {
  if [ -n "$SERVICE_PID" ] && kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo ">>> starting isolated agentalloy serve on :$EVAL_PORT (model :$MODEL_PORT)"
env -u AGENTALLOY_STATE_DUCK -u AGENTALLOY_INDEX_DIR \
  XDG_CONFIG_HOME="$WORK/xdg/config" XDG_DATA_HOME="$WORK/xdg/data" \
  AGENTALLOY_SERVICE_PORT="$EVAL_PORT" AGENTALLOY_MODEL_PORT="$MODEL_PORT" \
  AGENTALLOY_EMBED_PORT="$((EVAL_PORT + 8))" AGENTALLOY_UPSTREAM_KEY="$KEY" \
  AGENTALLOY_INTERP_MODEL="${AGENTALLOY_INTERP_MODEL:-minicpm5-2b}" \
  AGENTALLOY_REPO_ROOT="$WORK/repo" AGENTALLOY_INDEX_DIR="$WORK/index" \
  AGENTALLOY_STATE_DUCK="$WORK/state.duck" AGENTALLOY_USAGE_DUCK="$WORK/usage.duck" \
  TELEMETRY_DB_PATH="$WORK/telemetry.duck" AGENTALLOY_APPROVER_TOKEN="$TOKEN" \
  AGENTALLOY_MODEL_TIMEOUT="${AGENTALLOY_MODEL_TIMEOUT:-120}" \
  uv run --no-sync python -m agentalloy serve --port "$EVAL_PORT" --host 127.0.0.1 \
  > "$WORK/service.log" 2>&1 &
SERVICE_PID=$!

for _ in $(seq 1 120); do
  curl -sf "http://127.0.0.1:$EVAL_PORT/health" >/dev/null && break
  kill -0 "$SERVICE_PID" 2>/dev/null || { tail -30 "$WORK/service.log"; die "service exited during startup"; }
  sleep 1
done
curl -sf "http://127.0.0.1:$EVAL_PORT/health" >/dev/null || { tail -30 "$WORK/service.log"; die "service not healthy"; }

export AGENTALLOY_URL="http://127.0.0.1:$EVAL_PORT"
export AGENTALLOY_APPROVER_TOKEN="$TOKEN"
REPORT="eval/runs/skill-selection-pre-pr-$(date -u +%Y%m%dT%H%M%SZ).json"
FAIL=0

echo ">>> v2 integration suite"
AGENTALLOY_INTEGRATION_MODEL=1 uv run --no-sync pytest -m "integration and not container" -n0 -q tests/integration || FAIL=1

echo ">>> skill-selection eval"
set +e
uv run --no-sync python -m eval.skill_selection --out "$REPORT"
rc=$?
set -e
if [ "$rc" -eq 2 ]; then
  tail -30 "$WORK/service.log"
  die "skill-selection eval could not run (see above)"
fi
[ "$rc" -eq 0 ] || FAIL=1

echo ">>> baseline check"
uv run --no-sync python -m eval.check_selection_regression "$REPORT" || FAIL=1

echo ""
if [ "$FAIL" -ne 0 ]; then
  echo "Pre-PR eval FAILED — report: $REPORT"
  exit 1
fi
echo "Pre-PR eval green — report: $REPORT"
