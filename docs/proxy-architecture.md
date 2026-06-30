# Proxy Architecture

AgentAlloy's FastAPI service acts as an OpenAI- and Anthropic-compatible proxy — a gateway that sits between the harness and the LLM, evaluating each call through the signal layer and injecting composed skill context before forwarding to the upstream LLM. The composition path is deterministic by default (optional off-by-default LM-assist).

## Overview

**Problem solved:** The previous tier model required three wiring mechanisms (per-turn hooks, per-session injection, sidecar file watcher) keyed to harness capabilities. The proxy is a single universal mechanism — any harness that supports a custom API base URL points at it and gets the full AgentAlloy experience.

- **What it is:** an OpenAI-compatible `/v1/chat/completions` and native Anthropic `/proj/{token}/v1/messages` passthrough endpoint that reads the request, evaluates the signal layer (phase, pre-filter, gates), composes skills if warranted, injects them, and forwards to the upstream LLM (response passed back unchanged).
- **What it is not:** middleware that parses responses or intercepts tool calls. It enhances the request before the call and passes everything else through.

The Anthropic surface is a single native passthrough (the bare `/v1/messages` Anthropic→OpenAI translation shim was removed — see [proxy-surfaces.md](proxy-surfaces.md)):

- **Native Anthropic passthrough** (`POST /proj/{token}/v1/messages`) — the primary Claude Code path. Composes and injects AgentAlloy context into the **last user message**, leaves the top-level `system` block byte-unchanged (so prompt caching survives), then forwards the request **verbatim** to a configurable Anthropic upstream and relays the response (raw SSE byte relay when streaming). **No Anthropic↔OpenAI translation.** See [Native Anthropic Passthrough](#native-anthropic-passthrough) below.

AgentAlloy's composition path is deterministic by default. Two small-local-model stages sit alongside it, both fail-open to the deterministic path when the model is unavailable: the **signals-layer intent backend** (`SIGNAL_INTENT_BACKEND`, default `reranker` — a measured win, so it ships on; `cosine` opts out and is the fail-open floor) and the **composition fragment re-ranker** (`LM_ASSIST`, `arbitrate` on the GPU presets and `off` on cpu/container — scoring the candidate fragments only fits the latency budget on a GPU reranker). See BENCHMARKS.md and [lm-assist-design.md](lm-assist-design.md) for the numbers behind each default.

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
│  │  (determin-  │  │  (BM25+      │  │  (nomic-embed-   │   │
│  │   istic)     │  │   dense+RRF) │  │   text-v1.5      │   │
│  │              │  │              │  │   .Q8_0.gguf,    │   │
│  │              │  │              │  │   llama-server)  │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐                         │
│  │  DuckDB      │  │  LanceDB     │                         │
│  │  (skill      │  │  (vectors +  │                         │
│  │   graph:     │  │   Tantivy    │                         │
│  │   skill/ver/ │  │   BM25)      │                         │
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
│  OpenAI, Anthropic, or any local LLM (e.g. llama-server)     │
│  Receives: original request + AgentAlloy system prompt        │
│  Response: passed back to harness unchanged                   │
└──────────────────────────────────────────────────────────────┘
```

## Configuration

### Upstream LLM

Configured in `~/.config/agentalloy/.env`:

```
UPSTREAM_URL=http://localhost:8080/v1
UPSTREAM_MODEL=your-model-name
UPSTREAM_API_KEY=***
```

- `UPSTREAM_URL` — base URL of the generative LLM provider (OpenAI-compatible `/v1` endpoint) the proxy forwards chat completions to. No default (empty until configured); point it at your model runner or a hosted provider — **not** the embedding server on `47951`. The example above is a local OpenAI-compatible runner.
- `UPSTREAM_MODEL` — model name to forward requests to
- `UPSTREAM_API_KEY` — API key for the upstream provider (optional for local runners)

These `.env` values are the **global fallback**. A per-repo upstream captured by `agentalloy add <harness>` is written to that repo's `.agentalloy/upstream` and **wins** over them for requests from that repo — so one machine can forward different repos to different models. These are set during `agentalloy setup` (or per repo via `agentalloy add`) and read by the proxy at startup. The harness never sees any of these values — it only talks to `localhost:47950`.

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
| `POST` | `/proj/{token}/v1/messages` | **Native Anthropic passthrough** — the primary Claude Code path (wired via `ANTHROPIC_BASE_URL`). `{token}` is a per-repo discriminator (see below). Composes, injects into the last user message, forwards verbatim to the Anthropic upstream. No translation. |
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

## Native Anthropic Passthrough

The native passthrough path runs the same signal/compose pipeline as the other proxy endpoints, but forwards Anthropic Messages requests to an Anthropic upstream **verbatim** — no Anthropic↔OpenAI translation. It is the path Claude Code uses.

```
Claude Code ──(ANTHROPIC_BASE_URL=…/proj/<token>)──▶ proxy /proj/{token}/v1/messages
                                                     │
            decode {token} → project_dir            │
            read phase + lifecycle_mode (that repo) │
            evaluate signal → compose + inject      │  inject into LAST user
            into Anthropic messages                 │  message; system block
                                                     │  left byte-unchanged
            forward VERBATIM Anthropic JSON ────────▶ Anthropic upstream
            (caller's own auth header passed through) │  (ANTHROPIC_UPSTREAM_URL)
            relay response bytes (stream = raw SSE) ◀─┘
```

### The `/proj/<token>` discriminator

The route carries a per-repo discriminator: `token = base64url(realpath(project_dir))`, embedded in the wired base URL as `ANTHROPIC_BASE_URL=http://localhost:{port}/proj/<token>`. base64url avoids nested-slash and percent-encoding hazards. The proxy decodes the token to the project dir and feeds it as the highest-precedence source into working-dir resolution, recovering that repo's phase and `lifecycle_mode` straight from the URL.

This adds **zero new server state**: resolution is stateless and restart-safe, because the repo identity travels in the request URL rather than in proxy memory or a shared file. The query string is preserved (Claude Code calls `/v1/messages?beta=true`, so the wired path is `/proj/<token>/v1/messages?beta=true`). A `/r/<key>` namespace is reserved for a future key+registry upgrade.

### Injection target — last user message, system untouched

Unlike the OpenAI path (which appends to the system message), the passthrough path injects the composed block into the **last `role:"user"` message**, leaving the top-level `system` block byte-unchanged. Claude Code prompt-caches the system block (active `prompt-caching-scope` / `extended-cache-ttl` betas); mutating that cached prefix would bust the cache every turn, so injection deliberately avoids it. If the user `content` is a string the marker block is appended; if it is a block array, a text block is appended (or a stale one replaced). Markers are phase-stamped: `<!-- BEGIN AGENTALLOY-CONTEXT phase=<p> -->`.

Cadence is **durable, not stateless**. Claude Code never echoes an injected marker back into the next request, so scanning message history for one was structurally dead — it never matched, and the orientation block re-injected every turn (intake worst of all, since it bypassed the trigger entirely). Cadence now lives in `.agentalloy/announced`, which records the last phase whose orientation was emitted:

- **Entry announce** — when `announced != phase` (fresh wire, or a transition advanced the phase), the orchestrator orientation block is injected once and `announced` is set to the phase. Subsequent turns in the same phase match and stay quiet.
- **Transition eval** — every turn (all phases, no bypass) the reranker transition trigger runs; when it fires and the exit gate yields an advisory, a light `[agentalloy-eval]` block is injected. A clean transition (gate met, no advisory) advances the phase but injects nothing that turn — the new phase announces on the next turn.
- **Neither** — steady-state turn: forwarded unchanged.

The request-level injector remains idempotent within a single payload (a current-phase marker already present short-circuits a second injection), which is independent of the cross-turn `announced` state. The active phase is also surfaced as standing state every turn via the Claude Code status line (`agentalloy statusline` → `⚙ agentalloy ▸ <phase>`) and the managed `.claude/CLAUDE.md` phase-protocol block, so the agent stays oriented even on the quiet turns. System/always-apply uses a distinct `AGENTALLOY-SYSTEM` marker injected once per session.

### Auth transparency

Wiring sets **only** `ANTHROPIC_BASE_URL` and **never** `ANTHROPIC_API_KEY`. Setting any API key risks forcing Claude Code into API-key mode and breaking account auth, so it is deliberately omitted. The proxy holds **no** Anthropic credential of its own; it forwards the caller's credential verbatim. Header handling is a **denylist, not an allowlist**: every inbound header is forwarded except hop-by-hop (`connection`, `keep-alive`, `transfer-encoding`, `te`, `upgrade`, `proxy-*`) and internal/routing headers, with `Host` rewritten for the upstream. `authorization`, `x-api-key`, `anthropic-beta`, and `x-claude-code-session-id` are always preserved — an allowlist would drop load-bearing headers (e.g. the `anthropic-beta` oauth/caching/thinking flags). This keeps account/OAuth auth (Pro/Max/Team users, who have no API key) working unchanged.

#### Auth spike result

Confirmed from live traffic: an account-authenticated Claude Code (OAuth, `anthropic-beta: …oauth-2025-04-20…`, no `x-api-key`) attaches its credential to a custom `ANTHROPIC_BASE_URL`, and a passthrough proxy forwards it to `api.anthropic.com` successfully. Proxy-only is therefore viable for everyone, including account users. On that basis the hook path has since been **removed entirely** — the proxy is the sole transport for Claude Code.

### Configurable upstream

The upstream target is `ANTHROPIC_UPSTREAM_URL` (default `https://api.anthropic.com`). Because it is configurable, the proxy can be chained — e.g. Claude Code → AgentAlloy → another proxy → Anthropic — so a user who already occupies `ANTHROPIC_BASE_URL` with another passthrough proxy can keep both.

### Streaming

Streaming (`stream == true`) is a **raw byte relay**: the upstream httpx stream is piped straight into a FastAPI `StreamingResponse` with `content-type: text/event-stream` preserved and no re-parsing. Read timeouts are generous (Claude turns run minutes); the connect timeout is short.

### Soft-fail

Any error in the pre-forward stage (resolve / compose / inject) forwards the **original** payload unchanged. The whole pre-forward stage is wrapped in a single guard, so composition can never block the request — a passthrough failure degrades to plain Anthropic behavior rather than an error.

## Signal Layer Integration

### Flow

1. **Request arrives** — proxy extracts system prompt, messages, and working directory
2. **Lifecycle + phase check** — reads `.agentalloy/config` (mode) and `.agentalloy/phase`. Non-`full` mode or no phase file → passthrough
3. **Announce decision** — compares `.agentalloy/phase` against `.agentalloy/announced`. A mismatch marks this as an *entry* turn (announce the phase once)
4. **Transition trigger** — runs the reranker-primary intent classifier (deterministic floor) for *every* phase, including intake. No bypass
5. **Gate evaluation** — when the trigger fires, evaluates exit gates (deterministic predicates + cosine similarity); a met gate advances the phase, an unmet one yields an advisory
6. **Compose** — entry turn → orchestrator orientation block; advisory present → light `[agentalloy-eval]` block. Neither → nothing
7. **Inject** — entry announce records `announced = phase`; the block lands in the last user message
8. **Forward** — sends the (possibly unchanged) request to upstream LLM

### Passthrough

When the signal layer finds no match, the proxy forwards the request unchanged to the upstream LLM. No tokens spent, no delay added. This is the common case — most turns don't trigger composition.

### Composition

Uses the existing compose engine (deterministic by default):
- Hybrid BM25 + dense retrieval from DuckDB/LanceDB
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

This describes the OpenAI path. The native Anthropic passthrough path injects into the **last user message** instead (system block untouched) — see [Native Anthropic Passthrough](#native-anthropic-passthrough).

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

There is **no hook transport**. Claude Code is **proxy-wired** via the native Anthropic passthrough at `/proj/<token>/v1/messages` (`ANTHROPIC_BASE_URL`); the per-turn hook routes have been removed. The embedding model (`nomic-embed-text-v1.5.Q8_0.gguf`, served by llama-server with `--embeddings --pooling mean --ctx-size 2048 --ubatch-size 2048` on port 47951, queries prefixed `search_query: ` and documents `search_document: `), DuckDB/LanceDB, signal layer, phase file, and contracts all carried over unchanged.

## Telemetry

Every proxy request writes a trace to DuckDB:
- `trace_id`, `request_ts`, `phase`, `task_prompt`, `status`, `assembly_tier`, `assembly_model`, `source_skill_ids`, `system_skill_ids`, `retrieval_latency_ms`, `assembly_latency_ms`, `total_latency_ms`, `session_key`, `repo` (full schema: `CompositionTrace` in `storage/vector_store.py`)
- Passthrough requests (no composition) still traced — useful for understanding signal filter hit rates

## Security

- Upstream API keys are stored in config, never exposed to the harness
- The proxy runs on localhost only — no network exposure
- Working directory resolution is scoped to the user's projects
- No user data leaves the machine (embeddings run locally, composition is local)
