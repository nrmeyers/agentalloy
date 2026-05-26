<p align="center">
  <img src="AgentAlloy_cover.png" alt="AgentAlloy — Just-in-Time Instruction Composer" width="720" />
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

Phase-aware, intent-aware, and zero paid-LLM tokens spent on routing. No generative LLM in the hot path. No remote calls. No containers (unless you want them). The whole loop runs locally on one 0.6B embed model plus embedded [LadybugDB](https://docs.ladybugdb.com/) + DuckDB. In our proof-of-concept: **60% smaller prompts, 25% faster runs, same model — and answers improve, not degrade** (see [Empirical results](#empirical-results)).

Things your agent gets composed-and-injected without you pasting them into the prompt:

- "How do I write a failing pytest before the implementation?" — TDD workflow + framework idioms, composed from `pytest` + `testing` packs.
- "How do I structure an incremental dbt model so it stays correct across re-runs?" — data-engineering governance + domain skills, composed from `data-engineering` + `engineering` packs.
- "Wire OpenTelemetry into this FastAPI app." — observability rules + framework patterns, composed from `fastapi` + `analytics` packs.
- "I'm reviewing this PR — what should I check?" — review heuristics, composed phase-aware from `code-review` packs.

**This is what zero-shot agentic development looks like.**

---

## Contents

- [Quickstart](#quickstart)
- [Demo](#demo)
- [What makes the composition different](#what-makes-the-composition-different)
- [How it works: phases, contracts, signal layer](#how-it-works-phases-contracts-signal-layer)
- [How to use it](#how-to-use-it)
- [Profiles](#profiles)
- [Harness support](#harness-support)
- [Standalone CLI](#standalone-cli)
- [REST API](#rest-api)
- [Hardware presets](#hardware-presets)
- [Packs shipping in-tree](#packs-shipping-in-tree)
- [Architecture](#architecture)
- [Telemetry](#telemetry)
- [Configuration](#configuration)
- [Development](#development)
- [Empirical results](#empirical-results)
- [License](#license)

---

## Quickstart

Install the CLI. 

Step 1. Choose the path that fits you:

A) **Production** — standalone install, no repo needed (most common):

```bash
pipx install git+https://github.com/nrmeyers/agentalloy.git
```
...Or

B) **Local development / contribution to AgentAlloy codebase** — editable install, reflects source changes instantly:

```bash
git clone https://github.com/nrmeyers/agentalloy.git && cd agentalloy
uv sync
uv tool install --editable .
```

Step 2. Run the setup wizard and you're ready to use it:

```bash
agentalloy setup                                # one-time interactive install wizard
cd ~/your-project && agentalloy wire            # wire harness in this repo
```

The setup wizard walks you through everything: hardware detection, runner selection (`ollama`, `lm-studio`, or `llama-server`), model and port, service mode, **skill pack selection** (with tier-grouped listing), IDE harness wiring, and hardware target. It then executes all install steps and validates the result. **3–5 minutes** on a warm machine.

The pack selection screen groups packs by tier (Foundation, Languages, Frameworks, Tooling, etc.) and marks always-on packs. Select by pack name, tier name (e.g., `foundation`, `languages`), `all`, or leave blank for always-on packs only. You can always add more packs later with `agentalloy install-pack <name>`.

Non-interactive / scripted installs: pass flags directly:

```bash
agentalloy setup -n --runner ollama --hardware nvidia --packs all --harness cursor
```

**Agent-driven install.** If you'd rather have your coding harness drive the install for you, clone the repo and tell it:

```bash
git clone https://github.com/nrmeyers/agentalloy.git && cd agentalloy
# then in your coding harness:
> Install this tool by following INSTALL.md
```

Works in any of the [supported harnesses](#harness-support).

**Container alternative.** Build and run via your container tooling — see [INSTALL.md](INSTALL.md) for container setup details.

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

## What makes the composition different

- **Composed per task, not loaded every turn.** A skill that's irrelevant to the current task isn't in the prompt at all — RRF + applicability filtering picks the right subset for each request. 60% smaller prompts on average vs. flat injection (see [Empirical results](#empirical-results)).
- **Three instruction sets, fused.** Governance, workflow, and domain skills are composed together into one persona — not three files the agent has to reconcile on its own.
- **Phase-aware.** Build-phase skills weight differently than QA-phase or review-phase skills. The same task gets a different composition at different points in the lifecycle.
- **Hybrid retrieval, not lexical-only.** Token-literal queries (`"JWT"`, `"Prisma"`) hit BM25; semantic queries ("the auth handler") hit a 1024-dim dense leg. Phase-tuned Reciprocal Rank Fusion picks the better signal per query.
- **No model variance.** Embeddings + lexical match + deterministic fusion. Same task → same composition, regardless of which agent model you swap in tomorrow.
- **Versioned & validated.** Every skill is sourced from authoritative upstream docs and validated against the R1–R8 quality contract; reviewable history under `docs/skill-review-history/`.

---

## How it works: phases, contracts, signal layer

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

Classification depends on a single question: can the harness's LLM traffic be intercepted by the AgentAlloy proxy? Harnesses that honor a custom API base URL get full per-turn integration. Harnesses that route through their own backend fall back to a static rules file kept current by a watcher.

| Capability | Proxy-wired | Sidecar (no proxy support) |
|---|---|---|
| Initial workflow skill context | ✅ | ✅ |
| Phase transition detection (automatic) | ✅ Per-turn via proxy | ✅ Sidecar auto-detects via file watcher (when running); manual fallback via `agentalloy phase set <name>` |
| System skill enforcement (gates) | ✅ Proxy can block/modify request | ⚠️ Advisory text only — no enforcement |
| Mid-session context updates | ✅ Injected into next request | ⚠️ Requires file reload (harness-dependent) |
| Contract → skill injection | ✅ Per-turn via proxy | ✅ Sidecar (`agentalloy watch start`) |
| Semantic gate evaluation | ✅ Runs per-turn | ⚠️ Falls back to `UNKNOWN` without proxy |

**Sidecar mode is a real reduction in capability.** Without proxy interception, system skills become suggestions rather than gates, and phase transitions are automatic only when the sidecar watcher is running; otherwise manual via `agentalloy phase set <name>`. If you need enforcement, prefer a proxy-wired harness. See [`docs/sidecar-experience.md`](docs/sidecar-experience.md) for sidecar setup details and [`docs/harness-classification.md`](docs/harness-classification.md) for the full classification spec.

Current membership (lists evolve as harness vendors expose base-URL overrides — check `agentalloy wire --harness <name>` for current support):

- **Proxy-wired**: Claude Code, Continue.dev, Aider, Cline, OpenCode, Hermes Agent
- **Sidecar**: Cursor, Windsurf, GitHub Copilot, Gemini CLI

Full per-harness catalog: [`docs/install/harness-catalog.md`](docs/install/harness-catalog.md).

---

## Standalone CLI

The `agentalloy.install` module exposes a single CLI with subcommands. All write paths are user-scoped (LadybugDB and pack drafts live under `user_config_dir()`).

**Install & lifecycle**

| Command | Description |
|---|---|
| `setup` | End-to-end interactive install (calls every step below in order). |
| `preflight` | Gate prereqs (Python, runtimes, ports) before any state changes. |
| `pull-models` | Pull / verify the embedding model is loaded into your backend. |
| `start-embed-server` | Start the embedding backend (llama-server or Ollama) before pack install. |
| `seed-corpus` | One-shot pack ingestion into LadybugDB + DuckDB. |
| `install-packs [--packs <names>] [--list]` | Install/refresh specific pack(s), or `--list` to see what's available. |
| `install-pack [--pack <name>]` | Install a single pack by name. |
| `reembed` | Recompute embeddings for unembedded or updated LadybugDB fragments. |
| `wire [--harness <name>]` | Auto-detect the harness in the current repo and inject sentinels (or pass `--harness` to force). |
| `wire-harness --harness <name>` | Lower-level: explicit harness wiring with full flag control. |
| `unwire` | Remove AgentAlloy sentinels from the current repo (keeps user state). |
| `write-env` | Write `.env` with the resolved backend / model / paths. |
| `update` | Pull the latest packs and re-seed. |
| `uninstall` | Cross-repo sentinel cleanup, optional data-dir wipe. |
| `reset-step <step>` | Roll back one step of an in-progress install. |

**Service**

| Command | Description |
|---|---|
| `serve` | Run the service in the foreground (uvicorn). |
| `server-start` / `server-stop` / `server-restart` / `server-status` | Manage the background FastAPI daemon on :47950. |
| `enable-service` | Register AgentAlloy as a persistent background service (systemd-user / launchd). |
| `status` | Show user-scope install state, wired repos, and service reachability. |
| `verify` | Run post-install integrity checks (corpus count, harness sentinels, port). |
| `doctor` | Diagnose a partial / broken install. |

**Phases, contracts, signal layer**

| Command | Description |
|---|---|
| `phase {get,set,clear}` | Read / write `.agentalloy/phase`. `set` is a manual override / sidecar fallback when the watcher is not running. |
| `contract {write,validate,list}` | Create or validate task contracts under `.agentalloy/contracts/<phase>/`. |
| `signal evaluate-phase` | Fire the pre-filter + gate evaluator; emits the next workflow skill's prose if a transition occurs. Invoked by the proxy per request (proxy-wired harnesses). |
| `signal evaluate-system --tool <name>` | Find system skills whose `applies_when` matches a tool that's about to fire. |
| `signal watch-contract --path <p>` | Validate a contract and trigger composition. |
| `signal check` | Diagnostics: dump current phase + active workflow skill + pre-filter state. |
| `compose --contract <path> [--inject]` | One-shot composition from a contract file. Used by hook scripts; can also be called directly. |

**Profiles & customization**

| Command | Description |
|---|---|
| `profile {list,active,create,use}` | Per-profile datastores and skill overrides (e.g., `work` vs `personal`). Auto-detected from cwd via git remote, path prefix, or explicit project marker. See docs/profiles-and-overrides.md. |
| `customize {list,edit,validate,update,diff,reset}` | Three-layer skill overrides (project → profile → shipped default). Edit a skill's prose, gates, or applicability for your project or profile without forking. See docs/profiles-and-overrides.md § Three-layer overrides. |
| `reset` | Wipe profile overrides and re-ingest shipped defaults. |

**Sidecar (for harnesses that can't be proxy-wired)**

| Command | Description |
|---|---|
| `watch start --harness <name>` | Start the file-watching sidecar for sidecar harnesses (Cursor, Windsurf, GitHub Copilot, Gemini CLI). |
| `watch stop` | Stop the sidecar. |
| `watch status` | Report whether the sidecar is running and where its log lives. |

**Telemetry**

| Command | Description |
|---|---|
| `telemetry` | Query / inspect composition traces from the CLI. |

Each subcommand emits structured JSON on stdout; pair with `jq` for scripting.

---

## REST API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/compose` | Hybrid retrieve + assemble. Returns JSON: `{output, source_skills, tokens_returned, compose_ms}`. |
| `POST` | `/compose/text` | Same as `/compose` but returns `text/plain` — paste-ready for harnesses that take freeform context. |
| `POST` | `/retrieve` | Retrieve only (no assembly). Returns ranked fragment IDs + scores + provenance. |
| `GET` | `/retrieve/{skill_id}` | Look up a single skill's retrievable fragments by id. |
| `GET` | `/skills/{skill_id}` | Inspect a skill — versions, fragments, applicability rules. |
| `GET` | `/telemetry/traces` | Composition trace history. See [Telemetry](#telemetry). |
| `GET` | `/health` | `{"status":"ok"}` liveness probe. |
| `GET` | `/diagnostics/runtime` | Backend / model / DB state for debugging. |

Request body for `/compose`:

```json
{
  "task": "<one-sentence task description>",
  "phase": "spec" | "design" | "build" | "qa" | "ship",
  "domain_tags": ["postgres", "fastapi"],     // optional hard filter
  "contract_path": ".agentalloy/contracts/build/<slug>.md",  // optional — overrides task + tags
  "contract_tags": ["NestJS", "JWT"]          // optional — explicit tags without a contract file
}
```

When `contract_path` is provided, AgentAlloy parses the contract's frontmatter and uses `domain_tags` as the BM25 input — the surgical, intent-aware path. When neither contract field is present, AgentAlloy rule-extracts keywords from `task` as a fallback.

---

## Hardware presets

The runtime needs only an embedding service. The install agent picks one for you, but here's the matrix:

| Preset | Hardware | Backend | VRAM / RAM |
|---|---|---|---|
| `cpu` | x86_64 / ARM64 | Ollama (CPU) | 8 GB RAM |
| `apple-silicon` | M1 / M2 / M3 / M4 | Ollama (Metal) | 8 GB unified |
| `nvidia` | NVIDIA + CUDA | Ollama (CUDA) | 4 GB VRAM |
| `radeon` | AMD Radeon dGPU/iGPU | LM Studio (Vulkan) | 4 GB VRAM |

All presets use **`qwen3-embedding:0.6b`** at 1024 dimensions. Default ports per runner: Ollama `localhost:11434`, LM Studio `localhost:1234`, llama-server `localhost:11434`. The on-disk index is portable across backends — switching is an env-var flip.

```bash
# Ollama presets
ollama pull qwen3-embedding:0.6b

# Radeon: open LM Studio → search qwen3-embedding:0.6b → Q8 → start server
```

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

```
                    ┌──────────────────────────────────────────────┐
                    │   paid LLM (your coding agent)               │
                    └────────┬──────────────────────────┬──────────┘
                             │                          │
                  writes contract                executes workflow
                  per phase task                 skill instructions
                             │                          │
                             ▼                          ▼
                  ┌──────────────────────┐    ┌──────────────────────┐
                  │ .agentalloy/         │    │ .agentalloy/phase    │
                  │  contracts/<phase>/  │    │ (sticky)             │
                  └──────────┬───────────┘    └─────────┬────────────┘
                             │                          │
                file-write event              prompt / file pre-filter
                             │                          │
                             ▼                          ▼
                  ┌──────────────────────┐    ┌──────────────────────┐
                  │ /compose             │    │ signal layer         │
                  │ (deterministic)      │    │ deterministic + cosine
                  │                      │    │ similarity            │
                  └──────────┬───────────┘    └─────────┬────────────┘
                             │                          │
              hybrid BM25 + dense over            transition? → write
              LadybugDB + DuckDB                  phase, emit next
                             │                    workflow skill
                             ▼                          │
                       composed prose ◄────────────────┘
                             │
                             ▼
                  Proxy-wired: injected into next LLM request
                  Sidecar: file watcher rewrites rules file
```

**Data plane** (the two embedded stores):

| Store | Role |
|---|---|
| **DuckDB** | 1024-dim vector index • BM25 FTS index • composition traces |
| **LadybugDB** (embedded [kuzu](https://kuzudb.com/)) | Skill / Version / Fragment / Pack graph — "what skill means and how its pieces relate" |

**Components**

- **Embedding** — `qwen3-embedding:0.6b` (1024-dim). Backend-agnostic via OpenAI-compatible `/v1/embeddings`.
- **Retrieval** — hybrid BM25 + dense cosine fused via Reciprocal Rank Fusion with phase-specific leg weighting. Contract `domain_tags` drive BM25 when present.
- **Applicability filter** — deterministic rule predicates on `ActiveSkill` records (always_apply, phase_scope, category_scope). No LLM parsing.
- **Signal layer** — pre-filter (keywords + file-event scope) → exit-gate evaluation (deterministic predicates + cosine-similarity gates) → atomic phase write + workflow-skill prose emission. Soft-fails everywhere; failure never blocks the agent.
- **Telemetry** — every `/compose`, `/retrieve`, and signal evaluation writes a structured trace to DuckDB inline-before-response. See [Telemetry](#telemetry).
- **Single-model runtime** — `qwen3-embedding:0.6b` does both retrieval embeddings *and* semantic gate scoring (cosine similarity against reference phrase sets). No second model, no chat classifier, no Docker.
- **No generative LLM in the runtime path.** The agent owns generation; AgentAlloy owns retrieval and routing.

---

## Telemetry

Every `/compose`, `/retrieve`, and signal evaluation writes a structured trace to DuckDB **before the response returns** — no async backlog, no dropped traces. Trace-write failures are logged but never propagate; the response always succeeds regardless of telemetry state.

Each trace captures: `trace_id`, `request_ts`, `phase`, `task_prompt`, `status`, `selected_fragment_ids`, `source_skill_ids`, `system_skill_ids`, `workflow_skill_ids`, `retrieval_latency_ms`, `assembly_latency_ms`, `total_latency_ms`, `response_size_chars`, and (on failure) `error_code`. Signal-layer evaluations additionally capture: `event_type` (`phase_eval` / `phase_transition` / `system_skill_applied` / `contract_retrieval`), `pre_filter_matched` (which signal triggered the evaluation), `gates_met`, `gates_unmet`, and `qwen_calls` (number of embed-server calls made during gate evaluation).

Query via `GET /telemetry/traces` with optional filters:

| Filter | Type | Purpose |
|---|---|---|
| `phase` | string | `spec` / `design` / `build` / `qa` / `ship` |
| `status` | string | success / error / degraded result type |
| `event_type` | string | `compose` / `phase_eval` / `phase_transition` / `system_skill_applied` / `contract_retrieval` |
| `since`, `until` | epoch ms | time-range window |
| `limit`, `offset` | int | pagination (1 ≤ limit ≤ 500, default 50) |

Use it to inspect which skills got composed for a task, profile retrieval latency across phases, or audit governance-rule applicability over time. Traces live in the same DuckDB file as the vector index (`DUCKDB_PATH`).

---

## Configuration

Runtime environment variables (written automatically by `agentalloy write-env` to `~/.config/agentalloy/.env`):

| Variable | Default | Purpose |
|---|---|---|
| `RUNTIME_EMBED_BASE_URL` | `http://localhost:11434` | Embedding endpoint (Ollama default; 1234 for LM Studio) |
| `RUNTIME_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Embedding model — used for both retrieval and semantic gate scoring |
| `LADYBUG_DB_PATH` | `~/.local/share/agentalloy/corpus/ladybug` | Legacy LadybugDB (fallback if profile datastore doesn't exist) |
| `DUCKDB_PATH` | `~/.local/share/agentalloy/corpus/skills.duck` | Legacy DuckDB (fallback; profiles use `profiles/<name>/skills.duck`) |
| `PROFILE_ROOT` | `~/.local/share/agentalloy/profiles` | Profile root (per-profile datastores live here) |
| `FORCED_PROFILE` | _(unset)_ | Override profile auto-detection (useful for tests) |
| `DEDUP_HARD_THRESHOLD` | `0.92` | Dedup hard cosine threshold |
| `DEDUP_SOFT_THRESHOLD` | `0.80` | Dedup soft cosine threshold |
| `BOUNCE_BUDGET` | `3` | Compose retry budget |
| `LOG_LEVEL` | `INFO` | Log verbosity |

The `.env` file is user-scoped (written to `~/.config/agentalloy/.env`), not per-repo. Re-run `agentalloy setup` or `agentalloy write-env` to regenerate it with the current config.

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

## Empirical results

See `docs/experiments/poc-composed-vs-flat.md` §13. Headline:

> **60% smaller prompts. 25% faster runs. Same model — and answers improve, not degrade.**

Reproduce: `AGENT_MODEL=<your-agent-model> uv run python -m eval.run_poc --n 3` (requires running AgentAlloy + the agent model loaded locally).

---

## License

MIT. See [LICENSE](LICENSE).
