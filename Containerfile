# Containerfile for the AgentAlloy service.
# Compatible with Podman (project preference) and Docker (works as Dockerfile via --file Containerfile).
#
# Build variants:
#   # Lightweight image (no GGUFs baked) — models download on first boot
#   podman build -t agentalloy:latest -f Containerfile .
#
#   # Full image (GGUFs pre-baked into the image) — for air-gapped/enterprise
#   podman build --build-arg PULL_MODEL=true -t agentalloy:full -f Containerfile .
#
# Run:    agentalloy setup --deployment container  (recommended — single-container with entrypoint)
#         or manually (bare run — bootstrap runs automatically):
#         podman run --replace -d --name agentalloy -p 47950:47950 \
#                    -v agentalloy-data:/app/data \
#                    ghcr.io/nrmeyers/agentalloy:latest
#         The GGUF models persist under /app/data/models inside the agentalloy-data
#         volume, so they download only once across restarts.
#         Pass -e AGENTIALLOY_PACKS=core,webhooks to install specific packs on a locally
#         built image that has no prebuilt corpus seed (GHCR images seed all packs automatically).

# ---------------------------------------------------------------------------
# Stage 0: llama.cpp binaries. The :full image ships llama-server plus its
# co-located shared libs under /app (libllama*.so, libggml*.so, the per-arch
# libggml-cpu-*.so loaded at runtime). We copy the whole dir and front it with
# a wrapper that sets LD_LIBRARY_PATH so the dynamic loader finds the libs.
# ---------------------------------------------------------------------------
FROM ghcr.io/ggml-org/llama.cpp:full AS llamacpp

# ---------------------------------------------------------------------------
# Stage 1: web UI build. Bakes the SPA into the image so the dashboard is live
# on first boot — container users never run `agentalloy pull-web`.
# ---------------------------------------------------------------------------
FROM node:22-slim AS webui
WORKDIR /web
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN corepack enable && corepack prepare pnpm@10 --activate && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build && test -f dist/index.html

FROM python:3.12-slim AS base

# Install uv (Astral) and minimal runtime deps. libgomp1 is the OpenMP runtime
# that llama-server links against (the rest of its libs come from the llamacpp
# stage below). git is required by the code-index staleness/auto-refresh path
# (it shells out to `git` for HEAD/rev-list on the mounted repos).
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git zstd libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# uv is the project's package manager (matches host conventions)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# llama-server + its shared libraries (from the llamacpp stage). The wrapper
# at /usr/local/bin/llama-server sets LD_LIBRARY_PATH so the loader resolves
# the co-located .so files.
COPY --from=llamacpp /app /opt/llama.cpp/
RUN printf '#!/bin/sh\nexport LD_LIBRARY_PATH=/opt/llama.cpp:${LD_LIBRARY_PATH}\nexec /opt/llama.cpp/llama-server "$@"\n' \
        > /usr/local/bin/llama-server \
    && chmod +x /usr/local/bin/llama-server \
    && llama-server --version

WORKDIR /app

# Copy dependency manifests first for layer-cache friendliness
COPY pyproject.toml uv.lock ./

# Install third-party deps without trying to build the project itself
# (needs README.md, src/, etc. — added in the next layer).
# --extra code-index: ship tree-sitter + grammars so the image can serve the
# optional code-index module when the operator sets CODE_INDEX_ENABLED=1
# (module stays off by default; the entrypoint needs no changes).
RUN uv sync --frozen --no-dev --no-install-project --extra code-index

# Copy the project source and README (used by hatchling for metadata),
# then install the project itself.
COPY README.md ./
COPY src/ ./src/

# Create an empty data dir so the image is runnable without a bind mount.
# The corpus (LanceDB fragments.lance + DuckDB agentalloy.duck) is not committed
# to the repo — CI bakes a prebuilt corpus into published images under
# /app/corpus-seed (see
# .github/workflows/container-build.yml); the entrypoint copies it into the
# data volume on first run so users skip the ~30-min CPU ingest+embed. For
# local builds corpus-seed/ holds only .keep and the entrypoint falls back
# to building the corpus via `agentalloy install-packs` as before.
RUN mkdir -p data
COPY corpus-seed/ /app/corpus-seed/

# Prebuilt web UI (from the webui stage); AGENTALLOY_WEB_DIST below points
# spa.py at it, short-circuiting the repo-layout / pull-web resolution.
COPY --from=webui /web/dist /app/web-dist/

