#!/usr/bin/env bash
# v5-bringup.sh — bring up the full agentalloy v5 stack from this checkout for
# local end-to-end testing, including the reranker (Stage B).
#
#   ./scripts/v5-bringup.sh up       # uv sync + start model servers + build corpus + serve (default)
#   ./scripts/v5-bringup.sh smoke    # hit /health /retrieve /compose and print results
#   ./scripts/v5-bringup.sh status   # show service + model-server health
#   ./scripts/v5-bringup.sh rebuild  # force a full reembed of the corpus
#   ./scripts/v5-bringup.sh serve    # (re)start just the service
#   ./scripts/v5-bringup.sh down     # stop the service + model servers (keeps the corpus)
#
# It runs against the checkout this script lives in (no separate clone), keeps
# all state in an isolated XDG instance dir (so it never touches a system
# agentalloy install), and is idempotent — re-running `up` reuses an existing
# corpus and already-running model servers.
#
# The two model servers are llama.cpp (`localhost/llama-server-cuda` by default)
# fed local GGUFs. EVERY knob below is overridable via the environment, e.g.
#   MODELS_DIR=/srv/models IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda ./scripts/v5-bringup.sh up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- knobs (override via env) ----------------------------------------------
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"               # this checkout
INSTANCE="${INSTANCE:-$HOME/.local/share/agentalloy-v5-instance}"     # isolated XDG state/data
MODELS_DIR="${MODELS_DIR:-/mnt/ai-data/llama/models}"                 # host dir holding the GGUFs
IMAGE="${IMAGE:-localhost/llama-server-cuda:latest}"                  # llama.cpp server image
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text-v1.5.Q8_0.gguf}"
RERANK_MODEL="${RERANK_MODEL:-Qwen3-Reranker-0.6B.Q8_0.gguf}"
PORT="${PORT:-47960}"; EMBED_PORT="${EMBED_PORT:-47953}"; RERANK_PORT="${RERANK_PORT:-47952}"
RUNTIME="${RUNTIME:-podman}"
# Pin the model servers to a specific GPU by UUID. Default: the first non-3090
# (the 3090 here is full of another model); set GPU_UUID="" to use all GPUs.
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

