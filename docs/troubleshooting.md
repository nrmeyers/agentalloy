# Troubleshooting

## llama.cpp / Model Issues

AgentAlloy serves inference with two `llama-server` (llama.cpp) instances: an embed
server on **47951** (`llama-server --embeddings --port 47951`) and an intent reranker
server on **47952** (`llama-server --port 47952`, completions mode). The models are
GGUFs: `Qwen3-Embedding-0.6B-Q8_0.gguf` and `Qwen3-Reranker-0.6B-Q8_0.gguf`.

### `llama-server: command not found`

The `llama-server` binary is not installed or not on your PATH. On a native
install, `agentalloy pull-models` (run by the setup wizard) auto-downloads a
prebuilt CPU build from the [ggml-org GitHub releases](https://github.com/ggml-org/llama.cpp/releases)
into `~/.local/share/agentalloy/runtime/llama.cpp/` and installs a launcher at
`~/.local/bin/llama-server`. So the usual fix is:

1. Ensure `~/.local/bin` is on your `$PATH`, then re-run `agentalloy pull-models`.
2. Verify with `llama-server --version`.

If your platform has no prebuilt asset (e.g. s390x), install llama.cpp manually
(`brew install llama.cpp` on macOS, or build/download a release from
https://github.com/ggml-org/llama.cpp) and put it on your PATH.

The container image bundles `llama-server` (copied from
`ghcr.io/ggml-org/llama.cpp:full`), so this only applies to native installs.

### GGUF model not downloaded

The embed or reranker GGUF is missing from the models directory (native installs
download it under `~/.local/share/agentalloy/`; the container downloads it into
`/app/data/models` in the `agentalloy-data` volume on first boot).

**Fix:** Re-run the model download step (`agentalloy pull-models`), or for the
container, restart it (`podman restart agentalloy`) so the entrypoint re-fetches any
missing GGUF.

### Embed/reranker server didn't bind (47951 / 47952)

The runtime can't reach a `llama-server` instance. Check that the embed server is
listening on **47951** and the reranker on **47952**:

```bash
curl -sf http://127.0.0.1:47951/health
curl -sf http://127.0.0.1:47952/health
```

If 47951 is down, embedding (and therefore composition) fails. If 47952 is down, the
phase-gate intent classifier simply falls open to cosine — composition still works.
Check the server log at `~/.local/share/agentalloy/logs/embed-server.log`, or for the
container, `podman logs -f agentalloy`.

### Model download hangs or takes very long

- Check your network connection
- The GGUFs download from Hugging Face (`Qwen/Qwen3-Embedding-0.6B-GGUF`,
  `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF`); a slow connection can take several minutes
- If truly stuck, cancel and retry — partial downloads resume on re-run

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

The embed `llama-server` (on 47951) failed to start. Check the
log at `~/.local/share/agentalloy/logs/embed-server.log`. Common causes: the
`llama-server` binary is not on PATH, the GGUF was not downloaded, or port 47951 is
already in use.

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
