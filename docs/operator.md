# Operator Reference

Operator guide for AgentAlloy. Covers key concepts, terminology, system architecture, configuration, and customization for operators who install, maintain, and extend their AgentAlloy instance.

> For the step-by-step install runbook, the full `agentalloy` command reference (`add`, `worktree`, `customize`, `cleanup`/`cleanup --deep`, per-harness `wire`/`unwire`, …), and container operations (the `ghcr.io/nrmeyers/agentalloy` image, ports `47950`/`47951`/`47952`, and corpus volume self-heal on reuse), see **[INSTALL.md](../INSTALL.md)**.

## Key Concepts and Terminology

### Packs

Packs are opt-in groups of related skills, organized into tiers. Each pack contains multiple skills and a `pack.yaml` manifest declaring its tier. Packs are installed via `agentalloy install-pack <name>` or the interactive `agentalloy setup` wizard.

**Tier hierarchy** (the canonical tier set is the keys of `TAG_POLICY_BY_TIER` in `ingest.py`; per-skill tier is declared in each pack's `pack.yaml` and resolved by `skill_tier.py`):

| Tier | Purpose | Example Packs |
|------|---------|---------------|
| foundation | Core engineering practices | core, documentation, engineering, performance, refactoring |
| language | Language-specific patterns | python, typescript, go, rust, java, csharp-dotnet |
| framework | Framework-specific patterns | fastapi, react, nextjs, nestjs, vue |
| tooling | Development tools | pytest, linting, testing |
| workflow | Process and lifecycle | code-review, design-review, intake, sdd |
| domain | Domain-specific knowledge | analytics, data-engineering, ui-design |
| platform | Platform-specific | github-actions |
| protocol | Protocol conventions | rest, webhooks |
| store | Data store patterns | redis, snowflake, temporal |

**Tag policies by tier** (from `ingest.py`): Each tier has a soft ceiling on domain tags per skill and a threshold above which rationale is required:

| Tier | Soft ceiling | Rationale required above |
|------|-------------|--------------------------|
| foundation | 12 | 8 |
| language | 10 | 7 |
| framework | 10 | 7 |
| store | 10 | 7 |
| cross-cutting | 12 | 8 |
| platform | 10 | 7 |
| tooling | 8 | 6 |
| domain | 10 | 7 |
| protocol | 8 | 6 |
| workflow | 8 | 6 |

### Skills

Skills are the unit of expertise. Each skill has a `skill_id`, `canonical_name`, category, and a set of fragments. Skills are either:

- **Domain skills** — task-specific expertise (e.g., "how to write TDD tests", "how to design REST APIs"). Stored in LadybugDB as Skill nodes with Version and Fragment children. Retrieved via hybrid BM25 + dense search.
- **System skills** — governance and safety rules (e.g., "never commit secrets", "use conventional commits"). Applied via applicability predicates (`always_apply`, `phase_scope`, `category_scope`).

System skill IDs must start with `sys-`.

**Skill class** — `domain` or `system`, determines storage, retrieval, and enforcement behavior.

### Fragments

Fragments are the smallest retrievable unit of skill content. Each fragment has:

- `sequence` — ordering within the skill
- `fragment_type` — categorization (see below)
- `content` — the actual prose, verbatim from the source SKILL.md

**Fragment types** (from `ingest.py`):

| Type | Purpose |
|------|---------|
| setup | Prerequisites, configuration, environment setup |
| execution | Core task steps and instructions |
| verification | Checks, tests, confirmation criteria |
| example | Concrete illustrations or code samples |
| guardrail | Constraints, things not to do, safety rules |
| rationale | Why-explanations, not how |

**Fragment size rules** (from `ingest.py`):

- Hard minimum: 5 words (rejected below this)
- Warning minimum: 25 words (lint warning, error with `--strict`)
- Hard maximum: 2000 words (rejected above this)
- Warning maximum: 800 words (lint warning, error with `--strict`)

### Phases

Phases track where the agent is in the software development lifecycle. The **authoritative SDD runtime lifecycle** is a linear graph (from `signals/gates.py`):

```
intake → spec → design → build → qa → ship
```

Plus a fast lane for small, clearly-bounded work that intake can route to (`intake → sdd-fast → qa → ship` — the fast lane compresses spec+design+build, then merges into the standard qa → ship verification and delivery). In the default lifecycle mode every session opens with intake: the proxy composes the intake workflow on the first request of a fresh session (the signal layer handles `intake` unconditionally), gated per-repo by `lifecycle_mode` — see **Lifecycle modes** below.

The phase file lives at `.agentalloy/phase` in each project and holds one of these phase names. Each phase has a corresponding workflow skill whose prose is injected as the agent's persona for that phase. Phase transitions are decided by exit gates (see Signal Layer).

Two separate vocabularies exist for **skill authoring/ingest** and should not be confused with the runtime lifecycle above:

- **`phase_scope` validation** (`ingest.py` `_VALID_PHASES`): `intake`, `spec`, `design`, `build`, `qa`, `ship`, `sdd-fast` — the values a skill may scope itself to at ingest time (reconciled to the runtime lifecycle).
- **Workflow position markers** (`ingest.py` `WORKFLOW_POSITION_MARKERS`): `sdd`, `phase:spec`, `phase:design`, `phase:plan`, `phase:testgen`, `phase:build`, `phase:verify`, `phase:deliver`, `code-review`, `release`, `incident`, `rfc` — tags describing where in a process a skill applies.

### Lifecycle modes

Whether the phase lifecycle runs at all is a **per-repo** setting, stored at `.agentalloy/config` (`lifecycle_mode:`) and read by the proxy on every request:

- **`full`** (default) — the intake front-door runs on every session and the full phase lifecycle is active; workflow skills inject per phase.
- **`off`** — wired but composes nothing (full passthrough).

Set it with `agentalloy wire --lifecycle-mode {full,off}`. When wiring detects a repo that already defines its own `.claude/agents/` or `.claude/commands/`, it prompts for the mode (interactive terminals only; non-interactive runs default to `full`). (The legacy `assist` mode was removed with the hook transport; a repo still configured `assist` now reads as `off`.)

In `full` mode on Claude Code, `wire` also writes a soft-precedence note at `.claude/CLAUDE.md` — loaded last by Claude Code, so a repo's own workflow guidance is weighted over conflicting global directives. The opt-in `agentalloy wire --clean-room` additionally excludes your global `~/.claude/CLAUDE.md` from that repo by adding it to `claudeMdExcludes` in `.claude/settings.json`; note this suppresses **all** of your global directives there, not just conflicting ones. Both writes are reversed by `agentalloy unwire`.

### Contracts

Task contracts are markdown files under `.agentalloy/contracts/<phase>/` that declare task intent. Frontmatter includes:

- `phase` — current phase
- `task_slug` — unique identifier
- `domain_tags` — BM25 input for retrieval (the primary retrieval signal)
- `scope.touches` / `scope.avoids` — file path patterns
- `success_criteria` — acceptance criteria list

When present, `domain_tags` from contracts drive BM25 retrieval — surgical and intent-aware. Without contracts, AgentAlloy falls back to rule-based keyword extraction from the task description.

### Signal Layer

The signal layer is a Python module (deterministic by default) that evaluates conditions and triggers actions. Three event types:

1. **Pre-filter** — cheap keyword matching + file-event scope checks. Decides if a signal evaluation is warranted.
2. **Gate evaluation** — deterministic predicates (`artifact_exists`, `git_state`, `contract_has_tags`) plus named-intent gates. The named-intent gates score utterances with the `qwen3-reranker-0.6b` cross-encoder (`SIGNAL_INTENT_BACKEND=reranker`, **the default** — a measured win on the labeled intent benchmark, see BENCHMARKS.md). This backend needs a reranker server — a `llama-server` running `Qwen3-Reranker-0.6B-Q8_0.gguf` (completions mode), default `127.0.0.1:47952`; if it is unreachable, or you set `SIGNAL_INTENT_BACKEND=cosine`, the gates fall open to cosine-similarity scoring against reference phrase sets, byte-for-byte. Cosine is the fail-open floor, so the default is safe even where the reranker server is not running — but the lift only materializes where it is.
3. **Action** — write phase file atomically, emit workflow skill prose, or fire system skills.

The signal layer runs per-request through the proxy for proxy-wired harnesses. For sidecar harnesses (Cursor, Windsurf, GitHub Copilot, Gemini CLI), the proxy path is not available and the signal layer is replaced by a file-watching sidecar. See [Sidecar Experience](sidecar-experience.md).

### Proxy interception

For proxy-wired harnesses, the AgentAlloy proxy intercepts every LLM request, evaluates the signal layer (phase transition, gate predicates, system skill applicability), mutates the request payload to inject the resulting context, and forwards to the real upstream. No per-turn hook installation is needed — the harness's LLM client points at `http://localhost:<port>/v1` via its native API-base configuration.

### Sidecar

The sidecar is a file-watching process for harnesses that can't be proxy-wired. Watches `.agentalloy/phase` and `.agentalloy/contracts/**` for changes and regenerates the harness's rules file within ~500ms (debounce). See [Sidecar Experience](sidecar-experience.md) for details.

### Classification

Harness classification determines which integration vector is available:

- **Proxy-wired** — harness honors a custom API base URL (OpenAI / Anthropic / config-file `apiBase`). Full capability: per-request context injection, gate enforcement at the proxy, automatic phase transitions. Examples: Claude Code, Continue.dev, Aider, Cline, OpenCode, Hermes Agent.
- **Sidecar** — harness routes through its own backend and cannot be intercepted. Capabilities reduced: advisory-only system skills, file-watcher phase detection. Examples: Cursor, Windsurf, GitHub Copilot, Gemini CLI.

See [Harness Catalog](install/harness-catalog.md) for the full list and [Harness Classification](harness-classification.md) for the classification spec.

### Profiles

Profiles are named bundles of skill overrides and per-profile datastores. They allow separate skill contexts for different work (e.g., `work` vs `personal`) without reinstalling.

Profile resolution order (from `profiles.py`):
1. Explicit project marker (`.agentalloy/profile`)
2. Git remote URL pattern (`match_remote` in `profiles.yaml`)
3. Path prefix (`match_path` in `profiles.yaml`)
4. Fallback to `default_profile`

See [Profiles and Overrides](profiles-and-overrides.md) for full details.

### Three-Layer Overrides

Skill overrides follow a three-layer resolution (from `customize.py`):

1. **Layer 1 (highest)** — Project-level: `.agentalloy/skills/<class>/<name>.yaml`
2. **Layer 2** — Profile-level: `~/.local/share/agentalloy/profiles/<name>/skills/<class>/<name>.yaml`
3. **Layer 3 (lowest)** — Shipped defaults: bundled in `_packs/`

Shipped defaults are immutable; operators override via project or profile layers. See [Profiles and Overrides](profiles-and-overrides.md) for CLI details.

## System Architecture Overview

### Data Plane

Two embedded databases:

| Store | Engine | Role |
|-------|--------|------|
| **LadybugDB** | Kuzu (graph DB) | Skill / Version / Fragment / Pack graph — "what skill means and how its pieces relate" |
| **DuckDB** | DuckDB (columnar) | 768-dim vector index, BM25 FTS index, composition traces, per-profile datastores (`skills.duck`), shared domain datastore (`domain.duck`) |

Embeddings are stored in DuckDB, not LadybugDB. The Kuzu VECTOR extension is intentionally NOT loaded due to lifecycle incompatibility with FastAPI.

### Service

AgentAlloy runs as a FastAPI service on port 47950 (default). Endpoints:

- `POST /compose` — hybrid retrieve + assemble (the primary entry point)
- `POST /compose/text` — same as `/compose`, returns `text/plain`
- `POST /retrieve` — retrieve only, no assembly
- `GET /retrieve/{skill_id}` — lookup single skill's fragments
- `GET /skills/{skill_id}` — inspect skill metadata
- `GET /telemetry/traces` — query composition traces
- `GET /health` — liveness probe
- `GET /diagnostics/runtime` — backend/model/DB state

### Retrieval Pipeline

1. **Query extraction** — from contract `domain_tags` (primary) or rule-based extraction from task text (fallback)
2. **BM25 leg** — lexical match over fragment content
3. **Dense leg** — cosine similarity against 768-dim embeddings (nomic-embed-text-v1.5.Q8_0.gguf)
4. **RRF fusion** — phase-tuned Reciprocal Rank Fusion combines both legs
5. **Applicability filter** — deterministic predicates remove inapplicable fragments
6. **Diversity selection** — top-k with diversity constraint (default: on)
7. **Assembly** — selected fragments assembled into composed prose output

**Optional flag-gated steps** (all off by default, all fail open to the deterministic path above when the local model or graph is unavailable):

- **Graph expansion** (`RETRIEVAL_GRAPH_EXPAND=on`, default off): splices `requires`-edge neighbors of the top ranked skills into the candidate set before selection.
- **Stage B LM fragment re-rank** (`LM_ASSIST=arbitrate` on the GPU presets, `off` on cpu/container): runs post-fusion, pre-selection. The `qwen3-reranker-0.6b` cross-encoder scores the top 8 fragments (pairwise yes/no logprobs over `/v1/completions`, bounded by `LM_ASSIST_MAX_CANDIDATES` which matches the reranker's `--parallel` slot count); on a HIT, survivors above `LM_ASSIST_KEEP_THRESHOLD` (default `0.0` — gated-off pending a P(yes) measurement) are routed through the SAME `skill_granular_select` diversity selection as the deterministic path (no longer "diversity off"). On disabled/timeout/error, deterministic selection runs unchanged.

### Embedding Model

Single model for all embedding needs: `nomic-embed-text-v1.5.Q8_0.gguf` at 768 dimensions, served by llama-server on `47951`. Used for:
- Fragment embeddings (retrieval)
- Semantic gate scoring (cosine similarity against reference phrase sets)
- Contract query embeddings

nomic serves on stock llama.cpp via the nomic-bert architecture. It requires `--embeddings --pooling mean --ctx-size 2048 --ubatch-size 2048`. nomic also has a prefix footgun: embed **queries** with a literal `search_query: ` prefix and **documents** with `search_document: `.

Served via the OpenAI-compatible `/v1/embeddings` endpoint that `llama-server --embeddings` exposes. The runtime honors `RUNTIME_EMBED_BASE_URL`, so it also accepts any other OpenAI-compatible embed endpoint that returns 768-dim vectors. The `EmbeddingDimMismatch` startup guard raises if an existing corpus was built at a different dimension than 768 — switching embed models needs a re-embed, not a config change (`EMBEDDING_DIM = 768` in `src/agentalloy/storage/vector_store.py` is the single source of truth).

### Telemetry

Every `/compose`, `/retrieve`, and signal evaluation writes a structured trace to DuckDB before the response returns. Trace fields include: `trace_id`, `request_ts`, `phase`, `task_prompt`, `status`, `selected_fragment_ids`, `source_skill_ids`, `system_skill_ids`, `workflow_skill_ids`, `retrieval_latency_ms`, `assembly_latency_ms`, `total_latency_ms`, `response_size_chars`, and (on failure) `error_code`.

Signal-layer traces additionally capture: `event_type`, `pre_filter_matched`, `gates_met`, `gates_unmet`, `qwen_calls`.

The proxy records each intercepted request as a single consolidated trace row (`status='proxy_composed'`). Summarize token savings with `agentalloy telemetry savings`, which counts one `proxy_composed` row per proxy request. Query the raw rows with `GET /telemetry/traces`, and reset the table with `agentalloy telemetry clear` (truncates `composition_traces`).

## Configuration

### Config File

User-scope configuration lives under `~/.config/agentalloy/` (the `.env` sourced into the service process; honors `XDG_CONFIG_HOME`). Runtime data — corpus, per-profile datastores, profiles registry — lives under `~/.local/share/agentalloy/` (honors `XDG_DATA_HOME`). The `.env` is written by `agentalloy write-env --preset <name>` from a hardware preset; the keys it manages (the override allow-list in `install/subcommands/write_env.py`, mapping to `Settings` fields in `config.py`) are:

- `LADYBUG_DB_PATH` — LadybugDB (skill graph) location
- `DUCKDB_PATH` — DuckDB (vector + FTS + traces) location
- `RUNTIME_EMBED_BASE_URL` — embedding llama-server URL (default `http://localhost:47951`)
- `RUNTIME_EMBEDDING_MODEL` — embedding model GGUF (default `nomic-embed-text-v1.5.Q8_0.gguf`)
- `SIGNAL_INTENT_BACKEND` — phase-gate intent backend (`reranker`/`cosine`)
- `SIGNAL_INTENT_RERANK_URL` — reranker llama-server URL
- `SIGNAL_INTENT_RERANK_MODEL` — reranker model GGUF
- `DEDUP_HARD_THRESHOLD` / `DEDUP_SOFT_THRESHOLD` — dedup cosine thresholds (defaults `0.92` / `0.80`)
- `BOUNCE_BUDGET` — re-bounce budget
- `LOG_LEVEL` — service log level

Embedding dimension is not a config key — it is a fixed code constant (`EMBEDDING_DIM = 768` in `storage/vector_store.py`); switching it requires a re-embed, not an env change. Upstream LLM forwarding uses the bare env vars `UPSTREAM_URL` / `UPSTREAM_MODEL` / `UPSTREAM_API_KEY` as the **global fallback**; a per-repo upstream captured by `agentalloy add` (written to that repo's `.agentalloy/upstream`) **overrides** them for requests from that repo (see Environment Variables below).

### Profiles Config

`~/.local/share/agentalloy/profiles.yaml` — profile resolution rules:

```yaml
default_profile: default
profiles:
  work:
    match_remote: ["github.com/company"]
    match_path: ["~/work/"]
  personal:
    match_path: ["~/projects/"]
```

### Watcher Config (sidecar harnesses)

`~/.agentalloy/watch/<profile_name>.yaml` — sidecar configuration per profile. PID file: `~/.agentalloy/watch/<profile_name>.pid`. Log file: `~/.agentalloy/watch/<profile_name>.log`. (Note: the watcher directory is hardcoded at `~/.agentalloy/watch/` — it does **not** follow the XDG data root used elsewhere.)

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ANTHROPIC_UPSTREAM_URL` | Upstream for the native Anthropic passthrough (`/proj/<token>/v1/messages`); point at another proxy to chain | `https://api.anthropic.com` |
| `RUNTIME_EMBED_BASE_URL` | Embed llama-server URL | `http://localhost:47951` |
| `RUNTIME_EMBEDDING_MODEL` | Embedding model (GGUF) | `nomic-embed-text-v1.5.Q8_0.gguf` |
| `SIGNAL_INTENT_BACKEND` | Phase-gate intent backend (`reranker`/`cosine`) | `reranker` |
| `SIGNAL_INTENT_RERANK_URL` | Reranker llama-server URL | `http://127.0.0.1:47952` |
| `SIGNAL_INTENT_RERANK_MODEL` | Reranker model (GGUF) | `Qwen3-Reranker-0.6B-Q8_0.gguf` |
| `RUNTIME_DIVERSITY_SELECTION` | Diversity mode | `on` |
| `AGENTALLOY_RELEASE_CHECK` | New-release check: the service polls the GitHub releases API at most once a day (its only outbound call, fail-silent) and caches the result for the status-line badge, `agentalloy status`, and the server-start line. Set `0`/`off` to disable. | `1` |

### Release-update check

The check is the one place the otherwise-offline service reaches the network. It is a single throttled producer (`install/release_check.py`, run from a background task in the app `lifespan`) writing a small cache at `${XDG_DATA_HOME:-~/.local/share}/agentalloy/release-check.json`; every consumer (status line, `agentalloy status`, server-start) only reads that cache, so nothing on the request path ever blocks on it. `agentalloy upgrade` (interactive) shows a preflight card — release title/notes/URL, the version bump, and a heads-up about customized skills that will be re-validated — and confirms before swapping. `agentalloy upgrade --dismiss` mutes the nudge for the current latest until a newer release lands.

## Customization

### Skill Authoring Pipeline

Skills are authored via the author-critic pipeline:

1. **Author** — Skill Authoring Agent fragments the source SKILL.md into structured YAML
2. **Dedup** — deterministic gate rejects near-duplicates (>0.92 similarity); 0.80-0.92 band passed to QA
3. **QA** — Skill QA Agent reviews against R1-R8 quality contract
4. **Ingest** — validated YAML loaded into LadybugDB via `python -m agentalloy.ingest`

QA reviews against the R1-R8 quality contract (clear triggers, actionable steps, specific pitfalls, verification, copy-paste-ready commands, no aspirational content, accurate cross-references, context-window fit). See [Skill Authoring and Overrides Spec](skill-authoring-and-overrides-spec.md) for the full definitions.

### Skill Override CLI

`agentalloy customize {list,edit,validate,update,diff,reset}` with `--profile` and `--project` flags. Edits a skill's prose, gates, or applicability without forking shipped defaults.

### Adding Packs

```bash
# List available packs
agentalloy install-packs --list

# Install a specific pack
agentalloy install-pack <name>

# Install multiple packs
agentalloy install-packs --packs pack1,pack2,pack3
```

### Re-embedding

After adding new packs or updating the embedding model:

```bash
agentalloy reembed
```

Recomputes embeddings for all unembedded or updated fragments in LadybugDB.

## Category Vocabularies

Canonical category values validated by the ingest pipeline (from `ingest.py`):

### Domain skills

`engineering`, `ops`, `review`, `design`, `tooling`, `quality`

### System skills

`governance`, `operational`, `tooling`, `safety`, `quality`, `observability`

A skill about "how to write tests" in category `ops` is a category-fit failure. Categories must describe the actual content of the skill.

## Cross-References

- [Profiles and Overrides](profiles-and-overrides.md) — profiles, per-profile datastores, three-layer overrides
- [Sidecar Experience](sidecar-experience.md) — sidecar architecture, watcher setup, capability comparison
- [Harness Classification](harness-classification.md) — proxy-wired vs sidecar classification spec
- [Harness Catalog](install/harness-catalog.md) — per-harness integration details, auto-detection, MCP fallback
- [Skill Authoring and Overrides Spec](skill-authoring-and-overrides-spec.md) — skill authoring pipeline, override YAML schema
