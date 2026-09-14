#!/usr/bin/env bash
# v2-bringup.sh — bring up the AgentAlloy v2 stack from this checkout.
#
#   ./scripts/v2-bringup.sh up       # uv sync + start models + serve (default)
#   ./scripts/v2-bringup.sh smoke    # hit /health /status /chat
#   ./scripts/v2-bringup.sh status   # show service + model health
#   ./scripts/v2-bringup.sh serve    # (re)start just the service
#   ./scripts/v2-bringup.sh down     # stop everything
#
# Model servers run via podman (or docker) with GPU pinned to the 3060.
# All state lives in an isolated instance dir.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- knobs (override via env) ----------------------------------------------
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTANCE="${INSTANCE:-$HOME/.local/share/agentalloy-instance}"
MODELS_DIR="${MODELS_DIR:-/mnt/ai-data/llama/models}"
IMAGE="${IMAGE:-localhost/llama-server-cuda:latest}"

# Models. Interp: back to LFM2.5-2.6B (QAD-Q4_0) on 2026-09-14 — MiniCPM5-2B
# had replaced it on 2026-09-11 (66/66 classification / 0 hallucination vs
# LFM's 62/66 / 3%, faster compose ~9s vs ~20s); the engine is the LFM again.
# Sampler is LFM's task-tuned config (temp 0.2 / top-k 80 / repeat-penalty
# 1.05); the interpreter sends temperature 0 per request anyway. DSpark
# speculative decoding: the 633MB LFM DSpark-F16 drafter (needs the :dspark
# image, --spec-type draft-dspark; n-max 9 = the drafter's trained block
# size). Set INTERP_DRAFT_MODEL="" to disable.
INTERP_MODEL="${INTERP_MODEL:-LFM2.5-2.6B-QAD-Q4_0.gguf}"
INTERP_NAME="${INTERP_NAME:-lfm2.5-2.6b-compressor}"
INTERP_IMAGE="${INTERP_IMAGE:-localhost/llama-server-cuda:dspark}"
INTERP_DRAFT_MODEL="${INTERP_DRAFT_MODEL-LFM2.5-2.6B-DSpark-F16.gguf}"
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text-v1.5.Q8_0.gguf}"

# Ports (v2 dev range :48950-3)
PORT="${PORT:-48950}"
PROXY_PORT="${PROXY_PORT:-48953}"
MODEL_PORT="${MODEL_PORT:-50001}"
EMBED_PORT="${EMBED_PORT:-48951}"

# MAIN session model (proxy upstream): vLLM-served; LFM stays the orchestrator on MODEL_PORT
MAIN_UPSTREAM="${MAIN_UPSTREAM:-http://100.81.56.59:8000}"
MAIN_MODEL="${MAIN_MODEL:-Qwen3.8-27B-FP8}"

RUNTIME="${RUNTIME:-podman}"

# Pin to 3060 (first non-3090 GPU)
GPU_UUID="${GPU_UUID:-$(nvidia-smi --query-gpu=name,uuid --format=csv,noheader 2>/dev/null | awk -F', ' '!/3090/{print $2; exit}')}"
# ---------------------------------------------------------------------------

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

wait_health(){ # $1=url $2=secs
  for _ in $(seq 1 "${2:-60}"); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo 000)" = 200 ] && return 0
    sleep 1
  done
  return 1
}

service_pid(){ ss -ltnp 2>/dev/null | awk -v p=":$PORT\$" '$4 ~ p' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1; }