start_llama(){ # $1=name $2=port  $3...=extra llama-server args
  local name="$1" port="$2"; shift 2
  if [ "$("$RUNTIME" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ]; then
    log "$name already running on :$port"; return 0
  fi
  "$RUNTIME" rm -f "$name" >/dev/null 2>&1 || true
  log "starting $name on :$port (gpu=${GPU_UUID:-all})"
  "$RUNTIME" run -d --name "$name" \
    --device nvidia.com/gpu=all -e NVIDIA_VISIBLE_DEVICES=all \
    ${GPU_UUID:+-e CUDA_VISIBLE_DEVICES=$GPU_UUID} \
    -v "$MODELS_DIR:/models:ro" -p "$port:$port" \
    "$IMAGE" --n-gpu-layers 99 --host 0.0.0.0 --port "$port" "$@" >/dev/null
  wait_health "http://localhost:$port/health" 60 || { "$RUNTIME" logs --tail 20 "$name" 2>&1; die "$name failed to become healthy"; }
  log "$name healthy"
}

write_env(){
  mkdir -p "$INSTANCE/config" "$INSTANCE/data"
  cat > "$INSTANCE/env.sh" <<EOF
# isolated v5 dev instance — source before any \`uv run agentalloy …\`
export XDG_CONFIG_HOME=$INSTANCE/config
export XDG_DATA_HOME=$INSTANCE/data
export RUNTIME_EMBED_BASE_URL=http://localhost:$EMBED_PORT
export LM_ASSIST=arbitrate
export LM_ASSIST_RERANK_URL=http://127.0.0.1:$RERANK_PORT
export LM_ASSIST_MODEL=$RERANK_MODEL
export LM_ASSIST_TIMEOUT_MS=2000
EOF
}

corpus_vectors(){ # echo current Lance vector count (0 if unbuilt/unreadable)
  ( cd "$REPO_ROOT" && source "$INSTANCE/env.sh" && uv run python -c '
from agentalloy.config import Settings
from agentalloy.storage.open import open_fragments
try: print(open_fragments(Settings()).count_embeddings())
except Exception: print(0)' 2>/dev/null ) || echo 0
}

cmd_up(){
  command -v uv >/dev/null || die "uv not on PATH"
  command -v "$RUNTIME" >/dev/null || die "$RUNTIME not on PATH"
  [ -d "$MODELS_DIR" ] || die "MODELS_DIR not found: $MODELS_DIR (set MODELS_DIR=…)"
  cd "$REPO_ROOT"
  log "uv sync ($REPO_ROOT)"; uv sync
  # model servers — embed needs ubatch 2048 (>512-token fragments); rerank needs
  # --parallel 16 so /compose can score its ~72-candidate pool inside the timeout.
  start_llama agentalloy-embed-v5  "$EMBED_PORT"  --model "/models/$EMBED_MODEL"  --embeddings --pooling mean --ctx-size 2048 --batch-size 2048 --ubatch-size 2048
  start_llama agentalloy-rerank-v5 "$RERANK_PORT" --model "/models/$RERANK_MODEL" --ctx-size 8192 --parallel 16
  write_env
  local n; n="$(corpus_vectors)"
  if [ "${n:-0}" -lt 1000 ]; then
    log "building corpus (all packs) — first run, a few minutes"
    ( cd "$REPO_ROOT" && source "$INSTANCE/env.sh" && uv run agentalloy install-packs --packs all --non-interactive --no-restart --json >/dev/null )
    log "reembed (full) via the batch-2048 embed server"
    ( cd "$REPO_ROOT" && source "$INSTANCE/env.sh" && uv run agentalloy reembed --force --no-restart )
  else
    log "corpus already built ($n vectors) — skipping (use 'rebuild' to force)"
  fi
  cmd_serve
}

cmd_serve(){
  cd "$REPO_ROOT"; source "$INSTANCE/env.sh"
  local pid; pid="$(service_pid)"
  [ -n "${pid:-}" ] && { log "stopping existing service pid $pid"; kill "$pid" 2>/dev/null || true; sleep 2; }
  log "serving on :$PORT (Stage B arbitrate)"
  nohup uv run agentalloy serve --port "$PORT" --host 127.0.0.1 > "$INSTANCE/serve.log" 2>&1 &
  wait_health "http://127.0.0.1:$PORT/health" 30 \
    && log "service healthy: http://127.0.0.1:$PORT" \
    || { tail -20 "$INSTANCE/serve.log"; die "service did not come up (see $INSTANCE/serve.log)"; }
}

cmd_rebuild(){ cd "$REPO_ROOT"; source "$INSTANCE/env.sh"; log "force reembed"; uv run agentalloy reembed --force --no-restart; }

cmd_status(){
  printf 'service :%s  -> %s\n' "$PORT"        "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health 2>/dev/null || echo down)"
  printf 'embed   :%s  -> %s\n' "$EMBED_PORT"  "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$EMBED_PORT/health 2>/dev/null || echo down)"
  printf 'rerank  :%s  -> %s\n' "$RERANK_PORT" "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$RERANK_PORT/health 2>/dev/null || echo down)"
  curl -s http://127.0.0.1:$PORT/health 2>/dev/null | python3 -m json.tool 2>/dev/null || true
}

cmd_smoke(){
  log "/retrieve (task='write a failing unit test', phase=build)"
  curl -s -X POST http://127.0.0.1:$PORT/retrieve -H 'content-type: application/json' \
    -d '{"task":"write a failing unit test before the implementation","phase":"build","k":5}' \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)
for r in d.get("results",[]):
    print("  %.3f  %s" % (r["score"], r["skill_id"]))'
  log "/compose (task='add a REST endpoint with validation and tests', phase=build)"
  curl -s -X POST http://127.0.0.1:$PORT/compose -H 'content-type: application/json' \
    -d '{"task":"add a REST endpoint with input validation and tests","phase":"build","k":6}' \
    | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("  status:", d["status"], "| output_chars:", len(d["output"]), "| stageB:", d["telemetry"].get("lm_assist_outcome"))
print("  skills:", d["source_skills"])'
}

cmd_down(){
  local pid; pid="$(service_pid)"
  [ -n "${pid:-}" ] && { log "killing service pid $pid"; kill "$pid" 2>/dev/null || true; }
  log "stopping model servers"; "$RUNTIME" stop agentalloy-embed-v5 agentalloy-rerank-v5 2>/dev/null || true
}

case "${1:-up}" in
  up) cmd_up ;; serve) cmd_serve ;; rebuild) cmd_rebuild ;;
  status) cmd_status ;; smoke) cmd_smoke ;; down) cmd_down ;;
  *) echo "usage: $0 {up|serve|rebuild|status|smoke|down}"; exit 2 ;;
esac
