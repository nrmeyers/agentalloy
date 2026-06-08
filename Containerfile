# Containerfile for the AgentAlloy service.
# Compatible with Podman (project preference) and Docker (works as Dockerfile via --file Containerfile).
#
# Build variants:
#   # Lightweight image (~300 MB, no pre-pulled model) — for general users
#   podman build -t agentalloy:latest -f Containerfile .
#
#   # Full image (~975 MB, model pre-pulled) — for air-gapped/enterprise
#   podman build --build-arg PULL_MODEL=true -t agentalloy:full -f Containerfile .
#
# Run:    agentalloy setup --deployment container  (recommended — single-container with entrypoint)
#         or manually: podman run --replace -d --name agentalloy -p 47950:47950 \
#                      -v agentalloy-data:/app/data -v ~/.ollama:/root/.ollama \
#                      -e AGENTIALLOY_PACKS= -e ENTRYPOINT=/app/entrypoint.sh \
#                      agentalloy:latest /app/entrypoint.sh

FROM python:3.12-slim AS base

# Install uv (Astral) and minimal runtime deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl zstd \
    && rm -rf /var/lib/apt/lists/*

# uv is the project's package manager (matches host conventions)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first for layer-cache friendliness
COPY pyproject.toml uv.lock ./

# Install third-party deps without trying to build the project itself
# (needs README.md, src/, etc. — added in the next layer).
RUN uv sync --frozen --no-dev --no-install-project

# Copy the project source and README (used by hatchling for metadata),
# then install the project itself.
COPY README.md ./
COPY src/ ./src/

# Create an empty data dir so the image is runnable without a bind mount.
# The corpus (LadybugDB + DuckDB) is not shipped in the repo — it's
# generated locally on first install via `agentalloy install-packs` and
# `agentalloy.migrate`. The entrypoint script (generated at runtime by
# `agentalloy setup --deployment container`) bind-mounts a host volume
# onto /app/data so user data persists across container restarts.
RUN mkdir -p data

RUN uv sync --frozen --no-dev

ENV LADYBUG_DB_PATH=/app/data/ladybug \
    DUCKDB_PATH=/app/data/skills.duck \
    LOG_LEVEL=INFO

EXPOSE 47950

# Conditional model pre-pull for the "full" image variant.
# When PULL_MODEL=true, this layer pulls the embedding model into the image.
# This is useful for air-gapped/enterprise deployments where the model
# should be baked into the image rather than downloaded at runtime.
ARG PULL_MODEL=false
RUN if [ "$PULL_MODEL" = "true" ]; then \
        echo "Pre-pulling embedding model into image (this may take several minutes)..." && \
        curl -fsSL https://ollama.ai/install.sh | sh && \
        OLLAMA_HOST=127.0.0.1:11434 ollama serve & OLLAMA_PID=$! && \
        sleep 5 && \
        ollama pull qwen3-embedding:0.6b && \
        kill "$OLLAMA_PID" 2>/dev/null || true && \
        echo "Model pre-pulled successfully."; \
    else \
        echo "Skipping model pre-pull (latest variant)."; \
    fi

# Note: HEALTHCHECK is intentionally omitted — the container runtime module
# uses _wait_for_health() to poll /health with exponential backoff rather
# than relying on the OCI HEALTHCHECK directive (which Podman doesn't always
# honor in its default OCI image format).

CMD ["uv", "run", "uvicorn", "agentalloy.app:app", "--host", "0.0.0.0", "--port", "47950"]
