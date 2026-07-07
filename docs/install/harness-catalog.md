# Harness Catalog

Complete reference for all coding-agent harnesses supported by AgentAlloy, including target files, integration vectors, tier classification, and auto-detection markers.

## Proxy Wiring (Default)

AgentAlloy's default wiring mode is **proxy wiring**: instead of injecting markdown
instructions into a harness's config, AgentAlloy writes the harness's API endpoint to
point at the local AgentAlloy proxy server. All requests flow through the proxy, which
handles phase detection, skill composition, and system message injection transparently.

Every proxy-wired harness is configured to use the synthetic model name
`agentalloy-proxy`, which the proxy resolves to the user's configured upstream model
(via `_resolve_model()` in `proxy_router.py`).

Harnesses that cannot be configured natively (cursor, windsurf, antigravity,
github-copilot) receive a proxy instruction block explaining the proxy is active.

### Wiring modes

`agentalloy wire` always uses proxy wiring (its flags are `--harness`, `--port`,
`--via proxy`, `--force`, `--lifecycle-mode`, `--clean-room`, `--list`, `--json`). The
`--legacy` and `--mcp-fallback` flags below live on **`agentalloy wire-harness`**, not on
`wire`. `--harness` is repeatable and comma-tolerant
(`--harness claude-code --harness hermes-agent` or `--harness claude-code,hermes-agent`);
`wire --list` prints the harnesses currently wired in the cwd repo.

| `wire-harness` flag | Behavior |
|------|----------|
| (default) | Proxy wiring — writes native API endpoint config |
| `--legacy` | Legacy markdown-injection — writes static rules files (old behavior) |
| `--mcp-fallback` | MCP server config — writes stdio MCP server entry |

### Proxy-wired harnesses

These harnesses have native proxy wiring via `_wire_proxy_*()` functions:

| Harness | Config File | Fields Written | Phase |
|---------|-----------|---------------|-------|
| `continue-closed`, `continue-local` | repo `.continue/agents/agentalloy.yaml` (modern) + `.continuerc.json` (legacy fallback) | agent YAML: `provider: openai`, `apiBase=…/proj/<token>/v1`, `model: agentalloy-proxy`; rc: `models[].apiBase` | P1 |
| `aider` | `.aider.conf.yml` | `openai-api-base`, `openai-api-key`, `model` | P1 |
| `hermes-agent` | repo-local `.hermes/config.yaml` + `.hermes/.agentalloy-env` (`HERMES_HOME`) | `model.provider`, `model.base_url` (`…/proj/<token>/v1`), `model.default`; direnv/mise activation carriers; gateway restart | P1 |
| `opencode` | repo-local `opencode.json` | `provider.agentalloy` (`@ai-sdk/openai-compatible`, `baseURL=…/proj/<token>/v1`, `apiKey`), `model: agentalloy/agentalloy-proxy` | P1 |
| `claude-code` | per-repo `.claude/settings.local.json` `env` (primary) + `.agentalloy/claude-code-env.sh` | `ANTHROPIC_BASE_URL` (only) | P2 |
| `cline` | `~/.cline/data/settings/providers.json` (user-scoped) | `providers.openai-compatible.settings` (`provider`, `apiKey`, `model`, `baseUrl=…/v1`), `lastUsedProvider` — the exact schema `cline auth` writes (JS-style millisecond timestamps; a strict parse rejects other formats) | P2 |
| `codex` | repo-local `.codex/` (`CODEX_HOME`) | `config.toml` (`model_provider`, `[model_providers.agentalloy]` with `base_url`, `wire_api="responses"`, `env_key`), `.agentalloy-env`, `.gitignore` | P1 |
| `openclaw` | `~/.openclaw/openclaw.json` (user-scoped) | `models.providers.agentalloy` (`baseUrl=…/v1`, `api: openai-completions`), `agents.defaults.model.primary` | P1 |
| `copilot-cli` | `.copilot/.agentalloy-env` (sourced or injected via `agentalloy wrap`) | BYOK env: `COPILOT_PROVIDER_TYPE`, `COPILOT_PROVIDER_BASE_URL` (`…/proj/<token>/v1`), `COPILOT_PROVIDER_API_KEY`, `COPILOT_MODEL` | P1 |
| `github-copilot` | VS Code user-profile `chatLanguageModels.json` + `.github/copilot-instructions.md` | BYOK `customendpoint` provider group (`apiType: chat-completions`, model url `…/v1/chat/completions`, `toolCalling: true`) + sidecar instructions block | P2 |

