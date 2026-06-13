# Sidecar Harnesses: File-Watching Fallback

The AgentAlloy proxy intercepts LLM traffic from harnesses that honor a custom API base URL (Anthropic / OpenAI / custom endpoint) and injects skill context on every turn. A few harnesses can't be proxy-wired — they route to their own backends or ignore base-URL overrides — so AgentAlloy falls back to writing a static rules file that the harness reads ambiently, kept current by a **file-watching sidecar**.

## Which Harnesses Are Sidecar-Only

| Harness | Why it can't be proxy-wired |
|---|---|
| Cursor | Routes through Cursor's own service; no first-party base-URL override |
| Windsurf | No first-party base-URL override |
| GitHub Copilot | Closed routing through GitHub's backend |
| Gemini CLI | Talks to Google's Gemini API; ignores `OPENAI_*` / `ANTHROPIC_*` env vars |

Every other supported harness (Claude Code, Cline, Aider, Continue.dev, OpenCode, Hermes Agent) is proxy-wired by default and does **not** need the watcher.

## Capability Difference

| Capability | Proxy-wired | Sidecar |
|---|---|---|
| Per-turn context injection | Yes — proxy mutates each request | No — context lives in a static file |
| Phase transition detection | Per-turn via proxy | When watcher running; manual fallback otherwise |
| System skill enforcement | Gate evaluation in the proxy | Advisory text in rules file only |
| Semantic gate evaluation | Real-time via `signals/gates.py` | Not available — falls back to UNKNOWN |
| Contract composition | Per-turn via proxy | On file change via watcher |

The watcher has **zero gate-related logic**. It only regenerates rules files and runs `agentalloy compose --contract`. Gate evaluation requires per-turn interception, which only the proxy path provides.

## Architecture

The sidecar consists of two components:

1. **File watcher** (`watch/watcher.py`) — a watchdog-based observer watching `.agentalloy/` for changes
2. **Regenerators** (`watch/regenerators.py`) — per-harness writers that update the correct rules file

```
.agentalloy/
  phase                    ← watched for modifications
  contracts/**/*.md        ← watched for new/modified files

Watcher detects change
  ↓
Loads workflow skill prose for active phase
  ↓
Runs `agentalloy compose --contract` for changed contracts
  ↓
Regenerates harness-specific rules file
```

## Setup

### 1. Wire the harness

```bash
agentalloy wire --harness <name>
```

This writes the initial harness configuration (see [harness-catalog.md](install/harness-catalog.md) for the full list).

### 2. Start the watcher

```bash
agentalloy watch start --harness <cursor|windsurf|github-copilot|gemini-cli>
```

The watcher runs in the foreground. Press Ctrl+C to stop.

**Auto-detection:** If you omit `--harness`, the watcher reads `install-state.json` and auto-detects the active sidecar harness from `harness_files_written`.

### 3. Run persistently

The watcher is recommended under one of these:

- **tmux/screen**: `tmux new -s agentalloy-watch agentalloy watch start --harness cursor`
- **systemd user service**: Create a `[Service]` unit wrapping `agentalloy watch start --harness <name>`
- **launchd** (macOS): Create a `plist` with the same command

## Per-Harness Behavior

Each sidecar harness has a dedicated regenerator that writes to its specific target file. Two strategies are used:

### Dedicated file (full overwrite)

The entire file is owned by AgentAlloy and is overwritten on each regeneration:

| Harness | Target File | Notes |
|---|---|---|
| Cursor | `.cursor/rules/agentalloy-context.mdc` | YAML frontmatter with `alwaysApply: true`, `globs: ["**/*"]` |

### Shared file (marker block)

The file contains user content alongside AgentAlloy content. Only the sentinel-bounded block is replaced; all surrounding content is preserved byte-for-byte:

| Harness | Target File | Marker |
|---|---|---|
| Windsurf | `.windsurfrules` | `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->` |
| GitHub Copilot | `.github/copilot-instructions.md` | Same markers |
| Gemini CLI | `GEMINI.md` | Same markers |

The marker block strategy ensures user edits outside the block survive regeneration. If the markers already exist, the block is replaced in place. On first write, the block is appended.

> **Legacy harnesses:** Regenerators for `cline` (`.clinerules`) and `aider` (`.aider/agentalloy-context.txt`) still exist for users running `agentalloy wire --legacy`, but both are proxy-wired by default and should not need the watcher.

## What the Watcher Does

### On `.agentalloy/phase` change

