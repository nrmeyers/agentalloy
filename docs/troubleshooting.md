# Troubleshooting

## Ollama / Model Issues

### `pull model manifest: open ~/.ollama/id_ed25519: no such file or directory`

Your Ollama instance requires SSH key authentication. The agent tried to pull a model
but couldn't find the SSH key.

**Fix:**

1. Generate a key: `ssh-keygen -t ed25519 -f ~/.ollama/id_ed25519 -N ""`
2. Copy the public key to your Ollama server's `~/.ollama/server_user.pub`
3. Re-run `agentalloy pull-models`

**Local Ollama:** You're setting up the key on the same machine where Ollama runs.

**Remote Ollama:** If `OLLAMA_HOST` points to a remote instance, the public key
(`~/.ollama/id_ed25519.pub`) must be registered on that remote server's
`~/.ollama/server_user.pub`. Contact your Ollama administrator.

### Ollama says "server error: unauthorized"

Your SSH public key is registered on the Ollama server but the private key at
`~/.ollama/id_ed25519` doesn't match. This can happen if you regenerated the key
or copied the public key from a different machine.

**Fix:** Generate a new key pair and re-register the public key.

### `ollama: command not found`

Ollama is not installed or not on your PATH. Install it per the prerequisites:

- Linux: `curl -fsSL https://ollama.com/install.sh | sh`
- macOS: `brew install ollama`
- Windows: https://ollama.com/download

After installing, verify with `ollama --version`.

### `ollama: could not connect to ollama server`

The Ollama daemon is not running. Start it with:

```bash
ollama serve
```

Or if running as a service, check its status:

- Linux: `systemctl --user status ollama`
- macOS: `launchctl list | grep ollama`

### Model pull hangs or takes very long

- Check your network connection
- Use `ollama pull <model>` directly in a terminal to see progress
- Large models (7B+) can take 10+ minutes on slow connections
- If truly stuck, press Ctrl+C and retry — Ollama resumes from where it left off

## Service / API Issues

### Port 47950 is already in use

Another instance of AgentAlloy is running, or another service is using the default port.

**Fix:** Run `agentalloy write-env --port <n>` with a different port, then re-run
`agentalloy wire` to update harness configs.

### `preflight` fails with `cli_on_path`

The `agentalloy` CLI is not on your PATH. See
[`agentalloy` command not found after install](#agentalloy-command-not-found-after-install)
under General.

### `preflight` fails with `python_version`

You need Python 3.12 or later.

**Fix:** Check your version with `python --version`. Install a newer version if needed.

## Corpus / Embedding Issues

### Embedding server won't start

The embedding server (Ollama, LM Studio, or llama-server) failed to start. Check the
log at `~/.local/share/agentalloy/logs/embed-server.log`.

### DuckDB lock conflict

Multiple processes are trying to write to the corpus database simultaneously.

**Fix:** Stop all AgentAlloy services, then run `agentalloy reembed`.

Or run `agentalloy doctor --repair` to diagnose and fix automatically.

### Skill count is zero after install

The corpus was not populated. This usually means the pack installation step failed
or was skipped.

**Fix:** Run `agentalloy install-packs --packs all` to re-install all packs.

Or run `agentalloy doctor --repair` to diagnose and fix automatically.

## Harness / Wiring Issues

### Harness config not picking up changes

`agentalloy wire` writes its proxy/wiring configuration inside a **install block**
bounded by `<!-- BEGIN agentalloy install -->` / `<!-- END agentalloy install -->`.
If you edited the content inside these markers, the harness may not recognize the
block.

**Fix:** Run `agentalloy unwire` to remove the sentinels, then `agentalloy wire` to
re-wire cleanly.

> Note: this is distinct from the sidecar watcher's **rules block**, bounded by
> `<!-- BEGIN AGENTALLOY-CONTEXT -->` / `<!-- END AGENTALLOY-CONTEXT -->`, which the
> watcher regenerates in harness rules files. See
> [sidecar-experience.md](sidecar-experience.md). Do not confuse the two markers.

### `agentalloy wire` says harness not found

The current directory doesn't contain a recognized harness configuration file.

**Fix:** `cd` into a project directory that has a supported harness (e.g., one with
`CLAUDE.md`, `.cursor/`, `.opencode/`, etc.).

## General

### `agentalloy` command not found after install

The CLI was installed but `~/.local/bin` is not in your PATH.

**Fix:** Add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile
(`~/.bashrc`, `~/.zshrc`, etc.) and run `source ~/.bashrc` (or equivalent).

### State file schema mismatch (exit code 3)

You have an `install-state.json` from a different version of AgentAlloy.

**Fix:** Back up your state file, then re-run `agentalloy setup` with a fresh state.
Your corpus data is preserved separately.

### Already-completed step (exit code 4)

A step ran successfully before. The install state is up to date.

**Fix:** No action needed. If you want to re-run a specific step, use
`agentalloy reset-step <step-name>` first.
