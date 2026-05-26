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
| `claude-code` | `~/.agentalloy/claude-code-env.sh` | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` | P2 |
| `cline` | `.cline/settings.json` | `apiProvider`, `apiBaseUrl`, `apiKey`, `model` | P2 |

### Anthropic Messages Router

The Anthropic router (`proxy_anthropic_router.py`) allows Claude Code and Cline to use
the Anthropic `/v1/messages` API through the proxy. It translates Anthropic request/response
formats to/from the OpenAI-compatible upstream, including streaming SSE event conversion.

Phase 1 scope: text-only. Tool use / function calling is out of scope — tool_calls
deltas are silently stripped.

### Legacy Wiring (`--legacy`)

The `--legacy` flag opts into the old markdown-injection wiring path. This is the
behavior from before proxy wiring was introduced. Legacy wiring writes static rules
files and (for some harnesses) installs hook scripts.


## Full Harness List

AgentAlloy knows 13 harness entries in its registry: 12 active plus 1 legacy (`mcp-only`). They are grouped by tier below.

### Tier 1: Per-Turn Hooks (Legacy)

> **DEPRECATED:** Claude Code hooks were removed in the proxy wiring pass.
> Proxy wiring replaces hooks entirely — the proxy handles phase detection,
> skill composition, and system message injection transparently.
> The `--legacy` flag still installs hooks for backward compatibility.

Tier 1 harnesses previously exposed per-turn hooks that fired on every agent turn. AgentAlloy installed hook scripts into the harness's settings, enabling phase transition detection, semantic gate evaluation, system skill enforcement, and per-turn context injection.

| Harness | Target File(s) | Integration Vector | Hooks |
|---------|---------------|-------------------|-------|
| `claude-code` | `CLAUDE.md` + `.claude/settings.json` | `markdown_injection` + hooks | `UserPromptSubmit`, `PreToolUse`, `PreToolUse` |
| `continue-closed` | `.continuerc.json` | `markdown_injection` | Custom command (`/skill`) + system message |

**Claude Code hooks** (`.claude/settings.json`):

- `UserPromptSubmit` — fires on every user prompt. Runs `agentalloy signal evaluate-phase` which checks pre-filter keywords and evaluates exit gates. On phase transition, writes `.agentalloy/phase` atomically and emits the next workflow skill's prose.
- `PreToolUse` — fires before every tool call (matcher: `.*`). Used for system skill enforcement — checks `applies_when` predicates on system skills.
- `PostToolUse` — fires after file-writing tools (matcher: `Edit|Write|MultiEdit`). Used for contract detection and phase gate re-evaluation after file changes.

Hook scripts are installed to `~/.agentalloy/hooks/agentalloy-signal.sh` and invoked via environment variables (`AGENTALLOY_HOOK_EVENT`, `AGENTALLOY_TOOL_NAME`, `AGENTALLOY_TOOL_PATH`).

**Continue.dev hooks** (`.continuerc.json`):

- Custom command named `skill` — sends a `curl` POST to the local `/compose/text` endpoint with the user's task description. The agent is instructed to invoke `/skill` before starting any task.
- System message (closed variant only) — instructs the agent to invoke the `/skill` custom command before generating code or a plan.

### Tier 3: Sidecar (No Hooks)

Tier 3 harnesses do not expose any hook API and cannot be proxy-wired. AgentAlloy writes static rules files that the harness reads, and a file-watching sidecar regenerates those files when the project phase or contracts change.

> **NOTE:** `aider` and `cline` were removed from Tier 3 when proxy wiring was added.
> They now use native API endpoint configuration (`_wire_proxy_aider`, `_wire_proxy_cline`).

| Harness | Target File | Integration Vector | File Strategy |
|---------|------------|-------------------|---------------|
| `cursor` | `.cursor/rules/agentalloy-context.mdc` (or `.cursorrules` fallback) | `markdown_injection` | Dedicated (modern) / Shared (legacy) |
| `windsurf` | `.windsurf/rules/agentalloy.md` (or `.windsurfrules` fallback) | `markdown_injection` | Dedicated (modern) / Shared (legacy) |
| `github-copilot` | `.github/copilot-instructions.md` | `markdown_injection` | Shared (marker block) |
| `gemini-cli` | `GEMINI.md` | `markdown_injection` | Shared (marker block) |

**Per-harness regeneration details** (from `regenerators.py`):

- **Cursor** — writes `.cursor/rules/agentalloy-context.mdc` with YAML frontmatter (`description`, `globs`, `alwaysApply: true`). Full file overwrite — AgentAlloy owns this dedicated file entirely. Falls back to `.cursorrules` (shared, marker-bounded) if `.cursor/` directory does not exist.
- **Windsurf** — writes `.windsurf/rules/agentalloy.md`. Falls back to `.windsurfrules` (shared, marker-bounded) if `.windsurf/` directory does not exist.
- **GitHub Copilot** — marker-block replacement in `.github/copilot-instructions.md` using `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` markers.
- **Gemini CLI** — marker-block replacement in `GEMINI.md` using the same `AGENTALLOY-CONTEXT` markers.

> **NOTE:** Cline and aider regeneration details are now legacy-only. With proxy wiring (default), these harnesses use native API config instead of regenerated rules files.

### Non-Tiered

These harnesses integrate with AgentAlloy but are not classified as Tier 1 or Tier 3. With proxy wiring (default), most receive native API endpoint configuration instead of markdown injection.

| Harness | Proxy Config File | Legacy Config File | Notes |
|---------|-----------------|-------------------|-------|
| `hermes-agent` | `~/.hermes/config.yaml` (`custom_providers.agentalloy`) | `.hermes/SOUL.md` / `AGENTS.md` | Scope resolved at runtime via `--scope user\|repo` |
| `opencode` | `.opencode/.agentalloy-env` (`OPENAI_API_BASE`) | `.opencode/system-prompt.md` | Open-source coding agent. Env file requires sourcing. |
| `claude-code` | `~/.agentalloy/claude-code-env.sh` (`ANTHROPIC_BASE_URL`) | `CLAUDE.md` + `.claude/settings.json` | Anthropic Messages API via proxy. Hooks removed. |
| `cline` | `.cline/settings.json` (`apiProvider`, `apiBaseUrl`) | `.clinerules` | Removed from Tier 3; proxy-wired. |
| `aider` | `.aider.conf.yml` (`openai-api-base`, `model`) | `.aider/agentalloy-context.txt` | Removed from Tier 3; proxy-wired. |
| `continue-local` | `.continuerc.json` (`models[].apiBase`) | `.continuerc.json` | Local LLM variant of Continue.dev. |
| `continue-closed` | `.continuerc.json` (`models[].apiBase`) | `.continuerc.json` | Custom command (`/skill`) + system message. |
| `manual` | stdout (proxy instruction block) | stdout (sentinel-bounded markdown) | Emits instructions to stdout for manual copy-paste. |
| `mcp-only` | None | None | Legacy entry — no longer accepted. Use `--mcp-fallback` instead. |

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

Used by Tier 3 regenerator functions (`regenerators.py`) for: Windsurf, GitHub Copilot, Gemini CLI.

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
| `claude-code` | `~/.agentalloy/claude-code-env.sh`; user must remove shell profile source line manually |
| `cline` | Proxy fields from `.cline/settings.json` (or removes file if empty) |

For `--legacy` installs, uninstall removes the injected sentinel blocks and dedicated files
using the same `AGENTALLOY-CONTEXT` markers.
