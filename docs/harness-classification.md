# Harness Classification

Source of truth for how coding-agent harnesses are classified. All documentation (README, operator.md, [install/harness-catalog.md](install/harness-catalog.md)) must align with this spec.

## Purpose

Classification determines which integration vector AgentAlloy uses for a given harness. With the proxy redesign, the operational distinction collapsed to a single binary question:

> **Can the harness's LLM traffic be intercepted by the AgentAlloy proxy?**

If yes — the harness honors an OpenAI / Anthropic / custom base-URL override — AgentAlloy installs a proxy-wiring config and gets per-turn skill injection on every request. If no, AgentAlloy writes a static rules file that the harness reads ambiently, and a file-watching sidecar keeps that file current.

## The Two Categories

### Proxy-wired

**Mechanism:** AgentAlloy writes harness-specific configuration that points the harness's LLM client at `http://localhost:<port>/v1`. The proxy intercepts every request, injects skill context, evaluates gates, and forwards to the real upstream (OpenAI, Anthropic, or a local runner).

**Key properties:**
- Per-turn context injection (proxy mutates each request payload)
- System skill gate enforcement is possible (proxy can refuse/modify requests)
- Phase transitions are picked up on the next request
- Semantic gate evaluation runs per-turn
- No sidecar required

**Current members:**

| Harness | Wiring Vector |
|---|---|
| `claude-code` | per-repo `.claude/settings.local.json` `env` (`ANTHROPIC_BASE_URL=…/proj/<token>`, auto-loaded by Claude Code) + `.agentalloy/claude-code-env.sh` shell/direnv fallback |
| `continue-closed`, `continue-local` | `.continuerc.json` `models[].apiBase` |
| `aider` | `.aider.conf.yml` (`openai-api-base`, `model`) |
| `hermes-agent` | repo-local `.hermes/config.yaml` (`model.base_url=…/proj/<token>/v1`) under `HERMES_HOME`, activated via `.hermes/.agentalloy-env` (+ direnv/mise carriers) + repo-scoped gateway restart |
| `opencode` | repo-local `opencode.json` — `provider.agentalloy` on `@ai-sdk/openai-compatible` (`baseURL=…/proj/<token>/v1`) + default model `agentalloy/agentalloy-proxy` |
| `cline` | `.cline/settings.json` (`apiProvider`, `apiBaseUrl`, `apiKey`, `model`) |
| `codex` | repo-local `.codex/config.toml` (`CODEX_HOME`, hermes pattern): `model_provider = "agentalloy"`, `[model_providers.agentalloy]` `base_url=…/proj/<token>/v1`, `wire_api = "responses"` → native Responses passthrough (`docs/responses-surface.md`) |
| `openclaw` | `~/.openclaw/openclaw.json` — `models.providers.agentalloy` custom provider (`api: openai-completions`, `baseUrl=…/v1`) + default model |
| `copilot-cli` | `.copilot/.agentalloy-env` (BYOK `COPILOT_PROVIDER_TYPE=openai`, `COPILOT_PROVIDER_BASE_URL=…/proj/<token>/v1`, `COPILOT_PROVIDER_API_KEY`, `COPILOT_MODEL`) — sourced or injected via `agentalloy wrap` |
| `github-copilot` | dual-carrier: VS Code user-profile `chatLanguageModels.json` (BYOK `customendpoint` group, `apiType: chat-completions`, full URL `…/v1/chat/completions`, agent-mode capable) **+** the `.github/copilot-instructions.md` sidecar block (ambient context; covers policy-disabled BYOK). ⚠️ Not machine-verifiable (no headless VS Code) — manual-smoke gated. |

### Sidecar

**Mechanism:** Harness cannot be proxy-wired (does not expose a base-URL override, or routes through its own backend). AgentAlloy writes a static rules file that the harness reads on its own. A file-watching sidecar detects changes to `.agentalloy/phase` and `.agentalloy/contracts/**` and rewrites the rules file within ~500ms (debounced).

