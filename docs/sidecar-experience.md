# Sidecar Harnesses: File-Watching Fallback

The AgentAlloy proxy intercepts LLM traffic from harnesses that honor a custom API base URL (Anthropic / OpenAI / custom endpoint) and injects skill context on every turn. A few harnesses can't be proxy-wired — they route to their own backends or ignore base-URL overrides — so AgentAlloy falls back to writing a static rules file that the harness reads ambiently, kept current by an **in-process store hook**.

## Which Harnesses Are Sidecar-Only

| Harness | Why it can't be proxy-wired |
|---|---|
| Cursor | Routes through Cursor's own service; no first-party base-URL override |
| Windsurf | No first-party base-URL override |
| GitHub Copilot | Closed routing through GitHub's backend |
| Antigravity CLI (formerly Gemini CLI) | Talks to Google's Gemini API; ignores `OPENAI_*` / `ANTHROPIC_*` env vars |

Every other supported harness (Claude Code, Cline, Aider, Continue.dev, OpenCode, Hermes Agent, Copilot CLI, qwen-code) is proxy-wired by default and does **not** need the sidecar path.

## Capability Difference

| Capability | Proxy-wired | Sidecar |
|---|---|---|
| Per-turn context injection | Yes — proxy mutates each request | No — context lives in a static file |
| Phase transition detection | Per-turn via proxy | Automatic via in-process store hook |
| System skill enforcement | Gate evaluation in the proxy | Advisory text in rules file only |
| Semantic gate evaluation | Real-time via `signals/gates.py` | Not available — falls back to UNKNOWN |
| Contract composition | Per-turn via proxy | On phase change via store hook |

The store hook has **zero gate-related logic**. It only regenerates rules files. Gate evaluation requires per-turn interception, which only the proxy path provides.

## Architecture

The sidecar path consists of two components:

1. **Regenerators** (`watch/regenerators.py`) — per-harness writers that update the correct rules file
2. **In-process store hook** — one `register_wired_repos_watcher()` callback registered at service startup; every phase write fires it, and it resolves the wiring records *at fire time*

```
POST /state/phase (or CLI: agentalloy phase set)
  ↓
State store commits phase row
  ↓
on_write callbacks fire (including store hook)
  ↓
Loads workflow skill prose for active phase
  ↓
Regenerates harness-specific rules file
```

No separate watcher process is needed. The running service registers a single hook at startup, and each fire is scoped to the repo whose phase row changed — a repo wired against an already-running service is covered without a restart.

## Setup

### 1. Wire the harness

```bash
agentalloy add <name>
```

This writes the initial harness configuration (see [harness-catalog.md](install/harness-catalog.md) for the full list).

### 2. Start the service

```bash
agentalloy serve
```

The service registers one store hook at startup; it reads the recorded harness wiring on every phase write, so repos wired later are picked up without a restart. No separate `agentalloy watch` process is needed.

> **Deprecated:** The `agentalloy watch start` command still exists for backward compatibility but its file-watching handler is a no-op. It emits a deprecation warning on startup.

## Per-Harness Behavior

Each sidecar harness has a dedicated regenerator that writes to its specific target file. Two strategies are used:

### Dedicated file (full overwrite)

The entire file is owned by AgentAlloy and is overwritten on each regeneration:

| Harness | Target File | Notes |
|---|---|---|
| Cursor | `.cursor/rules/agentalloy.mdc` | YAML frontmatter with `alwaysApply: true`, `globs: ["**/*"]` |
| Windsurf | `.windsurf/rules/agentalloy.md` | Default target; falls back to shared `.windsurfrules` (below) |

### Shared file (marker block)

The file contains user content alongside AgentAlloy content. Only the sentinel-bounded block is replaced; all surrounding content is preserved byte-for-byte:

| Harness | Target File | Marker |
|---|---|---|
| Windsurf (fallback) | `.windsurfrules` | `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` — used only when the dedicated `.windsurf/rules/agentalloy.md` isn't available |
| GitHub Copilot | `.github/copilot-instructions.md` | Same markers |
| Antigravity CLI | `GEMINI.md` | Same markers |

The marker block strategy ensures user edits outside the block survive regeneration. If the markers already exist, the block is replaced in place. On first write, the block is appended.

> **Legacy harnesses:** Regenerators for `cline` (`.clinerules`) and `aider` (`.aider/agentalloy-context.txt`) still exist for users running `agentalloy add <harness> --sidecar`, but both are proxy-wired by default and should not need the sidecar path.

## What the Store Hook Does

### On phase change

1. Extracts the phase value from the committed store blob
2. Loads the workflow skill's `raw_prose` for that phase via `_load_workflow_skill_for_phase()`
3. Regenerates the rules file with `# Active Phase: <name>\n\n<prose>`

### What the Store Hook Does NOT Do

- **No gate evaluation** — semantic gates require per-turn interception, which the sidecar path does not provide.
- **No semantic analysis** — it does not analyze task content or make decisions about which skills apply.
- **No pre-filtering** — it regenerates files; it does not filter agent output.
- **No system skill enforcement** — system skills written to the rules file are suggestions, not gates.

## CLI Commands

```bash
# Start the service (handles store hook registration automatically)
agentalloy serve

# Check if the service is running
agentalloy status
```

### Manual phase override (sidecar fallback)

When you want to trigger regeneration manually, you can set the phase via CLI:

```bash
agentalloy phase set <name>
```

This writes to the store, which fires the store hook and regenerates the rules file. Proxy-wired harnesses never need this command; the proxy handles phase transitions automatically.

## Relationship to Profiles

The store hook loads workflow skill prose under the `default` profile. Per-repo profile resolution is **not** yet threaded through the service-side hook — `register_wired_repos_watcher()` takes `profile_name` but `app.py` does not pass one. See [profiles-and-overrides.md](profiles-and-overrides.md) for profile resolution elsewhere.

## MCP Fallback

Some harnesses support an MCP fallback variant instead of the default markdown-injection approach:

**Supported harnesses:** claude-code, cursor, continue-closed, continue-local

```bash
agentalloy add cursor --mcp-fallback
```

This writes an MCP server configuration instead of a markdown-injection block. The MCP server (`agentalloy.install.mcp_server`) exposes a single tool:

- `get_skill_for(task, phase)` — forwards to the local `/compose` endpoint and returns composed fragments

The MCP server runs via stdio JSON-RPC (MCP 2024-11-05 spec). It is dependency-free — no MCP SDK required. Run it with:

```bash
python -m agentalloy.install.mcp_server --port 47950
```

See [harness-catalog.md § "MCP fallback"](install/harness-catalog.md) for per-harness MCP configuration details.

## Troubleshooting

### Check if the service is running

```bash
agentalloy status
```

### Rules file not updating after phase change

1. Verify the service is running: `agentalloy status`
2. Check the service logs for `Regeneration failed` or `Regenerated` messages
3. Ensure the harness name matches what was wired: compare with `install-state.json`
4. Confirm the harness has a registered regenerator: one of `cursor`, `windsurf`, `github-copilot`, `antigravity`

### Stale rules file

If the rules file hasn't updated after a phase change:

1. Verify the service is running and not logging errors
2. Manually trigger regeneration: `agentalloy phase set <name>`
3. Check the service logs for regeneration messages

### Regeneration errors

Check the service logs for `Regeneration failed` messages. Common causes:
- No regenerator registered for the harness (must be one of: cursor, windsurf, github-copilot, antigravity; legacy: cline, aider, gemini-cli alias)
- Disk full or permission denied on the target file path
- Workflow skill prose not found for the active phase (the regenerator silently skips if prose is empty)
