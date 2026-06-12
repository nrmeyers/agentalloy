<p align="center">
  <img src="AgentAlloy_cover.webp" alt="AgentAlloy — Just-in-Time Instruction Composer" width="720" />
</p>

<p align="center">
  <b>Fuse your base model with the exact governance, workflows, and skills it needs — right now.</b>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/nrmeyers/agentalloy?color=blue" alt="license" /></a>
  &nbsp;
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="python 3.12+" />
  &nbsp;
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv" /></a>
  &nbsp;
  <img src="https://img.shields.io/badge/runtime-no--LLM-success" alt="no LLM in runtime" />
  &nbsp;
  <img src="https://img.shields.io/badge/packs-35+-orange" alt="35+ packs" />
  &nbsp;
  <img src="https://img.shields.io/badge/skills-300+-orange" alt="300+ skills" />
</p>

`AGENTS.md`, `SKILL.md`, and giant static system prompts were a clever first attempt — and they're already breaking. They load once at session start, then suffer context rot as the conversation drags on and your agent drifts from the script. Reloading them every turn just trades drift for token waste. The real problem is structural: over a single session, the rules your agent must follow and the skills it needs change dozens of times — and static files can't keep up. Leave them out and a smaller model flounders on tasks its training never covered; cram them all in and you pay the token tax on every turn, or pay it again redoing the work it got wrong.

**AgentAlloy** is a **just-in-time instruction composer**. A signal layer — a small local embed model (`qwen3-embedding:0.6b`) plus deterministic Python — wakes only when your agent's situation shifts: a phase transition, a new task contract, a meaningful file change. When nothing has changed, nothing is injected — your agent keeps working with the context it already has. When something *has* changed, AgentAlloy composes a fresh, highly targeted pre-prompt by fusing three instruction sets into the exact agent persona the moment calls for:

- **System Governance** — hard boundaries and operational rules (Linear issue naming, PR branch conventions, CI/deployment gates).
- **Workflow Directives** — process constraints (Spec-Driven Development rules, defining success criteria without solution wording).
- **Domain Skills** — a focused slice of a curated 300+ skill corpus (languages, testing frameworks, discovery techniques) retrieved via hybrid BM25 + dense scoring.

This gives smaller models the leverage to punch above their weight class, and gives larger models a runtime reminder of how they should be operating — both of which mean getting it right the first time, not the third.