#### Multiple harnesses per repo

A repo can carry several harness carriers at once — e.g. you drive it from both Claude
Code and Hermes. The carriers are disjoint (`.claude/settings.local.json` vs.
`~/.hermes/config.yaml`), so wiring a second harness never disturbs the first, and
`install-state.json#harness_files_written` tags every entry with its `harness` + `repo_root`.

The SDD lifecycle is **shared** across a repo's harnesses — there is one
`.agentalloy/{phase,config}` per repo, not one per harness. Consequences:

- `agentalloy unwire --harness <name>` removes only that harness's carriers and **keeps**
  the lifecycle state while any other harness still owns the repo. The state files (and the
  empty `.agentalloy/` husk) are removed only when the last harness is unwired.
- A harness's shared **user-scope** config (`~/.hermes/config.yaml`,
  `~/.agentalloy/claude-code-env.sh`) is removed only on the **last repo** wiring that
  harness, so a per-harness unwire in one repo never breaks it in your other repos.

### Native passthrough surfaces

- **Anthropic Messages passthrough** (`proxy_passthrough_router.py`, `POST /proj/<token>/v1/messages`) — Claude Code's transport. No translation: it composes + injects, then forwards the request verbatim to a configurable Anthropic upstream with the caller's own credential.
- **OpenAI Responses passthrough** (`proxy_responses_router.py`, `POST /proj/<token>/v1/responses`) — the codex transport ([responses-surface.md](../responses-surface.md)). Same verbatim-forward, auth-transparent contract against `RESPONSES_UPSTREAM_URL`.

The old bare-`/v1/messages` Anthropic→OpenAI translation shim was removed (see [proxy-surfaces.md](../proxy-surfaces.md)).

### Legacy Wiring (`--legacy`)

The `--legacy` flag opts into the old markdown-injection wiring path for harnesses
that support it (static rules files / sidecar watchers). This is the behavior from
before proxy wiring was introduced. It no longer installs hook scripts — the hook
transport has been removed; `claude-code` is proxy-wired only.


## Full Harness List

AgentAlloy knows 15 harnesses in its registry (16 keys — `gemini-cli` is an alias for `antigravity`). They are grouped below by how AgentAlloy integrates with them under the proxy redesign. See [harness-classification.md](../harness-classification.md) for the classification rule.

### Proxy-wired (default)

These harnesses honor a custom API base URL. AgentAlloy points them at the local proxy, which intercepts every LLM request to inject skill context, evaluate gates, and forward to the real upstream.

