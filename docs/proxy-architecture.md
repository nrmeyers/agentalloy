# Proxy Architecture

AgentAlloy's FastAPI service acts as an OpenAI-compatible proxy — a gateway that sits between the harness and the LLM, evaluating each call through the deterministic signal layer and injecting composed skill context before forwarding to the upstream LLM.

## Overview

**Problem solved:** Previous architecture required three different wiring mechanisms (per-turn hooks, per-session injection, sidecar file watcher) depending on harness capabilities. The tier model existed because some harnesses had hooks and others didn't.

**Solution:** The proxy is a single universal mechanism. Any harness that supports custom API endpoints points to the proxy, and gets the full AgentAlloy experience automatically. No hooks needed. No sidecar. No per-harness wiring logic.

**What it is:** An OpenAI-compatible `/v1/chat/completions` endpoint that:
1. Reads the incoming request (system prompt, messages, model)
2. Evaluates the signal layer (phase, pre-filter, gates)
3. Composes relevant skills if warranted
4. Injects composed skills into the system message
5. Forwards the modified request to the upstream LLM
6. Passes the response back unchanged

**What it is not:** A full middleware that parses LLM responses or intercepts tool calls. It enhances the system prompt before the call and passes everything else through.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Harness (Claude Code, Cursor, Continue, OpenCode, etc.)    │
│                                                              │
│  Sends: POST /v1/chat/completions                            │
│  (OpenAI-compatible format)                                  │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  AgentAlloy Proxy (:47950)                                   │
│                                                              │
│  1. Extract working directory from request                   │
│  2. Read .agentalloy/phase from disk                         │
│  3. Signal layer: pre-filter + gate evaluation               │
│  4. If signal matches → compose skills via /compose          │
│  5. Inject composed skills into system message               │
│  6. Forward to upstream LLM                                  │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │  Signal      │  │  Compose     │  │  Embedding       │   │
│  │  Layer       │→ │  Engine      │→ │  Model           │   │
│  │  (determin-  │  │  (BM25+      │  │  (qwen3-         │   │
│  │   istic)     │  │   dense+RRF) │  │   embedding      │   │
│  │              │  │              │  │   :0.6b)         │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐                         │
│  │  LadybugDB   │  │  DuckDB      │                         │
│  │  (Kuzu graph)│  │  (vectors+   │                         │
│  │  (skill/     │  │   FTS+       │                         │
│  │   version/   │  │   traces)    │                         │
│  │   fragment)  │  │              │                         │
│  └──────────────┘  └──────────────┘                         │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  Upstream LLM                                                │
│                                                              │
│  OpenAI, Anthropic, local runner (Ollama, LM Studio, etc.)   │
│  Receives: original request + AgentAlloy system prompt        │
│  Response: passed back to harness unchanged                   │
└──────────────────────────────────────────────────────────────┘
```

## Configuration

### Upstream LLM

Configured in `~/.config/agentalloy/.env`:

```
AGENTALLOY_UPSTREAM_URL=http://localhost:2099/v1
AGENTALLOY_UPSTREAM_MODEL=qwen/qwen2.5-coder-14b
AGENTALLOY_UPSTREAM_API_KEY=sk-xxx
```

- `AGENTALLOY_UPSTREAM_URL` — base URL of the LLM provider (OpenAI-compatible `/v1` endpoint)
- `AGENTALLOY_UPSTREAM_MODEL` — model name to forward requests to
- `AGENTALLOY_UPSTREAM_API_KEY` — API key for the upstream provider (optional for local runners)

These are set during `agentalloy setup` and read by the proxy at startup. The harness never sees these values — it only talks to localhost:47950.

### Working Directory

The proxy determines the working directory to read `.agentalloy/phase` from. Priority:

1. `cwd` field in the request (if the harness sends it)
2. `cwd` from the process environment (`AGENTALLOY_PROJECT_DIR`)
3. Current working directory of the proxy process

For per-repo resolution, the proxy reads `.agentalloy/phase` from the determined working directory. If no phase file exists, the proxy passes the request through unchanged.

### Profile Resolution

Same as existing: resolved per-repo via git remote URL, path prefix, or explicit project marker. The proxy uses the active profile to determine which datastore and skill overrides to use for composition.

## API Endpoints

### Proxy Endpoint

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible proxy — intercepts, composes, forwards |
| `POST` | `/v1/embeddings` | Forward to embed server (passthrough) |

The proxy endpoint accepts standard OpenAI chat completion format:

```json
{
  "model": "any-model-name",
  "messages": [
    {"role": "system", "content": "existing system prompt"},
    {"role": "user", "content": "user message"},
    ...
  ],
  "temperature": 0.7,
  "stream": true
}
```

Response format: identical to the upstream LLM's response (stream or non-stream).

### Existing Endpoints (unchanged)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/compose` | Manual composition (standalone) |
| `POST` | `/compose/text` | Manual composition, plain text |
| `POST` | `/retrieve` | Manual retrieval only |
| `GET` | `/retrieve/{skill_id}` | Lookup single skill's fragments |
| `GET` | `/skills/{skill_id}` | Inspect skill metadata |
| `GET` | `/telemetry/traces` | Query composition traces |
| `GET` | `/health` | Liveness probe |
| `GET` | `/diagnostics/runtime` | Backend/model/DB state |

