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
#         or manually (bare run — bootstrap runs automatically):
#         podman run --replace -d --name agentalloy -p 47950:47950 \
#                    -v agentalloy-data:/app/data -v ~/.ollama:/root/.ollama \
#                    ghcr.io/nrmeyers/agentalloy:latest
#         Pass -e AGENTIALLOY_PACKS=core,webhooks to install specific packs on a locally
#         built image that has no prebuilt corpus seed (GHCR images seed all packs automatically).

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
# The corpus (LadybugDB + DuckDB) is not committed to the repo — CI bakes a
# prebuilt corpus into published images under /app/corpus-seed (see
# .github/workflows/container-build.yml); the entrypoint copies it into the
# data volume on first run so users skip the ~30-min CPU ingest+embed. For
# local builds corpus-seed/ holds only .keep and the entrypoint falls back
# to building the corpus via `agentalloy install-packs` as before.
RUN mkdir -p data
COPY corpus-seed/ /app/corpus-seed/

# Bake the bootstrap entrypoint into the image so bare ``podman run``
# bootstraps correctly without requiring the setup wizard to bind-mount a
# generated script. container/entrypoint.sh is generated from
# _build_entrypoint_script("") — a test in tests/test_container_edge_cases.py
# asserts the two are identical so they can't drift.
COPY container/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

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
# uses _wait_for_readiness() to poll /readiness with exponential backoff rather
# than relying on the OCI HEALTHCHECK directive (which Podman doesn't always
# honor in its default OCI image format).

# Default ENTRYPOINT/CMD: bare ``podman run ghcr.io/nrmeyers/agentalloy:latest``
# runs the baked bootstrap entrypoint, which seeds the prebuilt corpus (GHCR
# images), starts Ollama, pulls the embedding model if needed, then starts
# uvicorn. The setup wizard bind-mounts a generated script on top of this
# default when packs are specified explicitly.
ENTRYPOINT ["/app/entrypoint.sh"]