| Harness | Proxy Config File | Notes |
|---------|------------------|-------|
| `claude-code` | per-repo `.claude/settings.local.json` `env` block (`ANTHROPIC_BASE_URL=…/proj/<token>` **only** — never an API key), with `.agentalloy/claude-code-env.sh` as a shell/direnv fallback | Native Anthropic Messages passthrough. settings.local.json is read natively by Claude Code, so the proxy auto-loads with no `source`/direnv step (the file is gitignored, so the machine-specific URL stays out of git). Auth is transparent: the proxy forwards the caller's own credential. |
| `continue-closed`, `continue-local` | repo `.continue/agents/agentalloy.yaml` (modern, per-repo `/proj/<token>` — live-verified via the headless `cn` CLI with `--config`) + `.continuerc.json` (legacy) | The legacy rc file is kept because Continue's YAML config path ignored `.continuerc.json` until a Dec 2025 fix and older extensions only read the legacy format. The IDE reads workspace agents from `.continue/agents/` natively; the `cn` CLI needs `--config` pointed at the file. |
| `aider` | `.aider.conf.yml` (`openai-api-base`, `openai-api-key`, `model`) | Sentinel-bounded YAML block. |
| `hermes-agent` | repo-local `.hermes/config.yaml` (copy of `~/.hermes/config.yaml` with the `model` block redirected at `…/proj/<token>/v1`), activated via `HERMES_HOME` from `.hermes/.agentalloy-env` | Inherently per-repo (`--scope` ignored). direnv/mise carriers auto-set `HERMES_HOME` on cd; wiring restarts the repo-scoped hermes gateway. |
| `opencode` | repo-local `opencode.json` (`provider.agentalloy` on `@ai-sdk/openai-compatible`, default model `agentalloy/agentalloy-proxy`) | OpenCode ignores `OPENAI_API_BASE`, and its built-in openai provider speaks the Responses API (`/v1/responses`), which the proxy does not serve — the config-file provider block is the only working vector (verified live by the harness e2e matrix). Merges over an existing `opencode.json`; per-repo `/proj/<token>` baked in. |
| `cline` | `~/.cline/data/settings/providers.json` — `openai-compatible` provider entry + `lastUsedProvider` | The old repo-local `.cline/settings.json` was **inert** (Cline never read it; e2e-matrix finding) — rewired to Cline's real provider store, live-verified via the headless cline CLI. User-scoped → bare `/v1` surface; applies to both the VS Code extension and the CLI. |
| `codex` | repo-local `.codex/config.toml` under `CODEX_HOME` (hermes pattern): global config copied, `model_provider = "agentalloy"`, `[model_providers.agentalloy]` → `base_url=…/proj/<token>/v1`, `wire_api = "responses"`, `env_key = "OPENAI_API_KEY"` | Modern codex is Responses-API-only (ignores `OPENAI_BASE_URL`; `wire_api = "chat"` removed upstream) — served by the proxy's native [Responses passthrough](../responses-surface.md) (`/proj/<token>/v1/responses`, auth-transparent, `RESPONSES_UPSTREAM_URL`). Activate via `source .codex/.agentalloy-env` or `agentalloy wrap codex`. `auth.json` is never copied; `.codex/.gitignore` keeps codex state out of git. Verified live by the harness e2e matrix. |
| `openclaw` | `~/.openclaw/openclaw.json` — `models.providers.agentalloy` custom provider (`api: openai-completions`) + `agents.defaults.model.primary` | OpenClaw ignores `OPENAI_BASE_URL`, and the old `plugins.json` entry was never its schema (e2e-matrix finding; live-verified). User-scoped assistant → bare `/v1` surface. Restart the openclaw gateway after wiring. |
| `copilot-cli` | `.copilot/.agentalloy-env` (BYOK `COPILOT_PROVIDER_*` env vars, per-repo `/proj/<token>` baked in) | Standalone Copilot CLI (npm `@github/copilot`, BYOK GA Apr 2026). Env-var-only carrier: `source` the file or launch via `agentalloy wrap copilot-cli -- copilot`. BYOK routes model traffic to your configured upstream key, not your Copilot subscription. The IDE/extension surface stays sidecar-only as `github-copilot`. |

> **Hook transport removed:** `claude-code` originally used `UserPromptSubmit` / `PreToolUse` / `PostToolUse` hooks installed via `.claude/settings.json`. That transport has been **removed entirely** — `claude-code` is now proxy-wired like every other interceptable harness (the proxy handles phase detection, skill composition, and injection). `agentalloy unwire` still strips any leftover hook entries from a previously hook-wired `~/.claude/settings.json`.

### Sidecar (no proxy interception)

These harnesses route through their own backends and cannot be intercepted by the proxy. AgentAlloy writes a static rules file that the harness reads ambiently; a file-watching sidecar regenerates that file when the project phase or contracts change.

| Harness | Target File | Reason proxy is not available |
|---------|------------|-------------------------------|
| `cursor` | `.cursor/rules/agentalloy.mdc` (or `.cursorrules` fallback) | Cursor routes through its own service; no first-party base-URL override |
| `windsurf` | `.windsurf/rules/agentalloy.md` (or `.windsurfrules` fallback) | No first-party base-URL override |
| `github-copilot` | `.github/copilot-instructions.md` (shared, marker-bounded) | Closed routing through GitHub backend |
| `antigravity` (alias `gemini-cli`) | `GEMINI.md` (shared, marker-bounded) | Antigravity CLI (formerly Gemini CLI). Ignores `OPENAI_*` / `ANTHROPIC_*` env vars; talks to Google's Gemini API |

