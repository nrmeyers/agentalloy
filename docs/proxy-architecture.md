# Proxy Architecture

AgentAlloy's FastAPI service acts as an OpenAI- and Anthropic-compatible proxy — a gateway that sits between the harness and the LLM, evaluating each call through the signal layer and injecting composed skill context before forwarding to the upstream LLM. The composition path is deterministic by default (optional off-by-default LM-assist).

## Overview

**Problem solved:** The previous tier model required three wiring mechanisms (per-turn hooks, per-session injection, sidecar file watcher) keyed to harness capabilities. The proxy is a single universal mechanism — any harness that supports a custom API base URL points at it and gets the full AgentAlloy experience.

- **What it is:** an OpenAI-compatible `/v1/chat/completions` and Anthropic-compatible `/v1/messages` endpoint that reads the request, evaluates the signal layer (phase, pre-filter, gates), composes skills if warranted, injects them into the system prompt, and forwards to the upstream LLM (response passed back unchanged).
- **What it is not:** middleware that parses responses or intercepts tool calls. It enhances the system prompt before the call and passes everything else through.

AgentAlloy's composition path is deterministic by default. Two optional LM-assist stages exist — a fragment re-ranker (`LM_ASSIST=arbitrate`) and a signals-layer intent backend (`SIGNAL_INTENT_BACKEND=reranker`) — both off by default, and both fail open to the deterministic path when the local model is unavailable.

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
   (Compose Engine is deterministic by default; an optional
    flag-gated LM re-ranker stage may run post-fusion —
    `LM_ASSIST=arbitrate`, off by default, fail-open.)
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
UPSTREAM_URL=http://localhost:11434
UPSTREAM_MODEL=qwen/qwen2.5-coder-14b
UPSTREAM_API_KEY=***
```

- `UPSTREAM_URL` — base URL of the LLM provider (OpenAI-compatible `/v1` endpoint)
- `UPSTREAM_MODEL` — model name to forward requests to
- `UPSTREAM_API_KEY` — API key for the upstream provider (optional for local runners)

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
| `POST` | `/v1/messages` | Anthropic Messages proxy — intercepts, composes, forwards. The primary Claude Code path (wired via `ANTHROPIC_BASE_URL`). |
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

Uses the existing compose engine (deterministic by default):
- Hybrid BM25 + dense retrieval from LadybugDB/DuckDB
- RRF fusion with phase-tuned leg weighting
- Applicability filter (deterministic predicates)
- Optional flag-gated LM fragment re-rank post-fusion (`LM_ASSIST=arbitrate`, off by default, fail-open)
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
agentalloy wire-harness --harness <name> --mcp-fallback
```

This installs an MCP server entry that exposes `get_skill_for(task, phase)` — effectively a manual compose call. The harness invokes it, gets skill context back, and uses it. No proxy involved.

MCP-fallback-compatible harnesses: claude-code, cursor, continue-closed, continue-local.

For the full proxy-wired and sidecar harness sets, see [Harness Classification](harness-classification.md) (the source of truth).

## History

The old three-tier model (hooks / per-session injection / sidecar) collapsed to a binary proxy-wired vs sidecar classification (see [Harness Classification](harness-classification.md)). The proxy is now the universal mechanism for interceptable harnesses; the file-watching sidecar remains for non-interceptable ones (cursor, windsurf, github-copilot, gemini-cli).

The hook routes are **kept and live** — Claude Code's default wiring is the hook path (`/v1/hook/user-prompt-submit`, `/v1/hook/pre-tool-use`, `/v1/hook/post-tool-use` in `api/hook_router.py`), which degrades gracefully if the service is down; `agentalloy wire --via proxy` switches it to base-URL proxy wiring. The embedding model (`qwen3-embedding:0.6b`), LadybugDB/DuckDB, signal layer, phase file, and contracts all carried over unchanged.

## Telemetry

Every proxy request writes a trace to DuckDB:
- `trace_id`, `request_ts`, `phase`, `upstream_model`, `signal_matched`, `composed`, `skills_injected`, `compose_ms`, `upstream_latency_ms`, `total_latency_ms`
- Passthrough requests (no composition) still traced — useful for understanding signal filter hit rates

## Security

- Upstream API keys are stored in config, never exposed to the harness
- The proxy runs on localhost only — no network exposure
- Working directory resolution is scoped to the user's projects
- No user data leaves the machine (embeddings run locally, composition is local)
