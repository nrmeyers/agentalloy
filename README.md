<p align="center">
  <img src="AgentAlloy_cover.webp" alt="AgentAlloy — Just-in-Time Context Engine" width="720" />
</p>

<p align="center">
  <b>Instructions tell your agent how to work here. The code index tells it what's actually here.<br/>AgentAlloy composes the first into your agent's context — and teaches it to query the second.</b>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/nrmeyers/agentalloy?color=blue" alt="license" /></a>
  &nbsp;
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="python 3.12+" />
  &nbsp;
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv" /></a>
  &nbsp;
  <img src="https://img.shields.io/badge/runtime-deterministic--by--default-success" alt="deterministic by default" />
  &nbsp;
  <img src="https://img.shields.io/badge/packs-35+-orange" alt="35+ packs" />
  &nbsp;
  <img src="https://img.shields.io/badge/skills-300+-orange" alt="300+ skills" />
</p>

Coding agents don't fail for lack of intelligence — they fail for lack of **context**: the rules of your shop, the skills your stack demands, and the ground truth of the code that's already there. `AGENTS.md`, `SKILL.md`, and giant static system prompts were a clever first attempt at supplying it — and they're already breaking. They load once at session start, then rot as the conversation drifts from the script; reloading them every turn just trades drift for token waste. The real problem is structural: over a single session, what your agent needs to know changes dozens of times, and static files can't keep up.

**AgentAlloy** is a **just-in-time context engine**: one local service, two context modules.

- **Instructions** — knows *how you work*. A signal layer watches for the moments that matter — a new task, a phase change, a meaningful file edit — and composes the governance rules, workflow guidance, and domain skills (from a curated 300+ skill corpus) that fit *this* moment. Nothing changed means nothing injected.
- **Code** — knows *what's there*. A local code-intelligence service: your repos parsed into a symbol graph with hybrid semantic/lexical search — exact call graphs ("what breaks if I change this?") and budgeted context bundles. The agent **queries it**; nothing is pushed. The composed instructions teach the agent when to ask: check blast radius at design, pull a grounded bundle at build, map regression scope at qa.

It attaches as a **local proxy**: your harness points its base URL at AgentAlloy and every request flows through with the right instructions composed in — for Claude Code, wiring sets a single env var and your own credentials pass through untouched. Smaller models get leverage they don't have alone; larger models get your actual house rules — and a way to interrogate your actual codebase — instead of their best guess. (A third module — **Knowledge**: the decisions behind the code and why they were made — is on the roadmap.)

Everything runs on your machine — one small embed model and a 0.6B reranker over embedded LanceDB + DuckDB. No cloud calls, and zero paid-LLM tokens spent deciding what to inject: routing is **deterministic by default**, and the one optional LM stage in the compose path ships off because it showed no lift on our evals (we measured, so we disabled it — [numbers here](BENCHMARKS.md)).

The structured workflow layer (spec → design → build → qa gates) is per-repo and **opt-out**: `wire --lifecycle-mode off` gives you pure context injection with no process attached, and `agentalloy flow free` pauses the workflow anytime without losing your place.

Composed into the prompt without you pasting a thing:

- "How do I write a failing pytest before the implementation?" — TDD workflow + framework idioms, composed from `pytest` + `testing` packs.
- "How do I structure an incremental dbt model so it stays correct across re-runs?" — data-engineering governance + domain skills, composed from `data-engineering` + `engineering` packs.
- "Wire OpenTelemetry into this FastAPI app." — observability rules + framework patterns, composed from `fastapi` + `analytics` packs.
- "I'm reviewing this PR — what should I check?" — review heuristics, composed from `code-review` packs.

One question away, because the injected guidance taught the agent to ask:

- "What breaks if I change this function's signature?" — `agentalloy code callers` returns exact transitive call sites, not a grep guess.
- "Start this task grounded." — `agentalloy code bundle` returns a budgeted slice of the symbols, callers, and docs the task actually touches.

---

## Contents