1. Reads the phase file (YAML or plain text)
2. Extracts the `phase` value
3. Loads the workflow skill's `raw_prose` for that phase via `_load_workflow_skill_for_phase()`
4. Regenerates the rules file with `# Active Phase: <name>\n\n<prose>`

### On `.agentalloy/contracts/**/*.md` change

1. Runs `agentalloy compose --contract <path> --inject --port <port>`
2. Appends the composed output to the content
3. Regenerates the rules file

### Combined events

When both phase and contract changes occur within the same debounce window, the content is joined with `\n\n---\n\n` separators: phase prose first, then contract compositions.

## What the Watcher Does NOT Do

- **No gate evaluation** — semantic gates require per-turn interception, which the watcher does not provide.
- **No semantic analysis** — it does not analyze task content or make decisions about which skills apply.
- **No pre-filtering** — it regenerates files; it does not filter agent output.
- **No system skill enforcement** — system skills written to the rules file are suggestions, not gates.

## Configuration

### Watch config file

Location: `~/.agentalloy/watch/<profile_name>.yaml`

Contents (auto-generated by `watch start`):

```yaml
project_root: /path/to/project
profile_name: default
harness: cursor
poll_interval_s: 1.0
debounce_ms: 500
```

### PID file

Location: `~/.agentalloy/watch/<profile_name>.pid`

Contains the watcher process PID. Removed automatically on shutdown.

### Log file

Location: `~/.agentalloy/watch/<profile_name>.log`

Contains timestamped log entries. Example:

```
2026-05-24 10:30:00,123 INFO Watching /path/to/project/.agentalloy for harness=cursor profile=default
2026-05-24 10:31:15,456 INFO Regenerated cursor rules file
```

### Debounce

Default: **500ms**. Multiple file events within the debounce window are coalesced into a single regeneration. This prevents burst writes (e.g., saving multiple files in an editor) from triggering redundant regenerations.

## CLI Commands

```bash
# Start the watcher (foreground)
agentalloy watch start --harness <name> [--profile <name>]

# Stop a running watcher (sends SIGTERM)
agentalloy watch stop [--profile <name>]

# Check watcher status
agentalloy watch status [--profile <name>] [--json]
```

All commands default to `profile=default` when `--profile` is omitted.

### Manual phase override (sidecar fallback)

When the sidecar watcher is not running, you can manually trigger a phase transition:

```bash
agentalloy phase set <name>
```

This is a fallback for sidecar harnesses. When the watcher is running, phase changes are detected automatically via the file watcher — you do not need to run `phase set`. Proxy-wired harnesses never need this command; the proxy handles phase transitions automatically.

## Relationship to Profiles

The watcher is profile-aware: its config, PID, and log files are all keyed by `profile_name` (see [Configuration](#configuration) above), resolved via `profiles.py` and defaulting to `"default"`. See [profiles-and-overrides.md](profiles-and-overrides.md) for profile resolution details.

## MCP Fallback

Some harnesses support an MCP fallback variant instead of the default markdown-injection approach:

**Supported harnesses:** claude-code, cursor, continue-closed, continue-local

```bash
agentalloy wire --harness cursor --mcp-fallback
```

This writes an MCP server configuration instead of a markdown-injection block. The MCP server (`agentalloy.install.mcp_server`) exposes a single tool:

- `get_skill_for(task, phase)` — forwards to the local `/compose` endpoint and returns composed fragments

The MCP server runs via stdio JSON-RPC (MCP 2024-11-05 spec). It is dependency-free — no MCP SDK required. Run it with:

```bash
python -m agentalloy.install.mcp_server --port 47950
```

See [harness-catalog.md § "MCP fallback"](install/harness-catalog.md) for per-harness MCP configuration details.

## Troubleshooting

### Check if the watcher is running

```bash
agentalloy watch status
```

JSON output (machine-readable):

```bash
agentalloy watch status --json
```

Returns: `{"profile": "default", "running": true/false, "pid": <int|null>, "last_log": "..."}`

### Watcher not detecting changes

1. Verify the `.agentalloy/` directory exists in your project root
2. Check the log file: `~/.agentalloy/watch/<profile_name>.log`
3. Ensure the harness name matches what was wired: compare `--harness` with `install-state.json`

### Stale PID file

If `watch status` reports `running: false` but the PID file exists, remove it manually:

```bash
rm ~/.agentalloy/watch/<profile_name>.pid
```

### Regeneration errors

Check the log file for `Regeneration failed` messages. Common causes:
- No regenerator registered for the harness (must be one of: cursor, windsurf, github-copilot, gemini-cli; legacy: cline, aider)
- Disk full or permission denied on the target file path
