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

Harnesses that cannot be configured natively (cursor, windsurf, gemini-cli,
github-copilot) receive a proxy instruction block explaining the proxy is active.

### Wiring modes

| Flag | Behavior |
|------|----------|
| (default) | Proxy wiring — writes native API endpoint config |
| `--legacy` | Legacy markdown-injection — writes static rules files (old behavior) |
| `--mcp-fallback` | MCP server config — writes stdio MCP server entry |

### Proxy-wired harnesses

These harnesses have native proxy wiring via `_wire_proxy_*()` functions:

| Harness | Config File | Fields Written | Phase |
|---------|-----------|---------------|-------|
| `continue-closed`, `continue-local` | `.continuerc.json` | `models[].apiBase` | P1 |
| `aider` | `.aider.conf.yml` | `openai-api-base`, `openai-api-key`, `model` | P1 |
| `hermes-agent` | `~/.hermes/config.yaml` (user) or `AGENTS.md` (repo) | `custom_providers.agentalloy` | P1 |
| `opencode` | `.opencode/.agentalloy-env` | `OPENAI_API_BASE`, `OPENAI_API_KEY` | P1 |
| `claude-code` | per-repo `.claude/settings.local.json` `env` (primary) + `.agentalloy/claude-code-env.sh` | `ANTHROPIC_BASE_URL` (only) | P2 |
| `cline` | `.cline/settings.json` | `apiProvider`, `apiBaseUrl`, `apiKey`, `model` | P2 |

### Anthropic Messages Router

Two paths serve Anthropic-speaking harnesses:

- **Native passthrough** (`proxy_passthrough_router.py`, `POST /proj/<token>/v1/messages`) — Claude Code's transport. No translation: it composes + injects, then forwards the request verbatim to a configurable Anthropic upstream with the caller's own credential.
- **Translation shim** (`proxy_anthropic_router.py`, bare `/v1/messages`) — for Anthropic-speaking harnesses pointed at an OpenAI-compatible upstream (e.g. Cline). It translates Anthropic request/response formats to/from the OpenAI upstream, including streaming SSE conversion. Text-only; tool_calls deltas are stripped.

### Legacy Wiring (`--legacy`)

The `--legacy` flag opts into the old markdown-injection wiring path for harnesses
that support it (static rules files / sidecar watchers). This is the behavior from
before proxy wiring was introduced. It no longer installs hook scripts — the hook
transport has been removed; `claude-code` is proxy-wired only.


## Full Harness List

AgentAlloy knows 13 harness entries in its registry: 12 active plus 1 legacy (`mcp-only`). They are grouped below by how AgentAlloy integrates with them under the proxy redesign. See [harness-classification.md](../harness-classification.md) for the classification rule.

### Proxy-wired (default)

These harnesses honor a custom API base URL. AgentAlloy points them at the local proxy, which intercepts every LLM request to inject skill context, evaluate gates, and forward to the real upstream.

| Harness | Proxy Config File | Notes |
|---------|------------------|-------|
| `claude-code` | per-repo `.claude/settings.local.json` `env` block (`ANTHROPIC_BASE_URL=…/proj/<token>` **only** — never an API key), with `.agentalloy/claude-code-env.sh` as a shell/direnv fallback | Native Anthropic Messages passthrough. settings.local.json is read natively by Claude Code, so the proxy auto-loads with no `source`/direnv step (the file is gitignored, so the machine-specific URL stays out of git). Auth is transparent: the proxy forwards the caller's own credential. |
| `continue-closed`, `continue-local` | `.continuerc.json` (`models[].apiBase`) | JSON mutation per model entry. |
| `aider` | `.aider.conf.yml` (`openai-api-base`, `openai-api-key`, `model`) | Sentinel-bounded YAML block. |
| `hermes-agent` | `~/.hermes/config.yaml` (`custom_providers.agentalloy`) user scope, or sentinel block in `AGENTS.md` repo scope | Scope resolved at runtime via `--scope user|repo`. |
| `opencode` | `.opencode/.agentalloy-env` (`OPENAI_API_BASE`) + sentinel block in `.opencode/system-prompt.md` | Env file must be sourced before launching OpenCode. |
| `cline` | `.cline/settings.json` (`apiProvider`, `apiBaseUrl`, `apiKey`, `model`) | Keys merged into existing file; other settings preserved. |