start_llama(){ # $1=name $2=image $3=port $4...=extra args
  local name="$1" image="$2" port="$3"; shift 3
  if [ "$("$RUNTIME" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ]; then
    log "$name already running on :$port"; return 0
  fi
  "$RUNTIME" rm -f "$name" >/dev/null 2>&1 || true
  log "starting $name on :$port (gpu=${GPU_UUID:-all})"
  "$RUNTIME" run -d --name "$name" \
    --device nvidia.com/gpu=all -e NVIDIA_VISIBLE_DEVICES=all \
    ${GPU_UUID:+-e CUDA_VISIBLE_DEVICES=$GPU_UUID} \
    -v "$MODELS_DIR:/models:ro" -p "$port:$port" \
    "$image" --n-gpu-layers 99 --host 0.0.0.0 --port "$port" "$@" >/dev/null
  wait_health "http://localhost:$port/health" 60 || { "$RUNTIME" logs --tail 20 "$name" 2>&1; die "$name failed to become healthy"; }
  log "$name healthy"
}

write_env(){
  mkdir -p "$INSTANCE"
  cat > "$INSTANCE/env.sh" <<EOF
# AgentAlloy v2 dev instance — source before running
export AGENTALLOY_SERVICE_PORT=$PORT
export AGENTALLOY_PROXY_PORT=$PROXY_PORT
export AGENTALLOY_MODEL_PORT=$MODEL_PORT
export AGENTALLOY_EMBED_PORT=$EMBED_PORT
export AGENTALLOY_UPSTREAM_URL=$MAIN_UPSTREAM
export AGENTALLOY_MODEL=$MAIN_MODEL
export AGENTALLOY_INTERP_MODEL=$INTERP_NAME
export AGENTALLOY_UPSTREAM_KEY=${AGENTALLOY_UPSTREAM_KEY:-sk-local}
export AGENTALLOY_REPO_ROOT=$REPO_ROOT
export AGENTALLOY_STATE_DUCK=$INSTANCE/state.duck
export AGENTALLOY_USAGE_DUCK=$INSTANCE/usage.duck
export AGENTALLOY_INDEX_DIR=$INSTANCE/index
# Telemetry DB too — otherwise the dev instance and any other running
# agentalloy service share ~/.local/share/agentalloy/telemetry.duck and
# the second one dies on the DuckDB writer lock.
export TELEMETRY_DB_PATH=$INSTANCE/telemetry.duck
EOF
}

cmd_up(){
  command -v uv >/dev/null || die "uv not on PATH"
  command -v "$RUNTIME" >/dev/null || die "$RUNTIME not on PATH"
  [ -d "$MODELS_DIR" ] || die "MODELS_DIR not found: $MODELS_DIR"
  cd "$REPO_ROOT"

  log "uv sync"
  uv sync

  # Model servers on 3060
  local draft_args=()
  if [ -n "$INTERP_DRAFT_MODEL" ]; then
    draft_args=(--spec-type draft-dspark --spec-draft-model "/models/$INTERP_DRAFT_MODEL" \
                --spec-draft-n-max 9 --spec-draft-ngl 99)
  fi
  start_llama agentalloy-interp "$INTERP_IMAGE" "$MODEL_PORT" --model "/models/$INTERP_MODEL" \
    --alias "$INTERP_NAME" --jinja --temp 0.2 --top-k 80 --repeat-penalty 1.05 "${draft_args[@]}"
  start_llama agentalloy-embed "$IMAGE" "$EMBED_PORT" --model "/models/$EMBED_MODEL" --embeddings --pooling mean --ctx-size 2048 --batch-size 2048 --ubatch-size 2048

  write_env
  cmd_serve
}

cmd_serve(){
  cd "$REPO_ROOT"; source "$INSTANCE/env.sh"
  # `|| true`: with pipefail, an empty port makes the service_pid pipeline
  # exit 1, and set -e would abort here before serving.
  local pid; pid="$(service_pid || true)"
  [ -n "${pid:-}" ] && { log "stopping existing service pid $pid"; kill "$pid" 2>/dev/null || true; sleep 2; }
  log "serving on :$PORT"
  # `python -m agentalloy` = the v2 stack CLI (agentalloy.cli → server.py).
  # The installed `agentalloy` entry is the v10 product shell since the M0
  # merge — `agentalloy serve` would start agentalloy.app:app, which has no
  # /chat or JSON /status.
  nohup uv run python -m agentalloy serve --port "$PORT" --host 127.0.0.1 > "$INSTANCE/serve.log" 2>&1 &
  # 300s: a cold instance dir ingests the whole repo (parse + embed every
  # chunk) before the port binds — measured ~2.5 min on the agentalloy repo.
  wait_health "http://127.0.0.1:$PORT/health" 300 \
    && log "service healthy: http://127.0.0.1:$PORT" \
    || { tail -20 "$INSTANCE/serve.log"; die "service did not come up (see $INSTANCE/serve.log)"; }
  log "dashboard: http://127.0.0.1:$PORT/dashboard"
}

cmd_status(){
  printf 'service :%s  -> %s\n' "$PORT"       "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health 2>/dev/null || echo down)"
  printf 'interp  :%s  -> %s\n' "$MODEL_PORT" "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$MODEL_PORT/health 2>/dev/null || echo down)"
  printf 'embed   :%s  -> %s\n' "$EMBED_PORT" "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$EMBED_PORT/health 2>/dev/null || echo down)"
  echo ""
  curl -s http://127.0.0.1:$PORT/status 2>/dev/null | python3 -m json.tool 2>/dev/null || true
}

cmd_smoke(){
  log "/health"
  curl -s http://127.0.0.1:$PORT/health 2>/dev/null | python3 -m json.tool 2>/dev/null

  log "/status"
  curl -s http://127.0.0.1:$PORT/status 2>/dev/null | python3 -m json.tool 2>/dev/null

  log "/chat (simple query)"
  curl -s -X POST http://127.0.0.1:$PORT/chat \
    -H 'content-type: application/json' \
    -d '{"messages":[{"role":"user","content":"what phase are we in?"}]}' \
    | python3 -m json.tool 2>/dev/null
}

cmd_down(){
  # `|| true`: see cmd_serve — a free port must not abort the pipeline.
  local pid; pid="$(service_pid || true)"
  [ -n "${pid:-}" ] && { log "killing service pid $pid"; kill "$pid" 2>/dev/null || true; }
  log "stopping model servers"
  "$RUNTIME" stop agentalloy-interp agentalloy-embed 2>/dev/null || true
}

case "${1:-up}" in
  up) cmd_up ;; serve) cmd_serve ;;
  status) cmd_status ;; smoke) cmd_smoke ;; down) cmd_down ;;
  *) echo "usage: $0 {up|serve|status|smoke|down}"; exit 2 ;;
esac
