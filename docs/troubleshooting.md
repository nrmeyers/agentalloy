# Troubleshooting

## llama.cpp / Model Issues

AgentAlloy serves inference with two `llama-server` (llama.cpp) instances: an embed
server on **47951** (`llama-server --embeddings --pooling mean --ctx-size 2048 --ubatch-size 2048 --port 47951`)
and an intent reranker server on **47952** (`llama-server --port 47952`, completions mode).
The models are GGUFs: `nomic-embed-text-v1.5.Q8_0.gguf` and `Qwen3-Reranker-0.6B-Q8_0.gguf`.
The embed model is `nomic-embed-text-v1.5` (768-dim), which serves on stock llama.cpp via
the `nomic-bert` architecture; queries must be prefixed with `search_query: ` and documents
with `search_document: `.

### `llama-server: command not found`

The `llama-server` binary is not installed or not on your PATH. On a native
install, `agentalloy pull-models` (run by the setup wizard) auto-downloads a
prebuilt `llama-server` matched to your hardware (a GPU build on NVIDIA/AMD via
Vulkan/CUDA/ROCm, Metal on Apple Silicon, or CPU otherwise) from the
[ggml-org GitHub releases](https://github.com/ggml-org/llama.cpp/releases)
into `~/.local/share/agentalloy/runtime/llama.cpp/` and installs a launcher at
`~/.local/bin/llama-server`. So the usual fix is:

1. Ensure `~/.local/bin` is on your `$PATH`, then re-run `agentalloy setup` (it reinstalls the runtime and pulls models).
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

**Fix:** Re-run the model download step via `agentalloy setup`, or for the
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
- The GGUFs download from Hugging Face (`nomic-ai/nomic-embed-text-v1.5-GGUF`,
  `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF`); a slow connection can take several minutes
- If truly stuck, cancel and retry — partial downloads resume on re-run

## Service / API Issues

### Port 47950 is already in use

Another instance of AgentAlloy is running, or another service is using the default port.

**Fix:** Run `agentalloy write-env --preset <preset> --port <n>` with a different port, then re-run
`agentalloy wire` to update harness configs. If the holder is a *stale* AgentAlloy process (e.g. a
`llama-server` left over from a previous run), `agentalloy cleanup` reclaims it without touching
foreign processes.

### `uninstall` left artifacts behind / orphaned runtimes

Repeated install/uninstall cycles — or a lost `install-state.json` — can strand artifacts that the
*state-driven* `uninstall` and bare `cleanup` don't see: an orphaned `llama-server`, a
container/volume corpse from an interrupted container install, a `.claude/settings.local.json` proxy
carrier in a repo the state never recorded, or a downloaded model.

**Fix (recover):** `agentalloy cleanup` reaps orphaned processes (47950/47951/47952), stale service
units, and a dangling `~/.local/bin/llama-server` shim — foreign-safe (a process you didn't start is
never killed).

**Fix (blank slate):** `agentalloy cleanup --deep` (preview with `--dry-run`, skip the prompt with
`--yes`) sanitizes the host *state-independently* — discovering artifacts by their known locations,
including the container/volume/image (podman and docker), stray proxy carriers (state repos plus a
bounded `$HOME` scan), and the agentalloy data/config/cache directories. A `llama-server` that
predates AgentAlloy is never touched; the CLI itself is left installed (with a hint to remove it).

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

## Code Index

### `/health` shows `modules.code_index: "unavailable"`

The toggle is on but the module's dependencies (tree-sitter + grammars) are not
installed — the service starts anyway and degrades.

**Fix:** `uv tool install 'agentalloy[code-index]'` and restart the service.
(The container image ships the extra preinstalled — this only happens on
native installs of the core wheel.)

### `agentalloy code …` says the module is disabled

The service is running but `CODE_INDEX_ENABLED` is not set.

**Fix:** Re-run `agentalloy setup` and pick the codebase-indexer module, or add
`CODE_INDEX_ENABLED=1` to `~/.config/agentalloy/.env` and restart the service.

### Index job fails in the `embed` phase (`LMUnavailable`)

The embed llama-server on 47951 is down or unreachable. Parsing and the graph
write don't need it; embedding does. (Oversized inputs are handled client-side —
a size rejection degrades to a shorter embed, so `LMUnavailable` here almost
always means the server itself.)

**Fix:** `agentalloy start-embed-server`, confirm `/health` shows
`embedding_runtime: ok`, then re-run `agentalloy code index` — completed
phases are incremental, so the re-run is cheap.

### Repo shows `[stale — N commits behind]`

Informational, not an error: the index was built at an older `git HEAD`.
Nothing auto-reindexes.

**Fix:** `agentalloy code index <path>` — or enroll the repo with
`agentalloy code watch enable` for automatic incremental reindexing.

### Watch is enabled but changes don't trigger a reindex

Watching needs BOTH switches: the `CODE_INDEX_WATCH=1` master switch
(service env) and per-repo enrollment.

**Fix:** `agentalloy code watch status` shows which half is missing;
`agentalloy code watch enable [path]` enrolls the repo, `agentalloy code watch
start` prints how to flip the master switch.

### Repo removal returns 409

An index job for that repo is still active — removal is refused rather than
pulling the store out from under it.

**Fix:** `agentalloy code status` to find the job, cancel it (or let it
finish), then retry `agentalloy code remove`.

### Legacy standalone codebase-indexer detected

`agentalloy doctor` reports a listener on port 8003 or an old
`~/.local/share/codebase-indexer/` data directory. The standalone service is
superseded by this module; its indexes can't be migrated (different storage
engines) — reindexing is the migration.

**Fix:** Stop the old daemon (`code-indexer stop`), index your repos with
`agentalloy code index`, verify with `agentalloy code status`, then delete
`~/.local/share/codebase-indexer/`. Old `<!-- BEGIN codebase-indexer -->`
blocks in harness files are replaced in place the next time you run
`agentalloy wire`.

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

## Web UI

### `/` answers 501 "web_ui_not_built"

The API is running but no web UI bundle exists. Setup downloads the prebuilt
bundle from the GitHub release matching your version; a 501 means that download
failed (offline install, GitHub unreachable) or predates the bundle assets.

**Fix:** `agentalloy pull-web`, then `agentalloy server-restart`. On a dev
checkout you can instead build locally: `cd frontend && pnpm install && pnpm
build` (Node via mise, pnpm — not npm) — the repo-layout build wins over the
downloaded bundle. `AGENTALLOY_WEB_DIST` can point at a build elsewhere.
Container images ship the UI baked in and never need this.

### Writes fail with 403

Mutating endpoints require the `X-AgentAlloy-CSRF: 1` header. The shipped UI
sends it; a 403 usually means a hand-rolled curl/script — add the header.

### Approve returns 409 "approve_refused"

The repo's current phase doesn't match, or the phase's exit artifact is missing
(e.g. approving `add-skill` with nothing under `.agentalloy/custom-skills/`).
The error detail names the exact refusal; the per-repo gate status on the Repos
page shows what's missing.

### Config edits don't take effect

Saving writes the user-scoped `.env`; click **Reload** to apply. Reload is
soft — per-request settings update, but store and embed-server connections
opened at startup keep their old config until a real service restart.