**Per-harness regeneration details** (from `regenerators.py`):

- **Cursor** — writes `.cursor/rules/agentalloy.mdc` with YAML frontmatter (`description`, `globs`, `alwaysApply: true`). Full file overwrite — AgentAlloy owns this dedicated file entirely. Falls back to `.cursorrules` (shared, marker-bounded) if `.cursor/` directory does not exist. Wire-time seed and watcher refresh target the same file.
- **Windsurf** — writes `.windsurf/rules/agentalloy.md` (dedicated). Falls back to `.windsurfrules` (shared, marker-bounded) if `.windsurf/` directory does not exist. Wire-time seed and watcher refresh target the same file.
- **GitHub Copilot** — marker-block replacement in `.github/copilot-instructions.md` using `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` markers.
- **Antigravity CLI** (formerly Gemini CLI) — marker-block replacement in `GEMINI.md` using the same `AGENTALLOY-CONTEXT` markers.

> **Legacy regenerators:** Regenerators for `cline` (`.clinerules`) and `aider` (`.aider/agentalloy-context.txt`) still exist for users running `agentalloy wire --legacy`. Both are proxy-wired by default and should not need the sidecar.

### Other

| Harness | Notes |
|---------|-------|
| `manual` | Emits the proxy instruction block to stderr for manual copy-paste. |
| `mcp-only` | Legacy entry — no longer accepted standalone. Use `--mcp-fallback` with a real harness. |

## Auto-Detection

When you run `agentalloy wire` without `--harness`, AgentAlloy scans the current directory for filesystem markers and picks the first match. Priority order (from `wire.py`):

| Priority | Harness | Markers Checked |
|----------|---------|----------------|
| 1 | `cursor` | `.cursor/`, `.cursorrules` |
| 2 | `windsurf` | `.windsurf/`, `.windsurfrules` |
| 3 | `continue-local` | `.continuerc.json` |
| 4 | `aider` | `.aider.conf.yml` |
| 5 | `opencode` | `.opencode/`, `opencode.json` |
| 6 | `cline` | `.clinerules` |
| 7 | `antigravity` | `GEMINI.md` |
| 8 | `github-copilot` | `.github/copilot-instructions.md` |
| 9 | `claude-code` | `CLAUDE.md` |
| 10 | `hermes-agent` | `.hermes/`, `AGENTS.md` |

Rationale: tool-specific dotfiles (`.cursor/`, `.windsurfrules`) are stronger signals than `CLAUDE.md` (which is now shared by Claude Code and many other agents). A repo with both `.cursor/` and `CLAUDE.md` auto-detects as `cursor` — pass `--harness claude-code` to override.

When multiple markers are detected, AgentAlloy prints a `NOTE:` on stderr and defaults to the highest-priority match.

## File Strategies

### Dedicated file

AgentAlloy owns the entire file. Written on every regeneration. No sentinels needed inside the file because there is no user content to preserve.

Examples: `.cursor/rules/agentalloy-context.mdc`, `.aider/agentalloy-context.txt`

### Shared file (sentinel-bounded)

The file contains user content alongside AgentAlloy content. AgentAlloy injects a sentinel-bounded block:

```html
<!-- BEGIN agentalloy install -->
<injected content>
<!-- END agentalloy install -->
```

On subsequent writes, the block between sentinels is replaced; all surrounding content is preserved byte-for-byte. Tamper detection: if a user edits content inside the sentinels, the next wire-harness run refuses with a sha256 mismatch error unless `--force` is passed.

Duplicate sentinel pairs are also rejected — the file writer requires at most one BEGIN and one END marker to avoid stranded pairs that `uninstall` cannot clean up.

### Marker block (sidecar regeneration)

Same concept as sentinel-bounded injection, but uses the `AGENTALLOY-CONTEXT` marker for sidecar regeneration:

```html
<!-- BEGIN AGENTALLOY-CONTEXT -->
<phase prose + contract composition>
<!-- END AGENTALLOY-CONTEXT -->
```