- [Getting started](#getting-started)
- [Demo](#demo)
- [Container deployment](#container-deployment)
- [What makes the composition different](#what-makes-the-composition-different)
- [How it works: phases, contracts, signal layer](#how-it-works-phases-contracts-signal-layer)
- [Code index (optional)](#code-index-optional)
- [How to use it](#how-to-use-it)
- [Profiles](#profiles-user-scoped-skill-contexts)
- [Harness support](#harness-support)
- [Standalone CLI](#standalone-cli)
- [REST API](#rest-api)
- [MCP Server](#mcp-server)
- [Packs shipping in-tree](#packs-shipping-in-tree)
- [Architecture](#architecture)
- [Telemetry](#telemetry)
- [Web UI](#web-ui)
- [Configuration](#configuration)
- [Development](#development)
- [Need Help?](#need-help)
- [Contributing](#contributing)
- [Benchmarks](#benchmarks)
- [License](#license)

---

## Getting started

Two doors — pick the one that's you:

### New to AgentAlloy

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh                   # 1. install uv (Linux / macOS)
uv tool install git+https://github.com/nrmeyers/agentalloy.git    # 2. install the agentalloy CLI
agentalloy setup                                                  # 3. run the setup wizard
cd /path/to/your/repo && agentalloy wire --harness claude-code    # 4. wire (per-repo — repeat in each project)
```

The wizard detects your hardware, downloads the GGUF models, starts the embed + reranker servers, lets you pick skill packs, wires your IDE harness, and validates the result — **3–5 minutes** on a warm machine. Its first question is **how to deploy**: **Container** (default — one GHCR image, zero host-side inference dependencies, CPU-only) or **Native** (llama-server on your host, GPU acceleration). Trade-offs and the full step-by-step runbook — scripted flags, air-gapped installs — live in **[INSTALL.md](INSTALL.md)**.

> **Already using Ollama?** AgentAlloy runs on `llama-server`, not Ollama. You can still point `RUNTIME_EMBED_BASE_URL` at any OpenAI-compatible 768-dim `nomic-embed-text-v1.5` endpoint you already run.

Scripted installs skip the wizard: `agentalloy setup -n --hardware nvidia --packs all --harness claude-code` (native) or `agentalloy setup -n --deployment container --harness claude-code`; more variants in [INSTALL.md § Scripted setup](INSTALL.md#scripted--non-interactive-setup). Just want to see it work first? [Run the demo](#demo).

### Already running AgentAlloy

```bash
agentalloy upgrade            # move to the latest release — native or container, auto-detected; preflight + confirm
agentalloy upgrade --check    # report current vs latest, change nothing
```

You don't have to remember to check: the running service polls GitHub for a newer release at most once a day (its only outbound call, opt out with `AGENTALLOY_RELEASE_CHECK=0`) and surfaces an `↑` nudge on the status line, in `agentalloy status`, and at server-start. Preflight details, release pinning (`--ref`), and re-embed prompts: [INSTALL.md § Upgrading](INSTALL.md#upgrading).

### Runtime toggles (set by what they measured)

Independent of deployment, composition is **deterministic by default**. Three env-var levers in `~/.config/agentalloy/.env` tune the optional model-backed assists; each default is the one the benchmarks earned, and each fails open to the deterministic path: **`SIGNAL_INTENT_BACKEND`** (default `reranker` — the primary phase-transition trigger; beats cosine on intent, per-intent macro-F1 0.24 → 0.69; the wizard provisions its server), **`LM_ASSIST`** (default `off` — the compose fragment re-ranker showed no eval-set lift at ~500 ms/compose), and **`RETRIEVAL_GRAPH_EXPAND`** (default `off` — no measured lift). Design and tuning detail: [docs/lm-assist-design.md](docs/lm-assist-design.md); config reference: [docs/operator.md](docs/operator.md); numbers: [BENCHMARKS.md](BENCHMARKS.md).

---

## Demo

```bash
$ curl -s -X POST http://localhost:47950/compose \
    -H 'Content-Type: application/json' \
    -d '{"task": "write a failing pytest", "phase": "build"}' | jq .

{
  "output": "## TDD: write the failing test first\n\nIn pytest, ...",
  "source_skills": ["test-driven-development", "pytest-fixtures"],
  "system_skills_applied": true,
  "latency_ms": { "retrieval_ms": 31, "assembly_ms": 16, "total_ms": 47 }
}
```

Your agent calls `/compose`, gets back the relevant raw skill prose, and assembles it inside its own prompt. No paid LLM in the loop, no token tax, no API key roulette. Sub-50ms p95 on a warm cache.

---

## Container deployment

The default deployment is a single container bundling the service and its two `llama-server` inference runners (embed :47951, reranker :47952 — neither exposed), pulled from GHCR (`ghcr.io/nrmeyers/agentalloy:latest`). One command sets it up — `agentalloy setup -n --deployment container --harness <name>` — port 47950 is the only external surface, and a named `agentalloy-data` volume persists corpus, databases, and GGUFs across restarts.

Container inference is CPU-only on every host (GPU acceleration requires a native install) — fast enough for the runtime path. Architecture, bootstrap sequence, hardware requirements, and operational commands: [INSTALL.md](INSTALL.md#container-architecture).

---

## What makes the composition different

- **Composed per task, not loaded every turn.** A skill that's irrelevant to the current task isn't in the prompt at all — RRF + applicability filtering picks the right subset for each request.
- **Three instruction sets, fused.** Governance, workflow, and domain skills are composed together into one persona — not three files the agent has to reconcile on its own.
- **Phase-aware.** Phase sets the candidate budget (`k`) and the dense-vs-lexical fusion weights — QA biases lexical, spec biases dense — so the same task composes differently across the lifecycle. Retrieval itself is phase-agnostic: there's no hard phase→category gate.
- **Hybrid retrieval, not lexical-only.** Token-literal queries (`"JWT"`, `"Prisma"`) hit BM25; semantic queries ("the auth handler") hit a 768-dim dense leg. Phase-tuned Reciprocal Rank Fusion picks the better signal per query.
- **No model variance by default.** Embeddings + lexical match + deterministic fusion mean the same task → same composition, regardless of which agent model you swap in tomorrow. (The optional composition fragment re-ranker is the only non-deterministic element in this path — off by default, fail-open.)
- **Versioned & validated.** Every skill is sourced from authoritative upstream docs and validated against the R1–R8 quality contract (`src/agentalloy/_packs/meta/sys-skill-authoring-rules.md`).

---

## How it works: phases, contracts, signal layer

Three small artifacts on disk drive everything AgentAlloy does. None of them belong to your agent's prompt — they're state files that the signal layer reads.

```
.agentalloy/phase       →  phase: build
```

**The phase file.** A sticky, one-line YAML file under your project tracking the SDD lifecycle: `intake → spec → design → build → qa → ship` — plus a **fast lane** (`sdd-fast`, a compressed spec-design-build for small tasks) and an **add-skill lane** (guided, human-approved custom-skill authoring). Each phase's workflow skill is injected as the persona until its declarative exit gates pass; `spec → design` and `design → build` additionally require an explicit `agentalloy approve <phase>` sign-off. The lifecycle is per-repo and opt-out (`agentalloy wire --lifecycle-mode off`), and `agentalloy flow free` pauses all workflow steering — domain skills keep composing — until `flow resume`. Lanes, lifecycle modes, and the full gate inventory: [docs/operator.md](docs/operator.md#phases).

**Task contracts.** A short markdown file (`.agentalloy/contracts/<phase>/<task>.md`) the agent writes once at task start, declaring `domain_tags`, scope, and success criteria in its frontmatter. From then on, **`domain_tags` is the BM25 input for retrieval** — surgical, intent-aware, and stable across the conversation. Schema and a full example: [docs/operator.md](docs/operator.md#contracts).

**The signal layer.** A small deterministic Python module that wakes on three events — a user prompt, a contract write, a tool about to fire. A cheap pre-filter exits silently when nothing matches (no tokens spent, no injection); otherwise it evaluates the active phase's exit gates — deterministic predicates (`artifact_exists`, `git_state`, `contract_has_tags`) plus a named-intent classifier that defaults to a small local reranker and fails open to cosine scoring — then atomically advances the phase (injecting the next workflow skill) or fires a system skill (commit-safety, secret-handling). Zero paid-LLM tokens spent on "where am I?" or "should I call AgentAlloy now?". Internals: [docs/operator.md](docs/operator.md#signal-layer) and [docs/proxy-architecture.md](docs/proxy-architecture.md).

---

## Code index (optional)

A second context module alongside skill composition: a tree-sitter symbol graph plus hybrid semantic/lexical search over **your own repos**, served under `/code/*` on the same port. Off by default — enable it in the setup wizard's module selection or set `CODE_INDEX_ENABLED=1`. The module's dependencies live behind the `[code-index]` extra (`uv tool install 'agentalloy[code-index]'`); the container image ships it preinstalled.

```bash
agentalloy code index                      # index the current repo (incremental; --force for full)
agentalloy code search "where are auth tokens validated"
agentalloy code callers <fqn>              # call sites (--depth N for transitive)
agentalloy code bundle "<task>"            # budgeted context bundle for a task
agentalloy code status                     # indexed repos + active jobs + staleness
agentalloy code watch enable               # per-repo file-watch enrollment (CODE_INDEX_WATCH is the master switch)
```

Indexes are per-repo under `~/.local/share/agentalloy/code_index/` (DuckDB symbol graph + LanceDB vectors) and reuse the same local embed server as the skill corpus. `agentalloy wire` adds a small code-index block to the repo's agent instructions when the module is enabled, and offers to index an unindexed repo on the spot; `code status` flags repos whose index is behind `git HEAD` (nothing auto-reindexes — enroll in watch for that). See [docs/code-index.md](docs/code-index.md) for the endpoint table, CLI reference, and storage layout.

---

## How to use it

Four paths, depending on how your harness integrates with external tools.

**Standalone HTTP service.** Run it on its own port and call `POST /compose` from anything — an agent, a script, CI. `python -m agentalloy` serves on :47950; `curl -s localhost:47950/health` confirms it's up.

**Proxy-wired harness (full integration).** If your harness honors a custom API base URL, `agentalloy wire --harness <name>` points it at the local proxy: every LLM request flows through with skills injected and gates evaluated. Claude Code wiring is **auth-transparent** — it sets only `ANTHROPIC_BASE_URL` (a per-repo `/proj/<token>` URL), never an API key, so your own credential (including account/OAuth auth) passes through verbatim; and the upstream is configurable (`ANTHROPIC_UPSTREAM_URL`), so any Anthropic-compatible provider or a chained proxy works. Wiring mechanics: [docs/install/harness-catalog.md](docs/install/harness-catalog.md); upstreams: [docs/operator.md](docs/operator.md#alternative-anthropic-compatible-upstreams).

**Parallel sessions with git worktrees.** `agentalloy worktree <harness> <branch> -b` creates the worktree and wires it in one shot; each worktree's distinct path gets its own `/proj/<token>` — its own phase, its own upstream — while all worktrees share the one running service and corpus. One caveat: corpus mutations (`install-packs`, `reembed`) take the single-writer lock and affect every worktree, so stop the service before running them. Details: the `worktree` entry in [INSTALL.md](INSTALL.md).

**Sidecar harness.** Cursor, Windsurf, GitHub Copilot, and Antigravity CLI route through their own backends and can't be intercepted. For those, `agentalloy wire` writes a static rules file and `agentalloy watch start --harness <name>` (once per session) keeps it regenerated within ~1s of a phase or contract change. Capability matrix: [Harness support](#harness-support) below.

---

## Profiles: user-scoped skill contexts

Profiles let you maintain separate skill contexts for different kinds of work — e.g., a `work` profile with stricter CI gates and team governance rules, a `personal` profile with relaxed constraints and hobby-project domain skills. Profiles auto-resolve per-repo based on git remote URL, filesystem path, or an explicit project marker, so you never need to switch them manually.

This is the key difference from `AGENTS.md` / `SKILL.md` approaches: the **install is one-time and user-scoped** (state under `~/.config/agentalloy/`, data under `~/.local/share/agentalloy/`) and profiles determine skill overrides per-repo — only the **wiring** is per-repo (`agentalloy wire` injects sentinels into each project's harness config files; the skills those sentinels reference come from the user-scoped profile). Full details: [profiles-and-overrides.md](docs/profiles-and-overrides.md).

---

## Harness support

Harnesses fall into two categories:

- **Proxy-wired** (Claude Code, Continue.dev, Aider, Cline, Codex, OpenClaw, OpenCode, Hermes Agent) — full per-turn integration via the local proxy. The proxy intercepts LLM traffic, injects skill context, and evaluates gates automatically.
- **Sidecar** (Cursor, Windsurf, GitHub Copilot, Antigravity CLI) — static rules file kept current by a file watcher. Reduced capability: no enforcement, advisory text only.

Proxy-wired is the preferred mode. Full per-harness catalog: [docs/install/harness-catalog.md](docs/install/harness-catalog.md).

---

## Standalone CLI

The `agentalloy` CLI handles install, service management, phase control, and composition. Key commands:

```bash
agentalloy setup                          # Interactive install wizard
agentalloy wire --harness <name>          # Wire a harness (--lifecycle-mode full|off, --clean-room)
agentalloy unwire [--harness <name>]      # Remove wiring (one harness in this repo; --all for every repo)
agentalloy serve                          # Run the service
agentalloy phase [set|clear]              # Bare prints current phase; set/clear to change
agentalloy flow free|resume|status        # Pause/resume workflow steering (skills keep composing)
agentalloy code <index|search|status|…>   # Code-index module CLI (see docs/code-index.md)
agentalloy compose --contract <path>      # One-shot composition
agentalloy approve <phase>                # Record the human-in-the-loop approval marker (spec | design)
agentalloy doctor                         # Diagnose install issues
agentalloy upgrade                        # Upgrade to the latest release (--check to preview)
```

Each subcommand emits structured JSON on stdout; pair with `jq` for scripting. Full command reference — including `add`, `worktree`, `customize`, `cleanup`, and `rerank-warmup`: [INSTALL.md](INSTALL.md) and [docs/operator.md](docs/operator.md).

---

## REST API

AgentAlloy serves both proxy surfaces — `POST /proj/{token}/v1/messages` (native Anthropic passthrough: auth-transparent, per-repo `{token}` discriminator, no translation) and `POST /v1/chat/completions` (OpenAI-compatible) — plus `POST /compose` (manual skill composition), `GET /health` (liveness), and `/code/*` when the [code-index module](#code-index-optional) is enabled. Full endpoint list and request/response schemas: [proxy-architecture.md](docs/proxy-architecture.md).

---

## MCP Server

For harnesses that speak the Model Context Protocol instead of taking a proxy, AgentAlloy ships a built-in dependency-free stdio MCP server exposing one tool — `get_skill_for(task, phase)` — which forwards to the local `/compose` endpoint. Wire it with `agentalloy wire-harness --harness <name> --mcp-fallback` (supported: Claude Code, Cursor, Continue.dev). Per-harness configuration: [Harness Catalog § MCP Fallback](docs/install/harness-catalog.md#mcp-fallback).

---

## Packs shipping in-tree

The corpus is **packs** — opt-in groups of related skills. `main` ships **38+ packs / 320+ declared skills** organized across 9 tiers:

<table>
<tr><th>Tier</th><th>Packs</th></tr>
<tr><td><b>foundation</b></td><td><code>core</code> · <code>documentation</code> · <code>engineering</code> · <code>performance</code> · <code>refactoring</code></td></tr>
<tr><td><b>language</b></td><td><code>csharp-dotnet</code> · <code>go</code> · <code>java</code> · <code>nodejs</code> · <code>python</code> · <code>rust</code> · <code>typescript</code></td></tr>
<tr><td><b>framework</b></td><td><code>fastapi</code> · <code>fastify</code> · <code>nestjs</code> · <code>nextjs</code> · <code>react</code> · <code>vue</code></td></tr>
<tr><td><b>tooling</b></td><td><code>linting</code> · <code>pytest</code> · <code>testing</code> · <code>vite</code> · <code>vitest</code></td></tr>
<tr><td><b>workflow</b></td><td><code>code-review</code> · <code>design-review</code> · <code>intake</code> · <code>sdd</code></td></tr>
<tr><td><b>domain</b></td><td><code>analytics</code> · <code>calendar-ui</code> · <code>data-engineering</code> · <code>ui-design</code></td></tr>
<tr><td><b>platform</b></td><td><code>github-actions</code></td></tr>
<tr><td><b>protocol</b></td><td><code>rest</code> · <code>webhooks</code></td></tr>
<tr><td><b>store</b></td><td><code>redis</code> · <code>redshift</code> · <code>snowflake</code> · <code>temporal</code></td></tr>
</table>

Every skill is sourced from authoritative upstream docs and validated against the **R1–R8 quality contract** (`src/agentalloy/_packs/meta/sys-skill-authoring-rules.md`) via a local-first author-critic pipeline (currently being redesigned). Nothing about authoring is required to *use* AgentAlloy at runtime.

---

## Architecture

AgentAlloy is a three-layer system:

1. **Signal layer** — deterministic Python that wakes on phase transitions, contract writes, or tool fires. Pre-filters cheaply, evaluates exit gates, and composes skills only when needed.
2. **Composition engine** — hybrid BM25 + dense retrieval over DuckDB (skill graph) and LanceDB (vector + BM25 index), fused via phase-tuned Reciprocal Rank Fusion.
3. **Proxy** — OpenAI-compatible and Anthropic Messages API endpoints that intercept harness traffic, inject composed skills, and forward to the upstream LLM.

Both runtime paths are **deterministic by default** — the only optional LM stages (the composition re-ranker and the signal-layer intent reranker) fail safe to deterministic scoring. See [docs/proxy-architecture.md](docs/proxy-architecture.md) for the full design.

---

## Telemetry

Every `/compose`, `/retrieve`, signal evaluation, and proxied request writes a structured trace to DuckDB before the response returns; trace-write failures never propagate. Query `GET /telemetry/{traces,savings,coverage}`, aggregate with `agentalloy telemetry savings`, or browse it all in the [web UI](#web-ui). Trace schema and filters: [docs/operator.md](docs/operator.md#telemetry).

---

## Web UI

A browser dashboard ships in the same FastAPI process at [http://localhost:47950/](http://localhost:47950/) — no extra daemon, localhost-only, no auth. Pages: Config (`.env` editor), Telemetry (traces, savings, coverage), Skills (corpus browser + override editor), Playground (retrieval/compose/signal simulator), Repos & Approvals (per-repo phase, gate blockers, sign-off queue), Ops, and a New Skill wizard. Setup downloads the prebuilt bundle automatically; without one the API is unaffected. Page tour and operator notes: [docs/operator.md](docs/operator.md#web-ui).

---

## Configuration

`agentalloy write-env --preset <cpu|nvidia|radeon|apple-silicon>` renders `~/.config/agentalloy/.env` from a hardware preset (the files under `src/agentalloy/install/presets/` are the source of truth). The keys you'll touch most: `UPSTREAM_URL` / `UPSTREAM_MODEL` / `UPSTREAM_API_KEY` (the global-fallback upstream the proxy forwards to; per-repo `agentalloy add` overrides them) and `RUNTIME_EMBED_BASE_URL` (embedding endpoint, default `http://localhost:47951`). Full key reference: [docs/operator.md](docs/operator.md#configuration).

---

## Development

```bash
uv sync && uv run pytest                                          # deps + unit tests (fast)
uv run ruff check . && uv run ruff format --check . && uv run pyright   # lint + format + types
```

Integration tests (`uv run pytest -m integration`) need a running nomic-embed llama-server. Branching, CI gates, and where the tests live: [RELEASE.md](RELEASE.md).

---

## Need Help?

[Installation guide](docs/install/) (per-harness setup) · [Operator guide](docs/operator.md) (CLI reference, service management) · [Troubleshooting](docs/troubleshooting.md) (common errors and fixes) · [Discussions](https://github.com/nrmeyers/agentalloy/discussions) (ask questions, share setups)

---

## Contributing

Use an editable install so your changes are reflected immediately: `git clone` the repo, then `uv sync && uv tool install --editable .` (full steps, plus migrating a legacy pipx install: [INSTALL.md](INSTALL.md#step-1b-install-the-cli-user-scoped)). Branching, commit conventions, PR flow, CI gates, and the release process are the contribution runbook: [RELEASE.md](RELEASE.md).

---

## Benchmarks

Measured on a 4-model × 3-condition matrix (composed / flat-oracle / no skills) over 18 pre-registered domain tasks (5 seeded runs per cell), with an independent 27B LLM-judge cross-check. Results that stand out:

- **On domain tasks, composed injection beat the bare model on every architecture (+0.02 to +0.17)**, capturing 51–71% of a hand-picked oracle's lift at 21–32% fewer tokens — automatic selection doing the job a human curator would.
- **The lift is biggest where it matters most: the LFM2.5 edge model gains +0.172**, and an independent 27B LLM-judge confirms it (+0.154, 95% CI excludes zero) — real answer quality, not a grader artifact.
- **Composed injection disciplines a small model.** On domain tasks the unguided baseline runs ~24% longer (2604 vs 1988 output tokens) and scores lower — focused skill prose makes the edge model both more correct *and* more concise.
- **Strong models sit near their ceiling** (35B +0.039, 27B +0.022): composition is the difference-maker for small models and a no-harm tie for large ones.

Full matrix, methodology, and caveats in [BENCHMARKS.md](BENCHMARKS.md).

---

## License

MIT. See [LICENSE](LICENSE).
