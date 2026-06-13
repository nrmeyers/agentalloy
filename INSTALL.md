# AgentAlloy — Install Runbook

> **For the calling LLM:** Read this file top-to-bottom. Each numbered step tells you what to do. When you see a `> RUN` block, execute that command and capture the output. When you see a `> ASK` block, ask the user the question and wait for their answer before proceeding. When you see a `> CONFIRM` block, present the result to the user and wait for them to confirm or correct.
>
> Skip steps already marked complete in the user-scope state file at `${XDG_CONFIG_HOME:-~/.config}/agentalloy/install-state.json`. If that file doesn't exist yet, you're on a fresh install. (You can read it with `agentalloy status`.)
>
> If any subcommand exits with a non-zero status, surface the error to the user and run `agentalloy doctor` for remediation hints. Do not continue past a failed step.

---

## What this installs

A local **AgentAlloy** service that gives your coding agent (this LLM, or another) access to a curated corpus of engineering skills — testing patterns, error handling, deployment recipes, observability, security, etc. — composed dynamically per task.

The runtime is a small FastAPI service backed by:
- An embedding model (`qwen3-embedding:0.6b`, 1024-dim) — runs on any hardware via Ollama, LM Studio, or llama-server
- A skill corpus split into **packs** that the user opts into at install time (default: 5 always-on packs — `core`, `engineering`, `documentation`, `performance`, `refactoring`; opt-in: `python`, `typescript`, `nodejs`, `fastapi`, `react`, `go`, `rust`, `data-engineering`, etc.). Pack source YAMLs ship in the wheel; the binary corpus (LadybugDB + DuckDB) is generated locally on first install.
- Your handoff harness (Claude Code / Cursor / Continue.dev / etc.) — wired so it can query the API

**AgentAlloy is user-scoped, not per-repo.** You install once; every project the user opens can wire to the same service. State lives at `${XDG_CONFIG_HOME:-~/.config}/agentalloy/`; corpus at `${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/`. Repos contain only sentinel-bounded blocks injected into agent config files (`CLAUDE.md`, `.cursor/rules/agentalloy.mdc`, etc.).

Total install time: usually 3–5 minutes on a warm machine.

---

## TL;DR

Most users want exactly this:

```bash
# One-time: install uv if needed (Linux / macOS)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the CLI into PATH so it works from any directory.
uv tool install git+https://github.com/nrmeyers/agentalloy.git

# Once per machine — installs everything user-scoped.
# Will prompt: "Do you want agentalloy to run persistently as a background service?"
agentalloy setup

# Once per repo — wire this project to the service. Auto-detects the harness.
cd ~/dev/some-project && agentalloy wire

# If you chose manual mode during setup, start the service now:
agentalloy serve
```

If you want to walk the user through this carefully (recommended for first install on the machine), the rest of this runbook drills into each step the LLM should take.

---

## Prerequisites

