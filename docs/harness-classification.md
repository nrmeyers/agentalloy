# Harness Classification

Source of truth for how coding-agent harnesses are classified. All documentation (README, operator.md, harness-catalog.md) must align with this spec.

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
| `claude-code` | `~/.agentalloy/claude-code-env.sh` (`ANTHROPIC_BASE_URL`) |
| `continue-closed`, `continue-local` | `.continuerc.json` `models[].apiBase` |
| `aider` | `.aider.conf.yml` (`openai-api-base`, `model`) |
| `hermes-agent` | `~/.hermes/config.yaml` `custom_providers` (user scope) or `AGENTS.md` instruction (repo scope) |
| `opencode` | `.opencode/.agentalloy-env` (`OPENAI_API_BASE`) + sentinel block in `system-prompt.md` |
| `cline` | `.cline/settings.json` (`apiProvider`, `apiBaseUrl`, `apiKey`, `model`) |

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
| `cursor` | Routes through Cursor's service; no first-party base-URL override | `.cursor/rules/agentalloy-context.mdc` (dedicated) or `.cursorrules` (shared) |
| `windsurf` | No first-party base-URL override | `.windsurf/rules/agentalloy.md` (dedicated) or `.windsurfrules` (shared) |
| `github-copilot` | Closed routing through GitHub backend | `.github/copilot-instructions.md` (shared, marker-bounded) |
| `gemini-cli` | Talks to Google's Gemini API; ignores `OPENAI_*` / `ANTHROPIC_*` env vars | `GEMINI.md` (shared, marker-bounded) |

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
- **Tier 1 / Tier 3 collapse:** The proxy redesign (PR #4, PR #8) made per-turn hook capability irrelevant — the proxy intercepts every turn regardless of whether the harness has a hook API. The remaining distinction is purely whether traffic can be intercepted at all.
- **Cline + Aider moved out of sidecar set:** Both got real proxy wiring in PR #8 (Cline via `.cline/settings.json`, Aider via `.aider.conf.yml`). Their watcher regenerators remain only for users running `agentalloy wire --legacy`.