> **Hook transport removed:** `claude-code` originally used `UserPromptSubmit` / `PreToolUse` / `PostToolUse` hooks installed via `.claude/settings.json`. That transport has been **removed entirely** — `claude-code` is now proxy-wired like every other interceptable harness (the proxy handles phase detection, skill composition, and injection). `agentalloy unwire` still strips any leftover hook entries from a previously hook-wired `~/.claude/settings.json`.

### Sidecar (no proxy interception)

These harnesses route through their own backends and cannot be intercepted by the proxy. AgentAlloy writes a static rules file that the harness reads ambiently; a file-watching sidecar regenerates that file when the project phase or contracts change.

| Harness | Target File | Reason proxy is not available |
|---------|------------|-------------------------------|
| `cursor` | `.cursor/rules/agentalloy-context.mdc` (or `.cursorrules` fallback) | Cursor routes through its own service; no first-party base-URL override |
| `windsurf` | `.windsurf/rules/agentalloy.md` (or `.windsurfrules` fallback) | No first-party base-URL override |
| `github-copilot` | `.github/copilot-instructions.md` (shared, marker-bounded) | Closed routing through GitHub backend |
| `gemini-cli` | `GEMINI.md` (shared, marker-bounded) | Ignores `OPENAI_*` / `ANTHROPIC_*` env vars; talks to Google's Gemini API |

**Per-harness regeneration details** (from `regenerators.py`):

- **Cursor** — writes `.cursor/rules/agentalloy-context.mdc` with YAML frontmatter (`description`, `globs`, `alwaysApply: true`). Full file overwrite — AgentAlloy owns this dedicated file entirely. Falls back to `.cursorrules` (shared, marker-bounded) if `.cursor/` directory does not exist.
- **Windsurf** — writes `.windsurf/rules/agentalloy.md`. Falls back to `.windsurfrules` (shared, marker-bounded) if `.windsurf/` directory does not exist.
- **GitHub Copilot** — marker-block replacement in `.github/copilot-instructions.md` using `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` markers.
- **Gemini CLI** — marker-block replacement in `GEMINI.md` using the same `AGENTALLOY-CONTEXT` markers.

> **Legacy regenerators:** Regenerators for `cline` (`.clinerules`) and `aider` (`.aider/agentalloy-context.txt`) still exist for users running `agentalloy wire --legacy`. Both are proxy-wired by default and should not need the sidecar.

### Other

| Harness | Notes |
|---------|-------|
| `manual` | Emits the proxy instruction block to stdout for manual copy-paste. |
| `mcp-only` | Legacy entry — no longer accepted standalone. Use `--mcp-fallback` with a real harness. |

## Auto-Detection

When you run `agentalloy wire` without `--harness`, AgentAlloy scans the current directory for filesystem markers and picks the first match. Priority order (from `wire.py`):

| Priority | Harness | Markers Checked |
|----------|---------|----------------|
| 1 | `cursor` | `.cursor/`, `.cursorrules` |
| 2 | `windsurf` | `.windsurf/`, `.windsurfrules` |
| 3 | `continue-local` | `.continuerc.json` |
| 4 | `aider` | `.aider.conf.yml` |
| 5 | `opencode` | `.opencode/` |
| 6 | `cline` | `.clinerules` |
| 7 | `gemini-cli` | `GEMINI.md` |
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

Used by sidecar regenerator functions (`regenerators.py`) for: Windsurf, GitHub Copilot, Gemini CLI.

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

Using `--mcp-fallback` with unsupported harnesses (e.g., `gemini-cli`, `opencode`, `aider`, `cline`) raises a clear error listing the four supported harnesses and suggesting the default markdown-injection variant instead.

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
| `hermes-agent` | Sentinel block from `~/.hermes/config.yaml` (user) or `AGENTS.md` (repo) |
| `opencode` | `.opencode/.agentalloy-env` and `.opencode/system-prompt.md` |
| `claude-code` | `.claude/settings.local.json` `env.ANTHROPIC_BASE_URL` stripped (other settings preserved) + `.agentalloy/claude-code-env.sh` removed; empty `.agentalloy/` directory is also removed |
| `cline` | Proxy fields from `.cline/settings.json` (or removes file if empty) |

For `--legacy` installs, uninstall removes the injected sentinel blocks and dedicated files
using the same `AGENTALLOY-CONTEXT` markers.