You need:
- **Python 3.12+** with [`uv`](https://github.com/astral-sh/uv) installed
- A network connection (for model downloads — the corpus is already in the wheel)
- One of the supported handoff harnesses installed (we'll ask which one in step 5)

The runbook itself runs `agentalloy preflight` (Step 0 below) to verify these. **Never bypass a failed preflight check** — every later step assumes the prereqs are met, and skipping a fatal failure here is what causes the LLM to hand-roll workarounds (`~/.local/bin/agentalloy install-packs --list` etc.) midstream.

For missing binaries (`uv`, `ollama`), **stop and ask the user to install them**. Do not auto-execute install scripts — that's a non-reversible action that requires the human in the loop. Install commands:

- **uv:** see https://docs.astral.sh/uv/getting-started/installation/
- **Ollama (Linux):** `curl -fsSL https://ollama.com/install.sh | sh`
- **Ollama (macOS):** `brew install ollama`, or https://ollama.com/download/mac
- **Ollama (Windows / other):** see https://ollama.com/download

After installing Ollama, the user must have `ollama serve` running (the official installer sets this up as a service on macOS/Windows; on Linux the install script registers a systemd unit). Step 0's runner-phase preflight confirms reachability.

> **Ollama SSH key authentication:** If your Ollama instance requires SSH key authentication
> (common when `OLLAMA_HOST` is set to a remote instance, or when SSH auth is enabled on a
> local instance), you must have an ed25519 key at `~/.ollama/id_ed25519` before pulling
> models. Ollama 0.20.3+ looks for the key at this path (not `~/.ssh/id_ed25519`).
>
> - **Check if auth is required:** Run `ollama list`. If it succeeds without a key, your
>   instance does not require auth. If it fails with
>   `pull model manifest: open ~/.ollama/id_ed25519: no such file or directory`, you need
>   to set up a key.
> - **Generate a key:** `ssh-keygen -t ed25519 -f ~/.ollama/id_ed25519 -N ""`
> - **Register the public key:** Copy the contents of `~/.ollama/id_ed25519.pub` to the
>   Ollama server's `~/.ollama/server_user.pub` (or the equivalent auth config for your setup).
> - **Remote Ollama:** If `OLLAMA_HOST` points to a remote instance, the public key must be
>   registered on that remote server's Ollama configuration.

---

## Step 0: Preflight (run this first, every time)

> RUN
> ```bash
> uv run python -m agentalloy.install preflight
> ```

This runs the host-agnostic checks: Python ≥ 3.12, `uv` present, `agentalloy` resolvable on PATH (i.e. `~/.local/bin` is in `$PATH`), XDG dirs writable, network reachable, default port `47950` free.

> CONFIRM
>
> If `preflight` exits non-zero, the JSON output lists `fatal_failures` and a `remediation` line for each. **Surface every fatal remediation to the user verbatim and STOP.** Do not run `agentalloy setup`, do not move on to Step 1, and do not invent workarounds. Once the user has applied the fixes, re-run Step 0 until it exits 0.

If `cli_on_path` fails before Step 1b has run, that's expected — Step 1b installs the CLI. Run Step 1 + Step 1b first, then re-run Step 0 to confirm `cli_on_path` now passes.

---

## Step 1: Pre-flight

> RUN
> ```bash
> uv sync
> ```

This installs the agentalloy Python dependencies into a project-local `.venv`. Should take under 30 seconds on a warm cache.

If `uv sync` fails:
- Network issue → retry or check proxy settings
- Python version mismatch → `python --version` should be ≥ 3.12

---

## Step 1b: Install the CLI user-scoped

> RUN
> ```bash
> uv tool install git+https://github.com/nrmeyers/agentalloy.git
> ```

This installs the `agentalloy` command into the user's PATH (at `~/.local/bin/agentalloy` or equivalent) so it works from any directory — not just from inside this repo. Required so `agentalloy wire`, `agentalloy serve`, and `agentalloy status` work after you `cd` into a project repo.

**Contributor note:** If you're developing agentalloy itself, install editable instead:

```bash
git clone https://github.com/nrmeyers/agentalloy.git
cd agentalloy
uv sync
uv tool install --editable .
```

Verify it landed by re-running preflight (which is the authoritative PATH check):

> RUN
> ```bash
> agentalloy preflight
> ```

If `cli_on_path` still fails, the JSON `remediation` field gives the exact `export PATH=...` line to add. Surface it to the user verbatim and **STOP** until they confirm they've fixed their shell profile and `which agentalloy` resolves to `~/.local/bin/agentalloy`.

---

## Step 2: Hardware detection

> RUN
> ```bash
> agentalloy detect
> ```

This emits a JSON document describing the hardware. Read it. The output is also written to `${XDG_DATA_HOME:-~/.local/share}/agentalloy/outputs/detect.json` so subsequent steps can refer to it.

> CONFIRM
>
> Tell the user, in plain English, what was detected. For example:
> > "I detected a MacBook Pro with Apple Silicon (M3 Pro), 36 GB unified memory, macOS 14.5. No discrete GPU. Metal acceleration is available. Does that look right?"
>
> Wait for the user to confirm or correct. If they correct, replace the corresponding fields in your working memory and use the corrected values for subsequent steps.

---

## Step 3: Host target selection

> RUN
> ```bash
> agentalloy recommend-host-targets --hardware ~/.local/share/agentalloy/outputs/detect.json
> ```

The output lists which host targets are available on this hardware. Exactly one will be flagged `recommended: true`.

> ASK
>
> Present the recommendation first, then list alternatives. For example:
> > "I recommend running the embedding model on the **iGPU** (Apple Metal), because it's faster than CPU and your Mac has it available. Alternatives: dGPU (not available on this hardware), or CPU+RAM (slower but works).
> >
> > Use the recommendation, or pick a different target?"
>
> Wait for an answer. Default to the recommendation if the user just hits enter.

---

## Step 4: Model variant selection

> RUN
> ```bash
> agentalloy recommend-models --hardware ~/.local/share/agentalloy/outputs/detect.json --host <chosen-target>
> ```

The output lists `{embed_model, embed_runner}` options valid for the chosen host target, with one flagged `default: true`. The `preset` field tells you which `.env` preset will be used.

> ASK
>
> Most users want the default. For example:
> > "For Apple Silicon + iGPU, I'll use **qwen3-embedding:0.6b** via Ollama. Use this default, or pick a different runner?"
>
> Wait for confirmation.

---

## Step 5: Pull models

> **Pre-pull check:** If your Ollama instance requires SSH key authentication, verify the
> key exists before pulling:
>
> ```bash
> test -f ~/.ollama/id_ed25519 && echo "Key present" || echo "Key missing — see prerequisites"
> ```
>
> If the key is missing, generate it as described in the prerequisites section above.

> RUN
> ```bash
> agentalloy pull-models --models ~/.local/share/agentalloy/outputs/recommend-models.json
> ```

The output may include `manual_steps_required` if the user picked a runner without auto-pull (LM Studio, MLX, vLLM). If so:

> CONFIRM
>
> Read the `manual_steps_required` instructions to the user verbatim. Wait for them to confirm they've completed those steps before proceeding.

If `recommend-models` ran non-interactively and defaulted to `ollama` but the user has `llama-server` installed, pass `--runner llama-server` to override:

```bash
agentalloy pull-models --models ~/.local/share/agentalloy/outputs/recommend-models.json --runner llama-server
```

---

## Step 6: Initialize the corpus directory

> RUN
> ```bash
> agentalloy seed-corpus
> ```

This creates the user-scoped corpus directory at `${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/` and initializes empty LadybugDB + DuckDB stores. The wheel no longer ships pre-built skills — Step 8 below populates the corpus from packs the user picks.

---

## Step 7: Start the embedding server

> RUN
> ```bash
> agentalloy start-embed-server --models ~/.local/share/agentalloy/outputs/recommend-models.json
> ```

This brings the embedding backend online before pack ingestion. What happens depends on the runner chosen in Step 4:

- **llama-server**: spawns `llama-server --embeddings --port 11434 --ubatch-size 2048` in the background and waits up to 120 seconds for the server to accept connections. The log is written to `~/.local/share/agentalloy/logs/embed-server.log`.
- **ollama**: fires `ollama serve` (idempotent — safe if already running) and polls port 11434 (Ollama's default).
- **lm-studio / other**: prints instructions for you to start the server manually (LM Studio defaults to port 1234). Start it before proceeding to Step 8.

Both the managed llama-server and Ollama listen on **11434** — that's the port the runtime's `RUNTIME_EMBED_BASE_URL` points at, written into `.env` by `write-env`. The step is idempotent: if the embed port is already listening it exits 0 immediately.

> CONFIRM
>
> Wait for this step to exit 0 before continuing. If it times out, check the log at `~/.local/share/agentalloy/logs/embed-server.log` for startup errors.

---

## Step 8: Pick and install skill packs

Run the interactive pack selection:

> RUN
> ```bash
> agentalloy install-packs
> ```

The user is presented with a **tier-grouped pack listing** — packs are organized under labeled tiers (Foundation, Languages, Frameworks, Tooling, Workflows, Domain, Platform, Protocol, Store). Each pack shows its description and skill count. Always-on packs are marked with `[always-on]`.

Example output:

```
 Foundation:
   [1] core            [always-on] — 42 skills
   [2] documentation   [always-on] — 8 skills
   [3] engineering     [always-on] — 55 skills
   ...
 Languages:
   [4] nodejs          — 18 skills
   [5] typescript      — 15 skills
   [6] python          — 22 skills
   ...
 Frameworks:
   [7] fastapi         — 14 skills
   [8] react           — 12 skills
   ...
```

The prompt accepts:
- **Pack names** (comma-separated): `nodejs,typescript,fastapi`
- **Tier names**: `languages,frameworks` installs all packs in those tiers
- **`all`**: installs every pack
- **Blank**: installs only the always-on packs (`core`, `engineering`, `documentation`, `performance`, `refactoring`)

Always-on packs (`core`, `documentation`, `engineering`, `performance`, `refactoring`) are always included regardless of selection.

> ASK
>
> Tell the user:
> > "AgentAlloy's corpus is split into packs. You opt in to the ones that match your stack. Five packs install automatically (marked [always-on]): `core`, `engineering`, `documentation`, `performance`, `refactoring`. Pick any additional packs by name or tier — e.g. `nodejs,typescript` or `languages,frameworks`, or `all`. Leave blank for always-on only. You can always add more packs later with `agentalloy install-pack <name>`."

> Read the available packs from the CLI's interactive prompt. Wait for the user's selection.

The command ingests each chosen pack and runs one bulk re-embed pass at the end. **Expect 5–10 minutes** on a warm-cache iGPU for a moderate selection (e.g., core + engineering + nodejs + typescript = ~115 skills, ~700 fragments).

Non-interactive / scripted environments: pass `--packs <name1,name2,...>` (or `--packs all`) to skip the prompt. With no flag in non-TTY mode, only the always-on packs install. Unknown pack names in `--packs` cause the command to fail fast with the available pack list; pass `--ignore-unknown` to skip unrecognized names and continue with the known subset.

If the bulk re-embed fails partway (e.g., the embedding server crashes mid-run), the install state records what landed and the embed step is idempotent — just re-run `agentalloy reembed` to finish.

---

## Step 9: Write `.env`

> RUN
> ```bash
> agentalloy write-env --preset <chosen-preset>
> ```

(Substitute the preset name from step 4's `preset` field, e.g., `apple-silicon`.) The `.env` is written to `${XDG_CONFIG_HOME:-~/.config}/agentalloy/.env` with mode `0600` (owner read/write only).

If the user wants a non-default port (because 47950 is taken on their machine), pass `--port <n>`. Otherwise let it default to 47950.

---

## Step 10: Handoff harness selection

> ASK
>
> Ask the user which coding harness they're using. Read the list aloud:
> > "What harness will be calling the skill API?
> > 1. Claude Code
> > 2. Gemini CLI
> > 3. Cursor
> > 4. Continue.dev with a closed/cloud model (Anthropic, OpenAI)
> > 5. Continue.dev with a locally-hosted model
> > 6. OpenCode with a local LLM
> > 7. Aider with a local LLM
> > 8. Cline
> > 9. Other / I'll wire it manually
> > 10. I want the strict-tools MCP fallback for one of the above"
>
> Wait for the user's choice. Note: option 10 is a compound — if chosen, follow up with "which of options 1–8 should the MCP server be configured for?"

Record the harness choice. The CLI uses one of: `claude-code`, `gemini-cli`, `cursor`, `continue-closed`, `continue-local`, `opencode`, `aider`, `cline`, `manual`. For the strict-tools MCP fallback, pass `--mcp-fallback` with one of the supported harnesses (claude-code, cursor, continue-closed, continue-local).

---

## Step 11: Wire the harness

> RUN
> ```bash
> cd <user's repo> && agentalloy wire --harness <chosen-harness>
> ```

(Substitute the harness key from step 10.) `agentalloy wire` auto-detects the harness from the cwd if you omit `--harness`. `wire` is the convenience wrapper over the explicit `agentalloy wire-harness` command (an accepted alias); user-facing flows should prefer `wire`.

**Auto-detection priority** (used when `--harness` is omitted; first match wins):
1. `.cursor/` or `.cursorrules` → `cursor`
2. `.continuerc.json` → `continue-local`
3. `.aider.conf.yml` → `aider`
4. `.opencode/` → `opencode`
5. `.clinerules` → `cline`
6. `GEMINI.md` → `gemini-cli`
7. `CLAUDE.md` → `claude-code`

A repo with multiple markers (e.g. both `.cursor/` and `CLAUDE.md`, common when more than one agent is wired to the project) will pick the higher-priority entry and print a `NOTE:` line so the user can pass `--harness <name>` to override. Tool-specific dotfiles outrank `CLAUDE.md` because the latter is shared by several agents and is a weaker signal.

The output lists which file(s) were modified and where the sentinel-bounded agentalloy block was injected. Tell the user:

> "I added a agentalloy integration block to **CLAUDE.md** in your project. The block is bounded by `<!-- BEGIN agentalloy install -->` / `<!-- END agentalloy install -->` markers — `agentalloy unwire` removes only what's between the markers, so your other content is safe.
>
> Repos are wired one-at-a-time. To wire another project, `cd` into it and run `agentalloy wire` again — AgentAlloy state is user-scoped, so you don't need to re-do steps 1–8."

If the user picked `manual`, the output includes copy-pasteable instructions for the user to apply themselves. Read those to the user.

---

## Step 12: Verify

> RUN
> ```bash
> agentalloy verify
> ```

This runs 8 enumerated install-time checks (embedding endpoint reachable, returns 1024-dim, DuckDB present at the user-scope corpus dir, LadybugDB present, skill count meets minimum, harness config present, harness config URL matches, runtime port available).

When the service is running, the corpus checks (`duckdb_present`, `ladybug_present`, `skill_count_meets_minimum`) query `GET /diagnostics/runtime` instead of opening DB files directly — Kùzu's single-writer lock would otherwise make those checks fail spuriously while the service holds the corpus open. `runtime_port_available` accepts `"healthy"` (passes) and `"degraded"` (passes with warning) responses from `/health`.

If `all_checks_passed: true`, proceed to step 13.

If any check fails:
> RUN
> ```bash
> agentalloy doctor
> ```
>
> Read the doctor output to the user. Each failed check has an `error` and a `remediation`. Surface the remediation to the user and ask if they want to retry the failed step or get help.

---

## Step 13: Enable persistent service

> **Note:** If you ran `agentalloy setup`, this step was already prompted interactively as part of that command. Skip to Step 13 if `install-state.json` already contains a `service_mode` entry.

> ASK
> "Do you want AgentAlloy to start automatically in the background, or will you start it manually each session?
>  1. Persistent — native service (systemd on Linux / launchd on macOS, starts at login)
>  2. Persistent — container (single-container model, starts on demand)
>  3. Manual — I'll run `agentalloy serve` myself"

Then based on the answer:

> RUN
> ```bash
> # For native:
> agentalloy enable-service --mode native
>
> # For container:
> agentalloy enable-service --mode container
>
> # For manual:
> agentalloy enable-service --mode manual
> ```

The subcommand detects the available service manager (systemd/launchd) or container runtime (podman preferred, docker fallback), writes the appropriate unit/plist/startup invocation, starts the service, and polls `/health` for up to 30s to confirm startup. Container deployments pull a pre-built image from GHCR — no repo checkout, no build context, and no `git` required (see the variant table below). On success, the mode is recorded in `install-state.json`.

> **Container deployments pull a pre-built image from GHCR.** The CI pipeline builds and publishes two image variants on every merge to `main`:
>
> | Variant | Size | Model | Use case |
> |---|---|---|---|
> | `ghcr.io/nrmeyers/agentalloy:latest` | ~300 MB | Not included | General users with network access. The model is pulled at first container start. |
> | `ghcr.io/nrmeyers/agentalloy:full` | ~975 MB | Pre-pulled (`qwen3-embedding:0.6b`) | Air-gapped/enterprise environments that need the model baked into the image. |
>
> Select the variant with `--image-tag` during setup:
> ```bash
> # Lightweight (default) — model downloaded at first start
> agentalloy setup --deployment container
>
> # Full — model pre-pulled into the image
> agentalloy setup --deployment container --image-tag full
> ```
>
> Setup pulls the image directly — no repo checkout, no build context, and no `git` required. For air-gapped environments, use `--image-path` to deploy from a local tarball (produced via `podman save`).
>
> **SSH key authentication (container):** The container mounts the host's `~/.ollama` directory into `/root/.ollama` inside the container. If your Ollama instance requires SSH key authentication (see the prerequisites section), the same key at `~/.ollama/id_ed25519` on the host is automatically available to the container — no additional configuration needed. If the model pull fails with an SSH key error inside the container, fix the key on the host as described in the prerequisites.

---

## Step 14: Start the service + first-run demo

Start the service in foreground (recommended — same idiom as `ollama serve`):

> RUN
> ```bash
> agentalloy serve
> ```

This sources `${XDG_CONFIG_HOME:-~/.config}/agentalloy/.env` into the process environment, then execs `uvicorn agentalloy.app:app` on the configured port. **Leave it running** in the terminal; open a new shell for the demo curl.

Alternatively, the user can manually run `uv run uvicorn agentalloy.app:app --host 127.0.0.1 --port 47950` from a terminal of their choice — `agentalloy serve` is just the convenience wrapper.

Wait 3 seconds for the service to start, then in another shell:

> RUN
> ```bash
> curl -s -X POST http://localhost:47950/compose \
>   -H 'Content-Type: application/json' \
>   -d '{"task": "write a failing pytest", "phase": "build"}'
> ```

Show the user the response. The `output` field contains concatenated raw skill fragments; `source_skills` lists which skills contributed. Tell the user:

> "The skill API is live. Returned guidance from these skills: [list]. The full text is what your harness will see when it queries this endpoint.
>
> **Try it now:** open your harness (Claude Code / Cursor / etc.) and ask: 'What skills do you have access to right now? Run `curl http://localhost:47950/health` to confirm.' If everything is wired correctly, your harness should respond with a list of skill capabilities pulled from the API."

---

## You're done

State summary:
- Service running at `http://localhost:<port>` (default 47950)
- Service mode recorded: `native` (systemd/launchd), `container` (podman/docker), or `manual` (`agentalloy serve`)
- Skill corpus seeded into `${XDG_DATA_HOME:-~/.local/share}/agentalloy/corpus/`
- Models pulled and on disk
- `.env` written to `${XDG_CONFIG_HOME:-~/.config}/agentalloy/.env`
- This repo's harness wired with sentinel-bounded injection
- All 8 verify checks passed

**To wire another repo to the same service:** `cd ~/dev/other-project && agentalloy wire`. No re-detect, no re-pull, no re-seed needed — the user-scope install serves every repo on this machine.

**To check status across all wired repos:** `agentalloy status` shows the user state, which repos are wired, the corpus location, and whether the service is reachable.

Operator commands the user can run later (these are NOT part of this runbook — they're for reference):

| Command | What it does |
|---|---|
| `agentalloy status` | Show user state + wired repos + service reachability |
| `agentalloy serve` | Start the service in foreground (terminal must stay open) |
| `agentalloy wire` | Wire the current repo (cwd) to the service |
| `agentalloy unwire` | Remove sentinels from the current repo only (keeps user state, `.env`, and corpus) |
| `agentalloy doctor` | Runtime health check on demand |
| `agentalloy update` | Migrate corpus in place after a version bump |
| `agentalloy install-pack <name>` | Add a published skill pack to the user corpus |
| `agentalloy reset-step <name>` | Clear a specific install step (escape hatch for changing config without full uninstall) |
| `agentalloy uninstall` | Full teardown — see below for exactly what's removed |

### Container operational commands

For container deployments (`--deployment container`), use these commands to manage the running container:

| Command | What it does |
|---|---|
| `podman logs -f agentalloy` | Follow container logs (`--tail 100` for the last 100 lines) |
| `podman ps --filter name=agentalloy` | Check if the container is running |
| `podman exec -it agentalloy sh` | Open an interactive shell inside the container |
| `podman restart agentalloy` / `podman stop agentalloy` | Restart / gracefully stop the container |
| `podman rm -f agentalloy` | Force-remove the container |
| `curl http://localhost:47950/health` | Check the service health endpoint |
| `podman exec agentalloy uv run agentalloy reembed` | Re-embed corpus inside the container |
| `podman exec agentalloy uv run agentalloy install-packs --packs all` | Install skill packs inside the container (add `--no-restart` to skip the service bounce) |

### Volume layout

```
+-------------------+          +---------------------------+
| Host              |          | Container (agentalloy)    |
|-------------------|          |---------------------------|
|                   |          |                           |
| ~/.ollama/        | :rw:     | /root/.ollama/            |
| (Ollama models +  |--------->| (Ollama model cache +     |
| SSH keys)         |          | SSH keys)                 |
|                   |          |                           |
| agentalloy-data/  | :rw:     | /app/data/                |
| (named volume)    |--------->| (LadybugDB + DuckDB)      |
|                   |          |                           |
| localhost:47950   | <----->  | :47950                    |
| (health API)      |  -p      | (FastAPI service)         |
|                   |          |                           |
+-------------------+          +---------------------------+
```

Both mounts persist across restarts: `agentalloy-data:/app/data` (LadybugDB + DuckDB, a named volume) and `~/.ollama:/root/.ollama` (Ollama model cache + SSH keys, a host bind mount).

### Health check

The service exposes a health endpoint at:

```bash
curl http://localhost:47950/health
```

Expected response:

```json
{"status": "healthy", "port": 47950, "corpus_ready": true}
```

The container uses a runtime-generated entrypoint script (`/app/entrypoint.sh`) that handles bootstrap: Ollama installation, model pull, migrations, and pack installation. On subsequent starts, the entrypoint skips all bootstrap steps if `.bootstrap-complete` exists.

### Uninstall — what it removes

`agentalloy uninstall` is the one-shot teardown. Run interactively it shows a preset menu (`keep-data` / `full` / `custom`); pass `--preset` or `--yes` to skip it.

**Always removed** (every preset, default behavior):

- **Sentinel-bounded harness blocks** in *every* repo recorded in install-state.json (CLAUDE.md, GEMINI.md, .clinerules, .cursorrules, .cursor/rules/agentalloy.mdc, .opencode/system-prompt.md, .aider.conf.yml, etc.). The cross-repo walk happens before the CLI is removed; pass `--no-all-repos` to limit to cwd. Tampered blocks (sha256 mismatch — the user edited inside the sentinels) are skipped without `--force`.
- **MCP entries** for `agentalloy` from `~/.claude/mcp_servers.json`, the cwd repo's `.cursor/mcp.json`, and `.continuerc.json`. The files are deleted if `agentalloy` was their only entry.
- **Native service unit + companion ollama unit** on Linux (`~/.config/systemd/user/agentalloy.service`, `~/.config/systemd/user/ollama.service`, sanitized `agentalloy.env`). On macOS the launchd plist at `~/Library/LaunchAgents/ai.agentalloy.plist`.
- **Manual-mode agentalloy server** if it's still listening on the configured port (SIGTERM, escalating to SIGKILL after 10s).
- **User-scope state**: `${XDG_CONFIG_HOME}/agentalloy/.env`, `install-state.json`, the state directory.
- **Derivable artifacts**: `${XDG_DATA_HOME}/agentalloy/outputs/` (per-step JSON dumps including preflight) and `server.log`.
- **CLI uninstall**: removes the `agentalloy` CLI from `~/.local/bin` (via `uv tool uninstall` or `pipx uninstall` depending on how it was installed).

**Preserved by default** — the corpus DB (`${XDG_DATA_HOME}/agentalloy/corpus/`) and pulled Ollama models survive a plain `agentalloy uninstall`. Pass `--remove-data` (or pick the `full` preset) to wipe the entire `${XDG_DATA_HOME}/agentalloy/` directory, the model cache (`~/.ollama`), the download cache (`~/.cache/agentalloy`), and any container named volumes.

**Flags**:
- `--remove-data` — also remove the corpus DB, model cache, and container volumes (default: preserve).
- `--keep-data` — explicit opt-in for the default preserve behavior (no-op; documents intent in scripts).
- `--force` — remove sentinel blocks even when the inner content has been edited.
- `--no-all-repos` — only clean sentinels in cwd (legacy behavior; useful for partial cleanup).
- `--preset {keep-data|full|custom}` — skip the menu: `keep-data` removes wiring + `.env` only; `full` removes everything (services, models, datastore, wiring, state); `custom` drills into a per-item prompt.

**Full wipe one-liner** (for testers ready to reinstall from scratch):
```bash
agentalloy uninstall --preset full --yes
```

### Uninstalling the container model

For container deployments (`--deployment container`), `agentalloy uninstall` also handles teardown:

- Stops and removes the `agentalloy` container and its local image (always).
- Removes the `agentalloy-data` named volume and the `~/.ollama` model cache **only with `--remove-data` / the `full` preset** — a plain uninstall preserves them.
- Cleans up harness wiring and state files (same as the native model).

To keep the corpus and model cache, use the default uninstall (or `--preset keep-data`) — both preserve the named volume and Ollama models while removing wiring and `.env`.

---

## If you got stuck

If you (the LLM) hit an unexpected state at any step, **stop and tell the user**. Don't improvise around the runbook. The CLI is the source of truth — if it says a step failed, that step failed; don't assume.

Common stuck-states:
- The CLI prints a `WARNING: Found legacy per-repo state at <repo>/.agentalloy/install-state.json`. That's a AgentAlloy install from before the v2 user-scope refactor. Either delete the legacy file or `mv` it to the user-scope location (the warning prints the exact command).
- The CLI exits 3 (schema mismatch). The user has a state file from a different version. Tell them to back it up and re-run install with a fresh state.
- The CLI exits 4 (already-completed). That step ran successfully before. Read the user-scope state file to see what's done; skip ahead. (`agentalloy status` shows this concisely.)
- A required external tool (Ollama, LM Studio) is missing. Tell the user the tool's install URL and wait for them to install it manually. Do NOT auto-execute install scripts.
- A port collision on 47950. Re-run `write-env` with `--port <n>` and re-run `agentalloy wire` so the harness config gets the new URL.
- **Ollama SSH key missing:** You see `pull model manifest: open ~/.ollama/id_ed25519: no such file or directory` when pulling models. Your Ollama instance requires SSH key authentication and the key is absent. **Fix:** follow the SSH-key steps in the prerequisites section (generate at `~/.ollama/id_ed25519`, register the `.pub` on the server).
- **Container model pull fails with the same SSH key error (container only):** the key exists on the host but the container can't read the mounted `~/.ollama`. **Fix:** ensure the host directory exists (`mkdir -p ~/.ollama && ssh-keygen -t ed25519 -f ~/.ollama/id_ed25519 -N ""`), then `podman restart agentalloy` (the container re-reads the mount on each start).