Phase-aware, intent-aware, and zero paid-LLM tokens spent on routing. No generative LLM in the hot path. No remote calls. No containers (unless you want them — `agentalloy setup --deployment container` gives you a single-container deployment). The whole loop runs locally on one 0.6B embed model plus embedded [LadybugDB](https://docs.ladybugdb.com/) + DuckDB.

Things your agent gets composed-and-injected without you pasting them into the prompt:

- "How do I write a failing pytest before the implementation?" — TDD workflow + framework idioms, composed from `pytest` + `testing` packs.
- "How do I structure an incremental dbt model so it stays correct across re-runs?" — data-engineering governance + domain skills, composed from `data-engineering` + `engineering` packs.
- "Wire OpenTelemetry into this FastAPI app." — observability rules + framework patterns, composed from `fastapi` + `analytics` packs.
- "I'm reviewing this PR — what should I check?" — review heuristics, composed phase-aware from `code-review` packs.

**This is what zero-shot agentic development looks like.**

---

## Quick Install

Choose your path:

| I want... | Run this |
|---|---|
| Full control, GPU acceleration, IDE integration | Native install (default) |
| Zero host dependencies, air-gapped / offline | Container deployment |
| Just try it out | [Run the demo](#demo) |

### Native install

```bash
# Step 1: install uv (Linux / macOS)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Step 2: install agentalloy
uv tool install git+https://github.com/nrmeyers/agentalloy.git

# Step 3: configure and wire
agentalloy setup
# select option 1
```

The setup wizard walks you through everything: hardware detection, runner selection (`ollama`, `lm-studio`, or `llama-server`), model and port, service mode, **skill pack selection** (with tier-grouped listing), IDE harness wiring, and hardware target. It then executes all install steps and validates the result. **3–5 minutes** on a warm machine.

> **Note:** If your Ollama instance requires SSH key authentication (e.g., when
> `OLLAMA_HOST` points to a remote instance), you'll need an ed25519 key at
> `~/.ollama/id_ed25519` before running setup. See [docs/troubleshooting.md](docs/troubleshooting.md)
> for details.

Non-interactive / scripted installs: pass flags directly:

```bash
agentalloy setup -n --runner ollama --hardware nvidia --packs all --harness cursor
```

### Container install

```bash
agentalloy setup 
# select options 2
```

Runs agentalloy + Ollama in a single container with `qwen3-embedding:0.6b` auto-pulled on first start. Published images ship a **prebuilt skill corpus** — first run is ready in minutes (model download only), no CPU ingest/embed wait. Port 47950 is the only external surface. Container inference is **CPU-only** on every host; for GPU acceleration (NVIDIA / AMD / Metal) pick the native install instead.

> **Container install pulls a pre-built image from GHCR.** Setup pulls `ghcr.io/nrmeyers/agentalloy:latest` directly — no repo checkout, no build context, and no `git` required. For air-gapped environments, use `--image-path` to deploy from a local tarball.

---

## Contents

- [Quick Install](#quick-install)
- [Demo](#demo)
- [What makes the composition different](#what-makes-the-composition-different)
- [How it works: phases, contracts, signal layer](#how-it-works-phases-contracts-signal-layer)
- [How to use it](#how-to-use-it)
- [Container deployment](#container-deployment)
- [Profiles](#profiles)
- [Harness support](#harness-support)
- [Standalone CLI](#standalone-cli)
- [REST API](#rest-api)
- [MCP Server](#mcp-server)
- [Packs shipping in-tree](#packs-shipping-in-tree)
- [Architecture](#architecture)
- [Telemetry](#telemetry)
- [Configuration](#configuration)
- [Development](#development)
- [Need Help?](#need-help)
- [Contributing](#contributing)
- [Benchmarks](#benchmarks)
- [License](#license)

---

## Demo

```bash
$ curl -s -X POST http://localhost:47950/compose \
    -H 'Content-Type: application/json' \
    -d '{"task": "write a failing pytest", "phase": "build"}' | jq .

{
  "output": "## TDD: write the failing test first\n\nIn pytest, ...",
  "source_skills": ["test-driven-development", "pytest-fixtures"],
  "tokens_returned": 1840,
  "compose_ms": 47
}
```

Your agent calls `/compose`, gets back the relevant raw skill prose, and assembles it inside its own prompt. No LLM-in-the-loop, no token tax, no API key roulette. Sub-50ms p95 on a warm cache.

---

## Container deployment

AgentAlloy can run as a single container (`agentalloy:local`, setup option #2) that bundles the service and its embedding model (Ollama) in one process. This is the recommended deployment for users who want zero host-side inference dependencies.

The setup wizard:

1. **Detects** your container runtime (`podman` preferred, `docker` fallback).
2. **Pulls** the pre-built image from GHCR (`ghcr.io/nrmeyers/agentalloy:latest`).
3. **Creates** a named volume `agentalloy-data` for persistent corpus data.
4. **Runs** the container with volume mounts, env vars, and port mapping.
5. **Waits** for the readiness endpoint (`/readiness`) to respond.

> **Note:** Container install pulls a pre-built image from GHCR — no repo checkout, no build context, and no `git` required. For air-gapped environments, use `--image-path` to deploy from a local tarball.

### Container architecture

```
┌─────────────────────────────────────────────┐
│  agentalloy:local  (podman run --replace)   │
│                                             │
│  /app/entrypoint.sh (bash)                  │
│  ├── Check .bootstrap-complete (skip if done)│
│  ├── Seed prebuilt corpus (published images) │
│  ├── Install Ollama (if missing)             │
│  ├── Start ollama serve --host 127.0.0.1    │
│  ├── Pull qwen3-embedding:0.6b (if missing)  │
│  ├── Run migrations                          │
│  ├── install-packs (skipped when seeded)     │
│  ├── Touch .bootstrap-complete               │
│  ├── exec uvicorn (main service)             │
│                                             │
│  ENV: AGENTIALLOY_PACKS, LADYBUG_DB_PATH     │
│      DUCKDB_PATH, LOG_LEVEL                  │
└───────────┬─────────────────────────────────┘
            │ -p 47950:47950
            ▼
   localhost:47950  (external)

Volume mounts:
  agentalloy-data → /app/data     (corpus, database)
  ~/.ollama       → /root/.ollama (Ollama models)
```

### Volume layout

| Volume / Path | Purpose | Persists across restarts? |
|---|---|---|
| `agentalloy-data:/app/data` | LadybugDB index, DuckDB skills database | Yes (named volume) |
| `~/.ollama:/root/.ollama` | Ollama model cache (`qwen3-embedding:0.6b`) | Yes (host bind mount) |

### Entrypoint bootstrap sequence

The entrypoint script (`/app/entrypoint.sh`) runs on every container start:

1. **Bootstrap check** — If `$APP_DIR/.bootstrap-complete` exists, skip all bootstrap steps and go straight to uvicorn.
2. **Prebuilt corpus seed** — Published images (`ghcr.io/nrmeyers/agentalloy`) carry a fully ingested + embedded corpus under `/app/corpus-seed`; if the data volume has no corpus yet it is copied in (seconds) and step 8 is skipped. Locally built images have no seed and fall through to the full build. The seed's `corpus-stamp.json` (embed model, dim, packs hash, git sha) is copied alongside for auditability.
3. **Ollama install** — If `ollama` binary is missing, download and run the official install script.
4. **Start Ollama** — Launch `ollama serve --host 127.0.0.1:11434` in the background. (Required even with a seeded corpus — query embedding happens at compose time.)
5. **Wait for ready** — Poll `http://127.0.0.1:11434` for up to 30 seconds.
6. **Pull model** — If `qwen3-embedding:0.6b` is not cached, pull it from the Ollama library.
7. **Run migrations** — Execute `python -m agentalloy.migrate` to initialize database schemas.
8. **Install packs** — Skipped when the corpus was seeded in step 2. Otherwise: if `AGENTIALLOY_PACKS` is set, run `install-packs --packs <packs>` (this is the slow path — ~3,000 fragments embedded on CPU).
9. **Flag complete** — Touch `$APP_DIR/.bootstrap-complete`.
10. **Start service** — `exec uvicorn agentalloy.api:create_app --host 0.0.0.0 --port 47950`.

Steps 2–8 are skipped on subsequent starts (idempotent bootstrap). First-run wall clock: published image ≈ model download only (~5–10 min on a typical connection); locally built image adds the corpus build (20+ min on CPU).

### Operational commands

<details><summary>Click to expand — full operational command reference</summary>

```bash
# Start the container (first-time setup)
agentalloy setup --deployment container

# View logs
podman logs agentalloy
podman logs -f agentalloy          # follow
podman logs --tail 100 agentalloy  # last 100 lines

# Check health
curl http://localhost:47950/health

# Inspect container
podman inspect agentalloy
podman ps --filter name=agentalloy

# Exec into the container
podman exec -it agentalloy sh

# Restart the container
podman restart agentalloy

# Stop and remove
podman stop agentalloy
podman rm -f agentalloy

# Inspect volumes
podman volume inspect agentalloy-data

# Re-embed corpus in the container
podman exec agentalloy uv run agentalloy reembed

# Install skill packs in the container
podman exec agentalloy uv run agentalloy install-packs --packs all

# Suppress restart after pack install
podman exec agentalloy uv run agentalloy install-packs --packs all --no-restart
```

</details>

### Hardware requirements

Container deployment is **CPU-only** on every host. GPU acceleration (NVIDIA CUDA, AMD ROCm, Apple Metal) only works with a native install. The bundled Ollama runs on CPU using `qwen3-embedding:0.6b` — functional for embeddings but slower than GPU.

| Requirement | Minimum |
|---|---|
| RAM | 8 GB |
| Disk (image + model + data) | ~4 GB |
| Container runtime | Podman (recommended) or Docker |

---

## What makes the composition different

- **Composed per task, not loaded every turn.** A skill that's irrelevant to the current task isn't in the prompt at all — RRF + applicability filtering picks the right subset for each request.
- **Three instruction sets, fused.** Governance, workflow, and domain skills are composed together into one persona — not three files the agent has to reconcile on its own.
- **Phase-aware.** Build-phase skills weight differently than QA-phase or review-phase skills. The same task gets a different composition at different points in the lifecycle.
- **Hybrid retrieval, not lexical-only.** Token-literal queries (`"JWT"`, `"Prisma"`) hit BM25; semantic queries ("the auth handler") hit a 1024-dim dense leg. Phase-tuned Reciprocal Rank Fusion picks the better signal per query.
- **No model variance.** Embeddings + lexical match + deterministic fusion. Same task → same composition, regardless of which agent model you swap in tomorrow.
- **Versioned & validated.** Every skill is sourced from authoritative upstream docs and validated against the R1–R8 quality contract; reviewable history under `docs/skill-review-history/`.

---

## How it works: phases, contracts, signal layer

<details><summary>Click to expand — deep dive into the signal layer internals</summary>

Three small artifacts on disk drive everything AgentAlloy does. None of them belong to your agent's prompt — they're state files that the signal layer reads.

### 1. The phase file

```
.agentalloy/phase       →  phase: build
```

A sticky, one-line YAML file under your project. Tracks where the agent is in the SDD lifecycle: `spec → design → build → qa → ship`. Each phase has a corresponding **workflow skill** (e.g., `sdd-build`) that ships persona prose and a set of declarative **exit gates**. When the agent enters a phase, that workflow skill's prose is injected as the persona for the duration; when the exit gates pass, the phase advances and the next workflow skill takes over.

### 2. Task contracts

```
.agentalloy/contracts/build/add-auth-middleware.md
```

A short markdown file the agent writes when starting a task. The frontmatter declares intent:

```yaml
---
phase: build
task_slug: add-auth-middleware
domain_tags: ["NestJS", "Express middleware", "JWT validation"]
scope:
  touches: ["src/auth/**", "tests/auth/**"]
  avoids:  ["src/billing/**"]
success_criteria:
  - "Existing auth tests still pass"
  - "Middleware tested with valid + invalid tokens"
---

# Add Auth Middleware
<one paragraph of task prose>
```

The agent writes the contract once at task start. From then on, **`domain_tags` is the BM25 input for retrieval** — surgical, intent-aware, and stable across the conversation. No prompt engineering required; the agent just records what it's about to do.

### 3. The signal layer

A small Python module that wakes on three kinds of events: a user prompt arrives, a contract file is written, a tool is about to fire. It runs a cheap **pre-filter** (signal keywords, file-event scope checks) to decide if anything needs to happen. If nothing matches, it returns silently — no tokens spent, no injection. If something matches, it evaluates the active phase's **exit gates** (deterministic predicates like `artifact_exists`, `git_state`, `contract_has_tags`, plus a few semantic ones that cosine-similarity-score the prompt against named intents using the same 0.6B embed server). When gates pass, the phase file is updated atomically and the next workflow skill's prose is emitted as pre-prompt context.

```
        ┌───────────────────┐
        │  prompt / event   │
        └─────────┬─────────┘
                  ▼
        ┌───────────────────┐
        │   pre-filter      │ ── no match ──► silent exit
        │   (cheap)         │
        └─────────┬─────────┘
                  ▼ match
        ┌───────────────────┐
        │  evaluate gates   │
        │  (deterministic + │
        │   cosine sim)     │
        └─────────┬─────────┘
                  ▼
   ┌──────────────┴──────────────┐
   │                             │
   ▼                             ▼
phase transition          system skill fires
  → next workflow            (commit-safety,
    skill injected            secret-handling,
                              etc.)
```

Everything between the agent and the embed model is deterministic Python. Zero paid-LLM tokens spent on "where am I?", "what should I be doing?", or "should I call AgentAlloy now?"

</details>

---

## How to use it

Three paths, depending on how your harness integrates with external tools.

### Standalone HTTP service

Run AgentAlloy on its own port; your agent (or your script, or your CI) calls `POST /compose` and reads the response. Zero coupling to a specific harness — works with anything that can hit an HTTP endpoint.

```bash
python -m agentalloy                  # default :47950
curl -s http://localhost:47950/health # {"status":"ok"}
```

### Wired into a proxy-wired harness (full integration)

If your harness honors a custom API base URL (OpenAI / Anthropic / a config-file `apiBase`), AgentAlloy points it at the local proxy. Every LLM request flows through the proxy, which injects skill context, evaluates gates, and forwards to the real upstream. Phase transitions, contract retrieval, and system skill enforcement all happen automatically.

```bash
agentalloy wire --harness <name>
```

### Wired into a sidecar harness

A few harnesses (Cursor, Windsurf, GitHub Copilot, Gemini CLI) route through their own backends and can't be intercepted. For those, AgentAlloy writes a static rules file and a file-watching sidecar regenerates that file within ~1s of a phase or contract change. You start the sidecar once per session:

```bash
agentalloy wire --harness <name>
agentalloy watch start --harness <name>
```

The capability matrix and a fuller picture live in [Harness support](#harness-support) below.

---

## Profiles: user-scoped skill contexts

Profiles let you maintain separate skill contexts for different kinds of work — e.g., a `work` profile with stricter CI gates and team governance rules, a `personal` profile with relaxed constraints and hobby-project domain skills. Profiles auto-resolve per-repo based on git remote URL, filesystem path, or an explicit project marker, so you never need to switch them manually.

This is the key difference from `AGENTS.md` / `SKILL.md` approaches:

- **AgentAlloy install is one-time and user-scoped.** A single install serves all your projects. State lives under `~/.config/agentalloy/` and data under `~/.local/share/agentalloy/`.
- **Profiles determine skill overrides per-repo.** Configure once, and the active profile resolves automatically when you `cd` between projects.
- **Wiring is still per-repo.** Each project needs `agentalloy wire` to inject sentinels into its harness config files (`.cursor/rules/`, `.clinerules`, etc.), but the skills those sentinels reference come from the user-scoped profile.

See [profiles-and-overrides.md](docs/profiles-and-overrides.md) for full details.

---

## Harness support

Harnesses fall into two categories:

- **Proxy-wired** (Claude Code, Continue.dev, Aider, Cline, OpenCode, Hermes Agent) — full per-turn integration via the local proxy. The proxy intercepts LLM traffic, injects skill context, and evaluates gates automatically.
- **Sidecar** (Cursor, Windsurf, GitHub Copilot, Gemini CLI) — static rules file kept current by a file watcher. Reduced capability: no enforcement, advisory text only.

Proxy-wired is the preferred mode. Full per-harness catalog: [docs/install/harness-catalog.md](docs/install/harness-catalog.md).

---

## Standalone CLI

The `agentalloy` CLI handles install, service management, phase control, and composition. Key commands:

```bash
agentalloy setup                          # Interactive install wizard
agentalloy wire --harness <name>          # Wire a harness (or --mcp-fallback)
agentalloy serve                          # Run the service
agentalloy phase get|set|clear            # Manage project phase
agentalloy compose --contract <path>      # One-shot composition
agentalloy doctor                         # Diagnose install issues
```

Full command reference: [docs/operator.md](docs/operator.md).

Each subcommand emits structured JSON on stdout; pair with `jq` for scripting.

---

## REST API

AgentAlloy serves both OpenAI-compatible and Anthropic Messages API endpoints through the proxy:

- `POST /v1/chat/completions` — OpenAI-compatible proxy
- `POST /v1/messages` — Anthropic Messages API proxy (Claude Code, Cline)
- `POST /compose` — Manual skill composition
- `GET /health` — Liveness probe

See [proxy-architecture.md](docs/proxy-architecture.md) for the full endpoint list and request/response schemas.

---

## MCP Server

AgentAlloy ships a built-in MCP server for harnesses that support the Model Context Protocol. Instead of proxying LLM traffic, the MCP server exposes a single tool the harness calls on demand:

- **`get_skill_for(task, phase)`** — forwards to the local `/compose` endpoint and returns composed skill fragments.

The server is dependency-free (no MCP SDK) and runs via stdio JSON-RPC (MCP 2024-11-05 spec).

```bash
# Wire with MCP fallback instead of proxy:
agentalloy wire --harness cursor --mcp-fallback
```

Supported harnesses: Claude Code, Cursor, Continue.dev. See [Harness Catalog § MCP Fallback](docs/install/harness-catalog.md) for per-harness configuration details.

---

## Packs shipping in-tree

The corpus is **packs** — opt-in groups of related skills. `main` ships **35+ packs / 300+ declared skills** organized across 9 tiers:

<table>
<tr><th>Tier</th><th>Packs</th></tr>
<tr><td><b>foundation</b></td><td><code>core</code> · <code>documentation</code> · <code>engineering</code> · <code>performance</code> · <code>refactoring</code></td></tr>
<tr><td><b>language</b></td><td><code>csharp-dotnet</code> · <code>go</code> · <code>java</code> · <code>nodejs</code> · <code>python</code> · <code>rust</code> · <code>typescript</code></td></tr>
<tr><td><b>framework</b></td><td><code>fastapi</code> · <code>fastify</code> · <code>nestjs</code> · <code>nextjs</code> · <code>react</code> · <code>vue</code></td></tr>
<tr><td><b>tooling</b></td><td><code>linting</code> · <code>pytest</code> · <code>testing</code></td></tr>
<tr><td><b>workflow</b></td><td><code>code-review</code> · <code>design-review</code> · <code>intake</code> · <code>sdd</code></td></tr>
<tr><td><b>domain</b></td><td><code>analytics</code> · <code>data-engineering</code> · <code>ui-design</code></td></tr>
<tr><td><b>platform</b></td><td><code>github-actions</code></td></tr>
<tr><td><b>protocol</b></td><td><code>rest</code> · <code>webhooks</code></td></tr>
<tr><td><b>store</b></td><td><code>redis</code> · <code>redshift</code> · <code>snowflake</code> · <code>temporal</code></td></tr>
</table>

Every skill is sourced from authoritative upstream docs and validated against the **R1–R8 quality contract** in `src/agentalloy/_packs/meta/sys-skill-authoring-rules.md`. Each pack ships with `.qa.md` reports under `docs/skill-review-history/` documenting independent Critic verdicts.

Pack authoring lives in a separate repo and tooling — see [agentalloy-authoring](../agentalloy-authoring). It uses a local-first author-critic pipeline that produces validated YAML packs; nothing about authoring is required to *use* AgentAlloy at runtime.

---

## Architecture

AgentAlloy is a three-layer system:

1. **Signal layer** — deterministic Python that wakes on phase transitions, contract writes, or tool fires. Pre-filters cheaply, evaluates exit gates, and composes skills only when needed.
2. **Composition engine** — hybrid BM25 + dense retrieval over LadybugDB (skill graph) and DuckDB (vector index), fused via phase-tuned Reciprocal Rank Fusion.
3. **Proxy** — OpenAI-compatible and Anthropic Messages API endpoints that intercept harness traffic, inject composed skills, and forward to the upstream LLM.

Zero generative LLM in the runtime path. See [docs/proxy-architecture.md](docs/proxy-architecture.md) for the full design.

---

## Telemetry

Every `/compose`, `/retrieve`, and signal evaluation writes a structured trace to DuckDB before the response returns — no async backlog, no dropped traces. Trace-write failures never propagate.

Query via `GET /telemetry/traces` or `agentalloy telemetry`. See [docs/operator.md](docs/operator.md) for the full trace schema and filter options.

---

## Configuration

Runtime environment variables are written automatically by `agentalloy write-env` to `~/.config/agentalloy/.env`. Key variables:

- `RUNTIME_EMBED_BASE_URL` — embedding endpoint (default: Ollama at `localhost:11434`)
- `RUNTIME_EMBEDDING_MODEL` — embedding model (default: `qwen3-embedding:0.6b`)
- `PROFILE_ROOT` — per-profile datastores
- `DEDUP_HARD_THRESHOLD` / `DEDUP_SOFT_THRESHOLD` — cosine dedup thresholds
- `BOUNCE_BUDGET` — compose retry budget

See [docs/operator.md](docs/operator.md) for the full configuration reference.

---

## Development

```bash
uv sync                          # install deps
uv run ruff check .              # lint
uv run ruff format --check .     # format
uv run pyright                   # types
uv run pytest                    # unit tests (fast)
uv run pytest -m integration     # integration — requires Ollama with qwen3-embedding:0.6b
```

Tests live under `tests/` and cover the install pipeline (`tests/install/`), retrieval, composition, applicability filtering, telemetry, and the harness-wiring catalog.

---

## Need Help?

- [Installation guide](docs/install/) — step-by-step setup for each harness
- [Operator guide](docs/operator.md) — CLI reference, service management
- [Troubleshooting](docs/troubleshooting.md) — common errors and fixes
- [Discussions](https://github.com/nrmeyers/agentalloy/discussions) — ask questions, share setups

---

## Contributing

To contribute to the AgentAlloy codebase, use an editable install so your changes are reflected immediately:

```bash
git clone https://github.com/nrmeyers/agentalloy.git
cd agentalloy
uv sync
uv tool install --editable .
```

### Migrating from pipx

If you previously installed AgentAlloy via `pipx`, migrate to `uv`:

```bash
pipx uninstall agentalloy        # remove the legacy install
uv tool install git+https://github.com/nrmeyers/agentalloy.git
```

User-scope state (`~/.config/agentalloy/`, corpus DB) is preserved across the swap — pipx and uv installs share the same state location.

---

## Benchmarks

Measured on a 4-model × 3-condition matrix (composed / flat / no skills; 10 generic + 8 domain pre-registered tasks, 3 seeded runs per cell). Results that stand out:

- **On domain tasks, composed injection lifted every model (+0.07 to +0.19) and landed within 0.01–0.03 of a hand-picked oracle injection** — automatic skill selection is doing the job a human curator would.
- **A 12B dense model with composed skills tied a 27B dense model on domain tasks (0.975 vs 0.971) at 2.5× the speed.** Bare, the 12B scored 0.906 — composition moved it up a weight class, on hardware a 27B doesn't fit.
- **A 1.5B-active edge model with composed skills matched a bare 27B dense model on generic tasks — at 12.6× the speed.** LFM2.5-8B-A1B + composed scored 0.850 vs the bare Qwen3.6-27B's 0.855, in 42s vs 534s for the same workload. That quality runs on a laptop or NPU; the 27B doesn't.
- **Composed injection made the edge model better *and* faster than itself**: +0.05 quality over its own no-skill baseline while finishing 29% faster — focused skill prose cut its output rambling nearly in half. Flat injection of the same skills delivered zero lift on that model.

Full matrix, methodology, and caveats in [BENCHMARKS.md](BENCHMARKS.md).

---

## License

MIT. See [LICENSE](LICENSE).
