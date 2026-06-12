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
# CPU embedding to seconds. Ollama setup stays unconditional: query
# embedding at compose time still needs the model at runtime.
SEED_DIR="${SEED_DIR:-/app/corpus-seed}"
CORPUS_SEEDED=false
if [ "$BOOTSTRAP_NEEDED" = "true" ] \
   && [ -f "$SEED_DIR/corpus-stamp.json" ] \
   && [ ! -f "$APP_DIR/data/skills.duck" ]; then
    echo ">> Seeding prebuilt corpus from image (skipping pack ingest + re-embed)"
    mkdir -p "$APP_DIR/data"
    cp -a "$SEED_DIR/ladybug" "$APP_DIR/data/ladybug"
    cp "$SEED_DIR/skills.duck" "$APP_DIR/data/skills.duck"
    cp "$SEED_DIR/corpus-stamp.json" "$APP_DIR/data/corpus-stamp.json"
    CORPUS_SEEDED=true
    # Surface the seed to host-side readiness polling (same atomic
    # tmp+mv pattern as the model_pull phase).
    cat > "$PROGRESS_TMP" <<JSON
{"phase": "corpus_seeded", "current_pack": "", "packs_ingested": 0, "packs_total": 0, "updated_at": "$(date -Iseconds)"}
JSON
    mv "$PROGRESS_TMP" "$PROGRESS"
fi

if [ "$BOOTSTRAP_NEEDED" = "true" ]; then
    # Record bootstrap start. Content is the canonical timestamp;
    # mtime is the fallback for stale-lock detection.
    date -Iseconds > "$LOCK"

    # Ollama installation
    if ! command -v ollama &> /dev/null; then
        echo ">> Installing Ollama..."
        curl -fsSL https://ollama.ai/install.sh | sh
    fi

    echo ">> Starting Ollama..."
    OLLAMA_HOST=127.0.0.1:11434 ollama serve &
    OLLAMA_PID=$!

    for i in $(seq 1 30); do
        if curl -sf http://127.0.0.1:11434 > /dev/null 2>&1; then
            echo ">> Ollama is ready"
            break
        fi
        sleep 1
    done

    echo ">> Checking embedding model..."
    if ! ollama list | grep -q qwen3-embedding; then
        echo ">> Pulling qwen3-embedding:0.6b..."
        cat > "$PROGRESS_TMP" <<JSON
{"current_pack": "qwen3-embedding:0.6b", "packs_ingested": 0, "packs_total": 1, "phase": "model_pull", "model": "qwen3-embedding:0.6b", "status": "in_progress", "updated_at": "$(date -Iseconds)"}
JSON
        mv "$PROGRESS_TMP" "$PROGRESS"
        ollama pull qwen3-embedding:0.6b
        echo "Model pull complete"
    fi

    echo ">> Running migrations..."
    uv run python -m agentalloy.migrate
fi

# --- SIGTERM trap (covers Ollama + uvicorn) -----------------------
trap 'kill ${OLLAMA_PID:-} ${UVICORN_PID:-} 2>/dev/null; exit 0' SIGTERM

# Pack ingest runs only when the corpus was not seeded from the image.
if [ "$BOOTSTRAP_NEEDED" = "true" ] && [ "$CORPUS_SEEDED" = "false" ]; then
    if [ -n "${AGENTIALLOY_PACKS:-}" ]; then
        IFS="," read -ra PACK_LIST <<< "$AGENTIALLOY_PACKS"
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
