#!/bin/bash
set -e

# App directory (configurable via APP_DIR env var, default /app)
APP_DIR=${APP_DIR:-/app}
LOCK="$APP_DIR/.bootstrap-lock"
COMPLETE="$APP_DIR/.bootstrap-complete"
PROGRESS="$APP_DIR/.bootstrap-progress"
PROGRESS_TMP="$APP_DIR/.bootstrap-progress.tmp"
CHECKPOINTS="$APP_DIR/.bootstrap-checkpoints"
INSTALL_LOCK="$APP_DIR/.install-packs-lock"

# --- Stale lock recovery -------------------------------------------
# If the previous run crashed mid-bootstrap, the lock file persists
# in the data volume. A lock older than 2h is considered stale.
if [ -f "$LOCK" ] && [ ! -f "$COMPLETE" ]; then
    LOCK_MTIME=$(stat -c %Y "$LOCK" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    if [ "$LOCK_MTIME" -gt 0 ] && [ $((NOW - LOCK_MTIME)) -gt 7200 ]; then
        echo ">> Stale bootstrap lock detected (>2h) - starting fresh"
        rm -f "$LOCK" "$CHECKPOINTS" "$PROGRESS" "$PROGRESS_TMP"
    fi
fi

# --- Checkpoint helpers --------------------------------------------
# pack_already_done: 0 (true) if the pack name appears in checkpoints.
# A corrupt checkpoint file simply yields no matches — treated as
# "not done yet", so we re-run the pack rather than failing closed.
pack_already_done() {
    [ -f "$CHECKPOINTS" ] || return 1
    grep -Fq "\"pack\": \"$1\"" "$CHECKPOINTS" 2>/dev/null
}

# write_progress <current_pack> <ingested> <total>
# Atomic JSON write: stage to .tmp then mv onto target. Readers either
# see the prior snapshot or the new one, never a torn write.
write_progress() {
    cat > "$PROGRESS_TMP" <<JSON
{"current_pack": "$1", "packs_ingested": $2, "packs_total": $3, "updated_at": "$(date -Iseconds)"}
JSON
    mv "$PROGRESS_TMP" "$PROGRESS"
}

# --- Bootstrap decision -------------------------------------------
BOOTSTRAP_NEEDED=true
if [ -f "$COMPLETE" ]; then
    BOOTSTRAP_NEEDED=false
    echo ">> Bootstrap already complete - skipping to uvicorn"
fi

# --- Prebuilt corpus seed ------------------------------------------
# CI-built images carry a fully ingested + embedded corpus under
# /app/corpus-seed (.github/workflows/container-build.yml). When it
# is present and the data volume has no corpus yet, copy it in and
# skip per-pack ingest + re-embed — first run drops from ~30 min of
# CPU embedding to seconds. llama-server setup stays unconditional:
# query embedding at compose time still needs the model at runtime.
SEED_DIR="${SEED_DIR:-/app/corpus-seed}"
VOL_STAMP="$APP_DIR/data/corpus-stamp.json"
CORPUS_SEEDED=false

# stamp_value <file> <key> - read a value from the flat corpus-stamp.json.
stamp_value() {
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" "$1" 2>/dev/null | head -1
}

# (Re-)seed the corpus from the image: on an empty volume (first run) or
# when the image corpus differs (packs_hash / embedding_dim) so that
# `agentalloy upgrade` self-heals from the fast prebuilt seed. Runs every
# boot (not just bootstrap) so upgrades, which keep .bootstrap-complete,
# still refresh.
NEED_SEED=false
if [ -f "$SEED_DIR/corpus-stamp.json" ]; then
    if [ ! -f "$APP_DIR/data/skills.duck" ]; then
        NEED_SEED=true
    elif [ ! -f "$VOL_STAMP" ]; then
        # Corpus present but unstamped (e.g. a pre-stamp volume, or one
        # whose stamp was lost): we can't verify it matches the image, so
        # re-seed from the authoritative corpus rather than trust a
        # partial always-on reconcile that leaves other packs stale.
        NEED_SEED=true
        echo ">> Volume corpus has no stamp - re-seeding from image to verify"
    elif [ "$(stamp_value "$SEED_DIR/corpus-stamp.json" packs_hash)" != "$(stamp_value "$VOL_STAMP" packs_hash)" ] \
         || [ "$(stamp_value "$SEED_DIR/corpus-stamp.json" embedding_dim)" != "$(stamp_value "$VOL_STAMP" embedding_dim)" ]; then
        NEED_SEED=true
        echo ">> Image corpus differs from volume (upgrade) - re-seeding"
    fi
fi

if [ "$NEED_SEED" = "true" ]; then
    echo ">> Seeding prebuilt corpus from image (skipping pack ingest + re-embed)"
    mkdir -p "$APP_DIR/data"
    rm -rf "$APP_DIR/data/ladybug" "$APP_DIR/data/skills.duck"
    cp -a "$SEED_DIR/ladybug" "$APP_DIR/data/ladybug"
    cp "$SEED_DIR/skills.duck" "$APP_DIR/data/skills.duck"
    cp "$SEED_DIR/corpus-stamp.json" "$VOL_STAMP"
    CORPUS_SEEDED=true
    # Surface the seed to host-side readiness polling (same atomic
    # tmp+mv pattern as the model_pull phase).
    cat > "$PROGRESS_TMP" <<JSON
{"phase": "corpus_seeded", "current_pack": "", "packs_ingested": 0, "packs_total": 0, "updated_at": "$(date -Iseconds)"}
JSON
    mv "$PROGRESS_TMP" "$PROGRESS"
fi

# --- llama.cpp model + server config -------------------------------
# Two llama-server daemons back the runtime: an embed server on 47951
# (--embeddings, query embedding at compose time) and a reranker server
# on 47952 (completions mode, /v1/completions with logprobs for the
# intent classifier). Both GGUFs are downloaded on first boot into the
# data volume so they persist across restarts.
MODELS_DIR="$APP_DIR/data/models"
EMBED_GGUF="$MODELS_DIR/nomic-embed-text-v1.5.Q8_0.gguf"
RERANK_GGUF="$MODELS_DIR/Qwen3-Reranker-0.6B-Q8_0.gguf"
EMBED_URL="https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
RERANK_URL="https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/resolve/main/qwen3-reranker-0.6b-q8_0.gguf"

if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    # Record bootstrap start. Content is the canonical timestamp;
    # mtime is the fallback for stale-lock detection.
    date -Iseconds > "$LOCK"

    mkdir -p "$MODELS_DIR"
    if [ ! -f "$EMBED_GGUF" ] || [ ! -f "$RERANK_GGUF" ]; then
        echo ">> Downloading llama.cpp GGUF models..."
        cat > "$PROGRESS_TMP" <<JSON
{"current_pack": "gguf-models", "packs_ingested": 0, "packs_total": 1, "phase": "model_download", "status": "in_progress", "updated_at": "$(date -Iseconds)"}
JSON
        mv "$PROGRESS_TMP" "$PROGRESS"
        if [ ! -f "$EMBED_GGUF" ]; then
            echo ">> Fetching embed model (nomic-embed-text-v1.5-Q8_0)..."
            curl -fsSL -o "$EMBED_GGUF" "$EMBED_URL" \
                --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30
        fi
        if [ ! -f "$RERANK_GGUF" ]; then
            echo ">> Fetching reranker model (Qwen3-Reranker-0.6B-Q8_0)..."
            curl -fsSL -o "$RERANK_GGUF" "$RERANK_URL" \
                --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30
        fi
        echo "Model download complete"
    fi
fi

# --- Start the llama-server daemons (every boot) -------------------
# These are long-lived runtime daemons, not bootstrap-only steps: even
# after .bootstrap-complete, query embedding + intent reranking need
# them up. Start them before uvicorn so /readiness reflects a usable
# service.
echo ">> Starting embed llama-server on 47951..."
llama-server --embeddings --pooling mean --ubatch-size 2048 --host 127.0.0.1 --port 47951 -m "$EMBED_GGUF" &
EMBED_PID=$!
echo ">> Starting reranker llama-server on 47952..."
llama-server --host 127.0.0.1 --port 47952 -m "$RERANK_GGUF" &
RERANK_PID=$!

echo ">> Waiting for llama-server health (47951 + 47952)..."
for i in $(seq 1 120); do
    EMBED_OK=false
    RERANK_OK=false
    curl -sf http://127.0.0.1:47951/health > /dev/null 2>&1 && EMBED_OK=true
    curl -sf http://127.0.0.1:47952/health > /dev/null 2>&1 && RERANK_OK=true
    if [ "$EMBED_OK" = "true" ] && [ "$RERANK_OK" = "true" ]; then
        echo ">> llama-server ready (embed + reranker)"
        break
    fi
    sleep 1
done

if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    echo ">> Running migrations..."
    uv run python -m agentalloy.migrate
fi

# --- SIGTERM/SIGINT trap (covers llama-servers + uvicorn) ----------
trap 'kill ${EMBED_PID:-} ${RERANK_PID:-} ${UVICORN_PID:-} 2>/dev/null; exit 0' SIGTERM SIGINT

# Pack ingest runs only when there is no corpus to start from: not seeded
# this boot (CORPUS_SEEDED) AND no existing volume corpus (skills.duck). A
# reused/populated volume is left to the seed logic above, so we never run a
# partial always-on reconcile over an already-full corpus.
if [ "$BOOTSTRAP_NEEDED" = "true" ] && [ "$CORPUS_SEEDED" = "false" ] \
   && [ ! -f "$APP_DIR/data/skills.duck" ]; then
    if [ -n "${AGENTALLOY_PACKS:-}" ]; then
        IFS="," read -ra PACK_LIST <<< "$AGENTALLOY_PACKS"
        TOTAL=${#PACK_LIST[@]}
        INGESTED=0
        if [ -f "$CHECKPOINTS" ]; then
            INGESTED=$(grep -c "pack_ingested" "$CHECKPOINTS" 2>/dev/null || echo 0)
        fi
        for pack in "${PACK_LIST[@]}"; do
            pack=$(echo "$pack" | tr -d ' ')
            [ -z "$pack" ] && continue
            if pack_already_done "$pack"; then
                echo ">> Pack $pack already ingested - skipping"
                continue
            fi
            write_progress "$pack" "$INGESTED" "$TOTAL"
            echo ">> Installing pack: $pack"
            touch "$INSTALL_LOCK"
            uv run agentalloy install-packs --packs "$pack" --no-restart
            rm -f "$INSTALL_LOCK"
            printf '{"step": "pack_ingested", "pack": "%s", "at": "%s"}\n' "$pack" "$(date -Iseconds)" >> "$CHECKPOINTS"
            INGESTED=$((INGESTED + 1))
        done
        write_progress "" "$INGESTED" "$TOTAL"
    else
        echo ">> No explicit packs — installing always-on packs"
        uv run agentalloy install-packs --no-restart
    fi
fi

# Mark bootstrap complete and clear the lock (covers both the
# pack-ingest path and the seeded-corpus path).
if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    rm -f "$LOCK"
    touch "$COMPLETE"
    echo ">> Bootstrap complete"
fi

# Start uvicorn AFTER bootstrap completes to avoid Ladybug lock conflicts.
echo ">> Starting uvicorn..."
uv run uvicorn agentalloy.app:app --host 0.0.0.0 --port 47950 --log-level info &
UVICORN_PID=$!

# Block on uvicorn — its exit is the container's exit.
wait $UVICORN_PID
