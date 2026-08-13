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

Default: `RESPONSES_UPSTREAM_URL` (default `https://api.openai.com`), built once
at lifespan startup into a lifespan-scoped `AnthropicPassthroughClient` — the
class is protocol-agnostic (it forwards paths, headers, and bytes); only its name
is historical.

Codex's target is **per-repo, per-harness**. `resolve_passthrough_client`
(`proxy_router.py`) reads only the `codex:` entry of `.agentalloy/upstream`
(captured by `agentalloy add codex --upstream-url`), strips its documented `/v1`
suffix (the file is written in the chat-completions shape; this surface's own
`/v1/responses` suffix would otherwise double to `/v1/v1/responses`), and
forwards there; absent a `codex:` entry it uses the default above. A chat
harness's upstream can never capture Codex. Clients are cached one per adopted
base URL on `app.state.responses_passthrough_client_cache`. `key_env` plays no
role on this surface — it stays auth-transparent, relaying the caller's own
credential; an override changes only the destination (fixes #505 — `add codex
--upstream-url` used to report an upstream this route never actually reached).

## Injection

Responses requests carry `input`: either a string or a list of items, where a
user turn is `{"type": "message", "role": "user", "content": [{"type":
"input_text", "text": …}]}`. `inject_into_responses_input` mirrors
`inject_into_anthropic_messages`:

- inject into the LAST user message item,
- same marker families (phase-stamped workflow block, once-per-session
  system block, strip-and-replace banner),
- string `input` gets the block appended as text; item-list `input` gets an
  appended `input_text` block on the last user message item,
- returns the SAME object on every no-op (identity = delivered, as on the
  Anthropic path).

### The `instructions` leg

`instructions` is codex's cached system prompt, and it is also where the SDD
phase prose goes — the highest-compliance location on this wire.
`inject_into_responses_instructions` is the sibling of
`inject_into_openai_system_prompt`: same phase-stamped workflow markers, same
idempotence, same strip-on-transition, but the target is an optional top-level
`str` rather than a message in an array. Absent / `None` / `""` is a real
injection (setting a scalar the harness left unset disturbs nothing, unlike
synthesizing a message into an array the harness owns); a non-`str` value is a
no-op. Identity-equals-delivered, as everywhere else on this surface.

This leg fires on **every carrier turn** — outside the `should_compose` guard,
outside `apply_signal` — and commits **no cadence marker**. A "delivered once"
record here would recreate the bug #499 fixed: codex rebuilds each request from
its own local history and never observes proxy mutations, so the prose would
vanish from turn 2 on. That failure mode is invisible on turn 1; the two-turn
tests in `tests/proxy/test_proxy_responses_native.py` are the ones that catch it.

`instructions` is therefore byte-identical **within a phase**, changing only on
a phase transition. There is no `cache_control`/ttl analog on this wire —
OpenAI caches prefixes implicitly with no knob, so the Anthropic breakpoint and
ttl-mirroring machinery has nothing to port.

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