Used by sidecar regenerator functions (`regenerators.py`) for: Cursor (shared-file fallback), Windsurf, GitHub Copilot, Antigravity CLI — plus the legacy `cline` regenerator (`.clinerules`).

## MCP Fallback

The `--mcp-fallback` flag replaces the default markdown-injection wiring with an MCP server configuration. Instead of writing static rules files, AgentAlloy writes an MCP server entry that the harness launches via stdio.

**Supported harnesses:** `claude-code`, `cursor`, `continue-closed`, `continue-local`

Usage:

```bash
agentalloy wire --harness cursor --mcp-fallback
```

### What it does

Writes the MCP server config for the chosen harness. The server is `agentalloy.install.mcp_server` — a dependency-free stdio JSON-RPC server implementing the MCP 2024-11-05 spec. It exposes a single tool:

- **`get_skill_for(task, phase)`** — forwards to the local `/compose` endpoint and returns composed fragments as text.

The server uses `sys.executable` (not bare `python`) so the harness invokes the same Python interpreter that wrote the config.

### Per-harness MCP config targets

| Harness | Config File | Config Location |
|---------|-----------|----------------|
| `claude-code` | `~/.claude/mcp_servers.json` | User scope (always `~/.claude/`) |
| `cursor` | `<repo>/.cursor/mcp.json` | Repo scope |
| `continue-closed` | `<repo>/.continuerc.json` | Repo scope (adds to existing `mcpServers` + `_agentalloy_install_marker`) |
| `continue-local` | `<repo>/.continuerc.json` | Repo scope (same as above) |

### MCP server entry

```json
{
  "command": "<sys.executable>",
  "args": ["-m", "agentalloy.install.mcp_server", "--port", "<port>"]
}
```

The server reads JSON-RPC messages from stdin (newline-delimited), writes responses to stdout, and logs to stderr. Messages are capped at 1 MiB. Protocol version: `2024-11-05`. Server info: `agentalloy v0.1.0`.

### Unsatisfied harnesses

Using `--mcp-fallback` with unsupported harnesses (e.g., `antigravity`, `opencode`, `aider`, `cline`) raises a clear error listing the four supported harnesses and suggesting the default markdown-injection variant instead.

### Legacy `mcp-only` harness

`--harness mcp-only` is no longer accepted as a standalone harness. It was superseded by `--mcp-fallback` and now surfaces a migration message:

```
ERROR: --harness mcp-only is no longer a standalone harness.
FIX:   Pick a real harness and add --mcp-fallback. Example:
       python -m agentalloy.install wire-harness --harness claude-code --mcp-fallback
```

## Uninstalling Proxy Wiring

`agentalloy uninstall` reverses proxy wiring for all proxy-wired harnesses.
Each `_unwire_proxy_*()` function uses sentinel comments to find and remove
the injected block, then cleans up any dedicated files:

| Harness | What gets removed |
|---------|------------------|
| `aider` | Sentinel block from `.aider.conf.yml`; `.agentalloy-aider-instructions.md` |
| `hermes-agent` | Repo-local `.hermes/config.yaml` + `.hermes/.agentalloy-env` (via the WireRecord walk); legacy sentinel blocks in `~/.hermes/config.yaml` / `AGENTS.md` stripped for pre-proxy installs |
| `opencode` | Repo-local `opencode.json` provider block (via the WireRecord walk); legacy `.opencode/.agentalloy-env` + `system-prompt.md` removed for pre-rewrite installs |
| `claude-code` | `.claude/settings.local.json` `env.ANTHROPIC_BASE_URL` stripped (other settings preserved) + `.agentalloy/claude-code-env.sh` removed; empty `.agentalloy/` directory is also removed |
| `cline` | Proxy fields from `.cline/settings.json` (or removes file if empty) |
| `copilot-cli` | `.copilot/.agentalloy-env` (via the WireRecord walk) |
| `codex` | Repo-local `.codex/{config.toml,.agentalloy-env,.gitignore}` (via the WireRecord walk; a pre-existing `config.toml` is restored from `original_content`) |

For `--legacy` installs, uninstall removes the injected sentinel blocks and dedicated files
using the same `AGENTALLOY-CONTEXT` markers.