## Signal Layer Integration

### Flow

1. **Request arrives** — proxy extracts system prompt, messages, and working directory
2. **Phase check** — reads `.agentalloy/phase`. If no phase file exists, skip to passthrough
3. **Pre-filter** — runs signal keywords against the user message. If no match, skip to passthrough
4. **Gate evaluation** — evaluates exit gates (deterministic predicates + cosine similarity)
5. **Compose** — if gates match, runs composition via existing `/compose` logic
6. **Inject** — appends composed skills to the system message
7. **Forward** — sends modified request to upstream LLM

### Passthrough

When the signal layer finds no match, the proxy forwards the request unchanged to the upstream LLM. No tokens spent, no delay added. This is the common case — most turns don't trigger composition.

### Composition

Uses the existing compose engine:
- Hybrid BM25 + dense retrieval from LadybugDB/DuckDB
- RRF fusion with phase-tuned leg weighting
- Applicability filter (deterministic predicates)
- Diversity selection (top-k with diversity constraint)
- Assembly into prose output

### Injection

Composed skills are appended to the system message with a marker block:

```
<!-- BEGIN AGENTALLOY-CONTEXT -->
<composed skill prose from /compose>
<!-- END AGENTALLOY-CONTEXT -->
```

If the system message already contains this block, it is replaced rather than duplicated. This ensures idempotent injection across multiple turns.

## Conversation State

The proxy maintains minimal state:
- **Current phase** — read from `.agentalloy/phase` on each request
- **Active profile** — resolved per-request based on working directory
- **Composition cache** — recent compositions cached to avoid re-composing identical requests

The proxy does NOT maintain:
- Message history (passed through unchanged)
- Token counts (passed through unchanged)
- Session state (stateless between requests)

## Wiring

### Universal Wiring

Harnesses that support custom API endpoints wire to the proxy by changing their LLM configuration to point to `http://localhost:47950/v1`. The harness's own client appends the endpoint path (e.g., `/chat/completions`) to this base URL.

```bash
agentalloy wire
```

This replaces the previous per-harness wiring logic. The command:
1. Detects the harness in the current directory
2. Writes the proxy URL into the harness's LLM configuration
3. Installs a minimal `.agentalloy/phase` file if one doesn't exist

### MCP Fallback

For harnesses that support MCP tools but not custom API endpoints:

```bash
agentalloy wire --mcp-fallback
```

This installs an MCP server entry that exposes `get_skill_for(task, phase)` — effectively a manual compose call. The harness invokes it, gets skill context back, and uses it. No proxy involved.

Supported harnesses: claude-code, cursor, continue-closed, continue-local.

## Migration from Tier Model

The three-tier model (hooks, session injection, sidecar) is replaced by the proxy. Existing components affected:

| Component | Status |
|-----------|--------|
| Hook scripts (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`) | Deprecated — removed in proxy redesign |
| Sidecar / file watcher | Kept — still the only option for non-interceptable harnesses (cursor, windsurf, github-copilot, gemini-cli). Marked with deprecation warning but functional. |
| Per-harness wiring (`wire_harness.py`) | Kept — proxy wiring for interceptable harnesses; legacy wiring + sidecar for the rest |
| Tier classification | Deprecated — replaced by binary proxy-wired vs sidecar classification |
| `/compose` endpoint | Kept — standalone manual composition |
| MCP fallback | Kept — for harnesses without custom API support |
| Embedding model | Kept — still `qwen3-embedding:0.6b` |
| LadybugDB / DuckDB | Kept — unchanged |
| Signal layer | Kept — runs inside proxy instead of hooks |
| Phase file (`.agentalloy/phase`) | Kept — proxy reads it |
| Contracts (`.agentalloy/contracts/`) | Kept — proxy reads them |

## Telemetry

Every proxy request writes a trace to DuckDB:
- `trace_id`, `request_ts`, `phase`, `upstream_model`, `signal_matched`, `composed`, `skills_injected`, `compose_ms`, `upstream_latency_ms`, `total_latency_ms`
- Passthrough requests (no composition) still traced — useful for understanding signal filter hit rates

## Security

- Upstream API keys are stored in config, never exposed to the harness
- The proxy runs on localhost only — no network exposure
- Working directory resolution is scoped to the user's projects
- No user data leaves the machine (embeddings run locally, composition is local)
