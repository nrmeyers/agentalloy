FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Rust for building the native extension
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

# Python deps first (cache layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir .

# Copy Rust source and build
COPY rust/ rust/
RUN cd rust && cargo build --release -p newagent-core && \
    cp target/release/lib_core.so /app/src/newagent/_core.cpython-311-x86_64-linux-gnu.so 2>/dev/null || \
    cp target/release/libnewagent_core.so /app/src/newagent/_core.cpython-311-x86_64-linux-gnu.so 2>/dev/null || true

# Copy Python source
COPY src/ src/
COPY tests/ tests/
COPY build/ build/
COPY runbook/ runbook/

# Install the package
RUN pip install --no-cache-dir -e .

# Default environment
ENV AGENTALLOY_SERVICE_PORT=48950
ENV AGENTALLOY_PROXY_PORT=48953
ENV AGENTALLOY_MODEL_PORT=50001
ENV AGENTALLOY_REPO_ROOT=/workspace

EXPOSE 48950 48953

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:48950/health || exit 1

# Default: start the server
CMD ["agentalloy", "serve", "--host", "0.0.0.0", "--port", "48950"]
