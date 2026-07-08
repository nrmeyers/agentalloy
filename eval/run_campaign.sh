#!/usr/bin/env bash
# 2026-06 rerun campaign orchestrator (see eval/campaign-2026-06.md).
#
# Cycles all four benchmark models via load-model.sh and runs the generic +
# domain legs for each. Sequential by design: the inference host serves one
# model at a time, all pinned to the RTX 3090.
#
#   ./eval/run_campaign.sh            # all four models
#   ./eval/run_campaign.sh 27B LFM    # subset
set -euo pipefail

cd "$(dirname "$0")/.."

LOAD_MODEL="${LOAD_MODEL:-$HOME/scripts/load-model.sh}"
export LM_STUDIO_URL="${LM_STUDIO_URL:-http://192.168.4.26:60000}"
export AGENTALLOY_URL="${AGENTALLOY_URL:-http://localhost:47950}"
N="${N:-5}"

declare -A ALIASES=(
    [35B]=qwen3.6-35B-A3B
    [27B]=qwen3.6-27b
    [12B]=gemma-4-12b-it
    [LFM]=lfm2.5-8b-a1b-coder
)
MODELS=("$@")
[[ ${#MODELS[@]} -eq 0 ]] && MODELS=(35B 27B 12B LFM)

curl -sf -m 5 "$AGENTALLOY_URL/compose" -X POST -H 'content-type: application/json' \
    -d '{"task":"preflight","phase":"build","k":1}' >/dev/null \
    || { echo "FATAL: agentalloy service not answering at $AGENTALLOY_URL"; exit 1; }

for m in "${MODELS[@]}"; do
    alias="${ALIASES[$m]:?unknown model flag $m}"
    echo "=================================================================="
    echo "=== $(date -Is)  model $m ($alias)"
    echo "=================================================================="
    "$LOAD_MODEL" "$m"

    export AGENT_MODEL="$alias"
    echo "--- generic leg ($m) ---"
    uv run python -m eval.run_poc --n "$N" --label "generic-$m" \
        --conditions none composed external
    echo "--- domain leg ($m) ---"
    uv run python -m eval.run_poc --n "$N" --task-set domain --label "domain-$m" \
        --conditions none composed composed-contract flat external
done

echo "=== campaign complete: $(date -Is)"
