# Harness Catalog

Complete reference for all coding-agent harnesses supported by AgentAlloy, including target files, integration vectors, tier classification, and auto-detection markers.

## Full Harness List

AgentAlloy knows 13 harness entries in its registry: 12 active plus 1 legacy (`mcp-only`). They are grouped by tier below.

### Tier 1: Per-Turn Hooks

Tier 1 harnesses expose per-turn hooks that fire on every agent turn. AgentAlloy installs hook scripts into the harness's settings, enabling phase transition detection, semantic gate evaluation, system skill enforcement, and per-turn context injection.

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

Tier 3 harnesses do not expose any hook API. AgentAlloy writes static rules files that the harness reads, and a file-watching sidecar regenerates those files when the project phase or contracts change.

| Harness | Target File | Integration Vector | File Strategy |
|---------|------------|-------------------|---------------|
| `cursor` | `.cursor/rules/agentalloy-context.mdc` (or `.cursorrules` fallback) | `markdown_injection` | Dedicated (modern) / Shared (legacy) |
| `windsurf` | `.windsurf/rules/agentalloy.md` (or `.windsurfrules` fallback) | `markdown_injection` | Dedicated (modern) / Shared (legacy) |
| `github-copilot` | `.github/copilot-instructions.md` | `markdown_injection` | Shared (marker block) |
| `cline` | `.clinerules` | `system_prompt_snippet` | Shared (marker block) |
| `gemini-cli` | `GEMINI.md` | `markdown_injection` | Shared (marker block) |
| `aider` | `.aider/agentalloy-context.txt` + `.aider.conf.yml` | `system_prompt_snippet` | Dedicated file |

**Per-harness regeneration details** (from `regenerators.py`):

- **Cursor** — writes `.cursor/rules/agentalloy-context.mdc` with YAML frontmatter (`description`, `globs`, `alwaysApply: true`). Full file overwrite — AgentAlloy owns this dedicated file entirely. Falls back to `.cursorrules` (shared, marker-bounded) if `.cursor/` directory does not exist.
- **Windsurf** — writes `.windsurf/rules/agentalloy.md`. Falls back to `.windsurfrules` (shared, marker-bounded) if `.windsurf/` directory does not exist.
- **GitHub Copilot** — marker-block replacement in `.github/copilot-instructions.md` using `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` markers.
- **Cline** — marker-block replacement in `.clinerules` using the same `AGENTALLOY-CONTEXT` markers.
- **Gemini CLI** — marker-block replacement in `GEMINI.md` using the same `AGENTALLOY-CONTEXT` markers.
- **Aider** — writes `.aider/agentalloy-context.txt` (dedicated file, full overwrite). Also adds `.agentalloy-aider-instructions.md` to the `.aider.conf.yml` `read:` section via sentinel-bounded injection.

### Non-Tiered

These harnesses integrate with AgentAlloy but are not classified as Tier 1 or Tier 3. They receive initial wiring but do not participate in the signal layer (no hooks, no sidecar regenerator).

| Harness | Target File | Integration Vector | Notes |
|---------|------------|-------------------|-------|
| `hermes-agent` | `.hermes/SOUL.md` (user scope) or `AGENTS.md` (repo scope) | `markdown_injection` | Scope resolved at runtime via `--scope user\|repo`. Both targets are shared files (sentinel-bounded). |
| `opencode` | `.opencode/system-prompt.md` | `system_prompt_snippet` | Open-source coding agent. Shared file, sentinel-bounded. |
| `continue-local` | `.continuerc.json` | `system_prompt_snippet` | Local LLM variant of Continue.dev. Custom command only, no system message injection. |
| `manual` | stdout | `manual` | Emits a sentinel-bounded markdown block to stdout for manual copy-paste. Useful for harnesses without dedicated wiring. |
| `mcp-only` | None | `mcp_server_config` | Legacy entry — no longer accepted as a standalone harness. Use `--mcp-fallback` with a real harness instead. |

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

Used by Tier 3 regenerator functions (`regenerators.py`) for: Windsurf, GitHub Copilot, Cline, Gemini CLI.

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
