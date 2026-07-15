# OpenAI Responses passthrough surface

Status: **shipped** (this doc is the spec it was built from).
Motivating finding: the harness e2e matrix proved modern codex is
Responses-API-only — it ignores `OPENAI_BASE_URL`, `wire_api = "chat"` was
removed upstream, and a custom `model_providers` entry POSTs HTTP
`/v1/responses` to its `base_url`. Other OpenAI-SDK harnesses are drifting the
same way (OpenCode's built-in openai provider already did).

## Design: native passthrough, not translation

Mirror of the Anthropic Messages passthrough (`proxy_passthrough_router.py`),
not the translating chat-completions bridge:

- **No wire translation.** The request is forwarded verbatim (byte-identical
  except the injected block) to a Responses-capable upstream. Translating
  Responses↔ChatCompletions (input items, reasoning items, function-call
  items, a distinct SSE event protocol) would be a large, lossy shim; codex
  users' upstream (api.openai.com) already speaks Responses natively.
- **Auth-transparent.** The proxy holds no OpenAI credential; the caller's
  `Authorization` header is relayed unchanged (`forward_headers` denylist).
- **Soft-fail everywhere.** Any pre-forward failure (signal, compose, inject)
  forwards the ORIGINAL request unchanged — composition never blocks the
  proxy. Cadence markers commit only after a 2xx forward (`commit_outcome`
  via the `on_status` seam).

## Route

`POST /proj/{token}/v1/responses` — per-repo discriminator, same as the
Anthropic passthrough. No bare `/v1/responses` route: codex wiring is
repo-local via `CODEX_HOME` (hermes pattern), so every request carries a
token; a tokenless surface would reintroduce the repo-ambiguity the token
exists to solve.

## Upstream

`RESPONSES_UPSTREAM_URL` (default `https://api.openai.com`). Reuses
`AnthropicPassthroughClient` — the class is protocol-agnostic (it forwards
paths, headers, and bytes); only its name is historical.

## Injection

Responses requests carry `input`: either a string or a list of items, where a
user turn is `{"type": "message", "role": "user", "content": [{"type":
"input_text", "text": …}]}`. `inject_into_responses_input` mirrors
`inject_into_anthropic_messages`:

- inject into the LAST user message item (the top-level `instructions` field
  is codex's cached system prompt — byte-identical, never touched),
- same marker families (phase-stamped workflow block, once-per-session
  system block, strip-and-replace banner),
- string `input` gets the block appended as text; item-list `input` gets an
  appended `input_text` block on the last user message item,
- returns the SAME object on every no-op (identity = delivered, as on the
  Anthropic path).

Signal-layer mapping (`_proxy_request_from_responses`): input message items →
`ProxyMessage` list (`input_text` blocks → text), top-level `tools` array →
`ProxyRequest.tools` (the carrier gate needs it to tell a real agent turn from
a background micro-request).

## Codex wiring (consumer)

Repo-local `CODEX_HOME` (`<repo>/.codex/`), hermes pattern:

- `config.toml`: copy of the user's global `~/.codex/config.toml` (their
  tuning survives) with `model_provider = "agentalloy"` and
  `[model_providers.agentalloy]` → `base_url =
  http://localhost:<port>/proj/<token>/v1`, `wire_api = "responses"`,
  `env_key = "OPENAI_API_KEY"`.
- Auth is `env_key = "OPENAI_API_KEY"` only: codex reads the user's real key
  from env and the proxy forwards it transparently. The global `auth.json`
  (ChatGPT OAuth state) is **never copied** into the repo — no secrets leave
  `~/.codex/`.
- `.codex/.agentalloy-env` exports `CODEX_HOME`; `agentalloy wrap codex`
  injects it via env_builder for launch-time activation. A `.codex/.gitignore`
  (`*`) keeps codex session state out of git.

## Non-goals (for now)

- Translating Responses → chat-completions for upstreams that lack the
  Responses API. Point `RESPONSES_UPSTREAM_URL` at a Responses-capable server.
- The stateful Responses features (`previous_response_id`, `GET
  /v1/responses/{id}`) — codex sends `store: false`; stateful calls would
  need passthrough GET routes, added when a harness actually uses them.