# Bake the bootstrap entrypoint into the image so bare ``podman run``
# bootstraps correctly without requiring the setup wizard to bind-mount a
# generated script. container/entrypoint.sh is generated from
# _build_entrypoint_script("") — a test in tests/test_container_edge_cases.py
# asserts the two are identical so they can't drift.
COPY container/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN uv sync --frozen --no-dev --extra code-index

# Runtime configuration. The two llama-server daemons are addressed here:
# the embed server on 47951 (RUNTIME_EMBED_BASE_URL) and the reranker server
# on 47952 (SIGNAL_INTENT_RERANK_URL, completions mode). Model filenames match
# the GGUFs the entrypoint downloads into /app/data/models.
#
# LM_ASSIST (Stage B fragment reranker) is OFF in the container — the image is
# CPU-only (no GPU passthrough) and CPU Stage B is not viable at the budget.
# Measured 2026-07-09 on the shipped 47952 reranker (`--parallel 1 -c 2048`): real
# distinct-doc scoring costs ~1800ms/candidate, and production telemetry isolates
# Stage B at ~6.6s median / ~11s p90 added latency vs the 203ms deterministic path
# — 2.3x the 3000ms budget, so it times out and fails open on real composes. (The
# "~145ms warm" figure that once justified arbitrate here was a KV-cache-reuse
# artifact, not the varied-fragment production path.) GPU *native* installs enable
# it via their hardware preset (nvidia / radeon / apple-silicon), where it fits.
# The forwarded preset (`.env`) now also ships off on CPU, so image ≡ deployment.
ENV AGENTALLOY_WEB_DIST=/app/web-dist \
    DUCKDB_PATH=/app/data/agentalloy.duck \
    FRAGMENTS_LANCE_PATH=/app/data/fragments.lance \
    TELEMETRY_DB_PATH=/app/data/telemetry.duck \
    CODE_INDEX_DATA_DIR=/app/data/code_index \
    LOG_LEVEL=INFO \
    LM_ASSIST=off \
    CODE_INDEX_REFRESH_SECONDS=300 \
    RUNTIME_EMBED_BASE_URL=http://localhost:47951 \
    RUNTIME_EMBEDDING_MODEL=nomic-embed-text-v1.5.Q8_0.gguf \
    SIGNAL_INTENT_BACKEND=reranker \
    SIGNAL_INTENT_RERANK_URL=http://127.0.0.1:47952 \
    SIGNAL_INTENT_RERANK_MODEL=Qwen3-Reranker-0.6B-Q8_0.gguf

EXPOSE 47950

# Conditional GGUF pre-bake for the "full" image variant.
# When PULL_MODEL=true, this layer downloads both GGUFs into the image under
# /app/data/models so air-gapped/enterprise deployments need no runtime
# download. The entrypoint skips the download whenever the files already exist,
# so a pre-baked image boots straight into the llama-servers.
ARG PULL_MODEL=false
RUN if [ "$PULL_MODEL" = "true" ]; then \
        echo "Pre-baking GGUF models into image (this may take several minutes)..." && \
        mkdir -p /app/data/models && \
        curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30 \
            -o /app/data/models/Qwen3-Embedding-0.6B-Q8_0.gguf \
            "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf" && \
        curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 30 \
            -o /app/data/models/Qwen3-Reranker-0.6B-Q8_0.gguf \
            "https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/resolve/main/qwen3-reranker-0.6b-q8_0.gguf" && \
        echo "GGUF models pre-baked successfully."; \
    else \
        echo "Skipping GGUF pre-bake (latest variant — models download on first boot)."; \
    fi

# Note: HEALTHCHECK is intentionally omitted — the container runtime module
# uses _wait_for_readiness() to poll /readiness with exponential backoff rather
# than relying on the OCI HEALTHCHECK directive (which Podman doesn't always
# honor in its default OCI image format).

# Default ENTRYPOINT/CMD: bare ``podman run ghcr.io/nrmeyers/agentalloy:latest``
# runs the baked bootstrap entrypoint, which seeds the prebuilt corpus (GHCR
# images), downloads the GGUF models if missing, starts both llama-servers,
# then starts uvicorn. The setup wizard bind-mounts a generated script on top
# of this default when packs are specified explicitly.
ENTRYPOINT ["/app/entrypoint.sh"]