**Key properties:**
- Context lives in a static file (not regenerated per turn)
- Sidecar rewrites the file on phase/contract changes
- System skills are advisory text only (no enforcement)
- Phase transitions are automatic only when sidecar is running
- Manual fallback: `agentalloy phase set <name>`

**Current members:**

| Harness | Reason | Rules File |
|---|---|---|
| `cursor` | Routes through Cursor's service; no first-party base-URL override | `.cursor/rules/agentalloy.mdc` (dedicated) or `.cursorrules` (shared) |
| `windsurf` | No first-party base-URL override | `.windsurf/rules/agentalloy.md` (dedicated) or `.windsurfrules` (shared) |
| `github-copilot` (markdown half) | Copilot-billed model traffic routes through GitHub's backend; the instructions file is the ambient channel and the fallback when org policy disables BYOK. The BYOK carrier above provides the proxy half. | `.github/copilot-instructions.md` (shared, marker-bounded) |
| `antigravity` (alias `gemini-cli`) | Antigravity CLI (formerly Gemini CLI). Talks to Google's Gemini API; ignores `OPENAI_*` / `ANTHROPIC_*` env vars | `GEMINI.md` (shared, marker-bounded) |

### Non-Classified

Harnesses that integrate with AgentAlloy but don't fit either category:

- `manual` — emits sentinel-bounded markdown to stdout for copy-paste
- `mcp-only` — legacy entry, no longer accepted standalone; use `--mcp-fallback` with a real harness

## Capability Matrix

| Capability | Proxy-wired | Sidecar |
|---|---|---|
| Initial workflow skill context | ✅ | ✅ |
| Phase transition detection | ✅ Per-turn (proxy) | ✅ Automatic when sidecar running; manual fallback otherwise |
| System skill enforcement | ✅ Proxy can block/modify requests | ⚠️ Advisory text only |
| Mid-session context updates | ✅ Injected every turn | ⚠️ File reload (harness-dependent) |
| Contract → skill injection | ✅ Per-turn (proxy) | ✅ Sidecar regenerates |
| Semantic gate evaluation | ✅ Runs per-turn | ⚠️ Falls back to UNKNOWN |

## Classification Rule

When a new harness is added, classify with one question:

1. **Does the harness honor a custom API base URL** (`OPENAI_BASE_URL` / `OPENAI_API_BASE` / `ANTHROPIC_BASE_URL` / a config-file `apiBase` field) **that can be pointed at `http://localhost:<port>/v1`?** → **proxy-wired**
2. **Otherwise** → **sidecar**

If the harness has *some* programmatic surface (a config file or a CLI flag) that AgentAlloy can write to but does not actually route LLM traffic through the proxy (e.g., `_wire_proxy_instruction` just writes an instruction file), it's still classified as **sidecar** for capability purposes — its LLM calls are not intercepted.

## History

- **Original design:** 3-tier model (hooks / per-session / file-only).
- **Tier 2 collapse:** Per-session injection (Continue.dev, OpenCode, Hermes Agent) was always a workaround for harnesses without per-turn hooks. The proxy redesign made it obsolete — those harnesses now route through the proxy and get true per-turn injection.
- **Tier 1 / Tier 3 collapse:** The proxy redesign made per-turn hook capability irrelevant — the proxy intercepts every turn regardless of whether the harness has a hook API. The remaining distinction is purely whether traffic can be intercepted at all.
- **Hook transport removed:** Claude Code originally used per-turn hooks (`UserPromptSubmit` / `PreToolUse` / `PostToolUse`). Once the auth spike confirmed account/OAuth credentials survive the proxy, the hook transport was removed entirely — Claude Code is now proxy-wired like every other interceptable harness. The only categories left are **proxy-wired** and **sidecar**.
- **Cline + Aider moved out of sidecar set:** Both got real proxy wiring (Cline via `.cline/settings.json`, Aider via `.aider.conf.yml`).
