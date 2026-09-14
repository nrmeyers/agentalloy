# AgentAlloy v2.0 — Server Launch Runbook

Named M1 deliverable (design §22). Exact commands to start the LFM2.5 model stack on dev ports.

## Model Locations

All GGUFs in `~/.local/share/agentalloy/models/`:
- `LFM2.5-2.6B-QAD-Q4_0.gguf` — driver (interpreter, :50001, shared with v1)
- `LFM2.5-Embedding-350M-Q8_0.gguf` — embedding model (:48951)
- `LFM2.5-ColBERT-350M-Q8_0.gguf` — ColBERT rerank model (:48952, **see §9 decision below**)

Source: official LiquidAI HuggingFace repos (downloaded 2026-09-03):
- `LiquidAI/LFM2.5-Embedding-350M-GGUF` → `LFM2.5-Embedding-350M-Q8_0.gguf`
- `LiquidAI/LFM2.5-ColBERT-350M-GGUF` → `LFM2.5-ColBERT-350M-Q8_0.gguf`

## Embedding Server (:48951) — WORKING

```bash
llama-server \
  --model ~/.local/share/agentalloy/models/LFM2.5-Embedding-350M-Q8_0.gguf \
  --port 48951 \
  --embedding \
  --pooling cls \
  --cont-batching \
  --n-gpu-layers 999
```

**Verified:** Returns 1024-dim vectors. Design §9 specifies MRL truncation to 768 + L2-renorm in the Rust crate (T3).

## ColBERT Rerank Server (:48952) — NOT WORKING (2026-09-03)

```bash
# DOES NOT WORK — crashes with CUDA error during initialization
llama-server \
  --model ~/.local/share/agentalloy/models/LFM2.5-ColBERT-350M-Q8_0.gguf \
  --port 48952 \
  --embedding \
  --pooling none \
  --cont-batching \
  --n-gpu-layers 999
```

**Issue:** llama-server crashes with `ggml_cuda_error` when loading the ColBERT model with `--pooling none`. Tried:
- `--n-gpu-layers 999` (force all to GPU) → crash
- `--n-gpu-layers 0` (CPU-only) → crash
- Without `--max-batch-size` (invalid arg) → crash

**Root cause:** Likely incompatibility between this llama-server build and the ColBERT model's pooling requirements. The `--pooling none` flag is supposed to emit per-token vectors (seq×dim) for ColBERT MaxSim, but the server crashes before serving.

## §9 Decision (recorded 2026-09-03)

**Fallback B: M1 ships dense + BM25 only. ColBERT MaxSim deferred to M1.x.**

- **M1 (this build):** Dense leg (1024→768 truncation + L2-renorm + HNSW) + lexical leg (tantivy BM25) + RRF fusion. AC-6/AC-14 green.
- **M1.x (follow-up):** Investigate llama-server ColBERT support (may need different build/version, or in-crate llama.cpp binding for per-token vectors). If resolved, add ColBERT MaxSim as second-stage rerank (design Fallback-A).

**Rationale:** ColBERT per-token vectors are the #1 technical risk (design §23 risk 1). Rather than block M1 on resolving it, we ship the dense+BM25 baseline (which still satisfies AC-6/AC-14) and defer ColBERT to M1.x once the server issue is resolved.

## Port Strategy

Dev ports (this build):
- :48950 — newagent service (FastAPI, T9)
- :48951 — embedding server (this runbook)
- :48952 — rerank server (this runbook, currently non-functional)
- :50001 — driver LFM2.5-2.6B (shared with v1, already running)

Final flip (after M1 ACs pass):
- :48950 — newagent service (drop-in replacement for v1)
- :48951 — embedding server
- :48952 — rerank server
- v1 decommissioned

## Health Checks

```bash
curl http://localhost:48951/health  # {"status":"ok"}
curl http://localhost:48952/health  # (currently fails)
curl http://localhost:50001/health  # (v1's driver, should be running)
```

## AgentAlloy v2.0 Quick Start

### Start the service
```bash
agentalloy serve          # REST API on :48950
agentalloy proxy          # Steering proxy on :48953
agentalloy mcp            # MCP server (stdio)
```

### CLI commands
```bash
agentalloy status                     # Show ports and config
agentalloy ask "search for auth code" # Ask the interpreter
agentalloy index --repo .             # Build code index
agentalloy harness setup --type qwen-code --mode dual
agentalloy sessions list              # List workflow sessions
agentalloy sessions create my-task    # Create a session
agentalloy sessions stash my-task     # Stash (snapshot state)
agentalloy sessions resume my-task    # Resume from stash
agentalloy sessions status my-task    # Show session detail
```

### API endpoints
```bash
# Core
GET  /health              # Health check
GET  /status              # Phase, symbols, chunks, skills
POST /chat                # Interpreter (non-streaming)
POST /chat/stream         # Interpreter (SSE streaming)

# State
GET  /gates               # Approval gate status
GET  /sessions            # List sessions
GET  /sessions/{key}      # Session detail
POST /sessions/{key}/stash    # Stash session
POST /sessions/{key}/resume   # Resume session

# Analytics
GET  /analytics           # Tool usage, error rates, phase activity
GET  /lessons             # QA lessons (compound engineering)
GET  /usage               # Token usage summary
GET  /usage/history       # Per-request history

# UI
GET  /dashboard           # Web dashboard
```

### Docker deployment
```bash
docker compose up -d      # Start AgentAlloy service
docker compose logs -f    # Follow logs
```

### Environment variables
```bash
V2_SERVICE_PORT=48950     # REST API port
V2_PROXY_PORT=48953       # Steering proxy port
V2_MODEL_PORT=50001       # Upstream model port
V2_UPSTREAM_URL=http://localhost:50001
V2_UPSTREAM_KEY=sk-local-...
V2_REPO_ROOT=.            # Primary repo to index
V2_EXTRA_REPOS=           # Comma-separated additional repos
V2_INDEX_DIR=./index      # Index storage
V2_STATE_DUCK=./state.duck  # State database
```
