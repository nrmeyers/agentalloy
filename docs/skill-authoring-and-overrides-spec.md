# Skill Authoring and Overrides Spec

This document describes the skill authoring pipeline used to produce and validate shipped skill packs, and the override system that lets users customize system and workflow skills without forking.

## Skill Classes

AgentAlloy defines three skill classes:

| Class | Purpose | Customizable? |
|-------|---------|---------------|
| `system` | Hard boundaries and operational rules (commit safety, secret handling, PR conventions) | Yes — via project/profile overrides |
| `workflow` | Process constraints tied to SDD phases (spec, design, build, qa, ship) | Yes — via project/profile overrides |
| `domain` | Domain expertise (framework patterns, language idioms, testing strategies) | No — centrally curated |

Domain skills are not exposed to the customize CLI. Attempting to `customize` a domain skill returns an error pointing to this document.

## Skill Authoring Pipeline

Skills are produced via an **author-critic pipeline** that validates each skill against the R1-R8 quality contract before it ships in a pack.

### Pipeline stages

1. **Source authoring** — author writes the skill source (markdown or YAML), applying R2, R3, R4 as they write. For imports, applies R6-R7. Uses `sys-r1-tiered-sourcing` to fetch authoritative docs per R1.

2. **Self-review** — author checks verification fragments against R3 and rationale fragments against R8.

3. **Independent critic** — an independent reviewer evaluates the source against the full R1-R8 contract plus the review history (`docs/skill-review-history/`).

4. **Revision pass** — author applies line-level fixes only; redesigns are rejected.

5. **Transform** — source is transformed to review YAML (`sys-skill-transform-contract`) with structured fields.

6. **Pack assembly** — YAML skills are grouped into packs with `pack.yaml` metadata.

### R1-R8 Quality Contract

The eight rules are defined in `src/agentalloy/_packs/meta/sys-skill-authoring-rules.md`. Summary:

- **R1** — Fetch authoritative docs before authoring against fast-moving APIs (tiered sourcing)
- **R2** — Every non-stdlib name in a code block must show its `import` once
- **R3** — Verification fragments are contracts; every item must be mechanically checkable
- **R4** — Examples must cover the case-space, not just the happy path
- **R5** — Date-stamp version-specific or minimum-value claims
- **R6** — Imports must label authorship honestly (verbatim vs scaffolded)
- **R7** — Fabricated examples must be flagged or replaced
- **R8** — Rationale fragments need lexical anchors for the obvious query

Review history is stored under `docs/skill-review-history/` and cited by the authoring rules. See the README there for expected filenames and report structure.

### Pack structure

Each pack ships as a directory under `src/agentalloy/_packs/<name>/` with:

```
pack.yaml                    # pack metadata
<skill1>.yaml                # skill YAML files
<skill2>.yaml
```

**`pack.yaml` schema:**

```yaml
name: <pack-name>
version: <semver>
tier: <foundation|language|framework|tooling|workflow|domain|platform|protocol|store>
description: |
  <multi-line pack description>
author: <author-name>
embed_model: qwen3-embedding:0.6b
embedding_dim: 1024
license: MIT
homepage: https://github.com/nrmeyers/agentalloy
always_install: false
depends_on:
  - core
  - engineering
skills:
  - skill_id: <skill-id>
    file: <skill-file>.yaml
    fragment_count: <int>
```

### Skill YAML schema

**System skill:**

```yaml
skill_id: <id>
skill_class: system
canonical_name: <human-readable name>
raw_prose: |
  <instruction prose — at least 80 characters>
applies_when:
  <predicate expression: tool name, file glob, git state, etc.>
domain_tags:
  - <tag1>
  - <tag2>
```

**Workflow skill:**

```yaml
skill_id: <id>
skill_class: workflow
canonical_name: <human-readable name>
raw_prose: |
  <instruction prose — at least 80 characters>
applies_to_phases:
  - <spec|design|build|qa|ship>
exit_gates:
  <gate definitions: artifact_exists, git_state, contract_has_tags, etc.>
contract_template: |
  <markdown template for task contracts>
signal_keywords:
  - <keyword1>
  - <keyword2>
domain_tags:
  - <tag1>
  - <tag2>
```

**Domain skill:**

```yaml
skill_id: <id>
skill_class: domain
canonical_name: <human-readable name>
raw_prose: |
  <instruction prose>
category: <category>
tier: <tier>
always_apply: false
phase_scope:
category_scope:
domain_tags:
  - <tag1>
  - <tag2>
```

See `sys-skill-tagging-rules.md` for domain_tags conventions: count (2-5, soft ceiling of 8 for protocol tier), title-stem-overlap ban, pairwise synonym ban, and retrieval-orientation requirements.

## Override System

The override system lets users customize system and workflow skills at three layers of specificity.

### Three-layer resolution

When AgentAlloy resolves a skill, it checks layers from highest to lowest priority:

| Layer | Priority | Path | Scope |
|-------|----------|------|-------|
| 1. Project | Highest | `<project>/.agentalloy/skills/{system,workflow}/<name>.yaml` | Single repository |
| 2. Profile | Medium | `~/.local/share/agentalloy/profiles/<profile>/skills/{system,workflow}/<name>.yaml` | All repos using this profile |
| 3. Shipped default | Lowest | `src/agentalloy/_packs/**/<name>.yaml` | Always present |

The first layer with a matching file wins. Shipped defaults are immutable — you cannot edit them directly.

### What can be overridden

Any field in the skill YAML can be overridden:

- **`raw_prose`** — modify the instruction text
- **`applies_when`** (system) — change which tools/files trigger the skill
- **`applies_to_phases`** (workflow) — change which phases the skill applies to
- **`exit_gates`** (workflow) — customize gate definitions
- **`contract_template`** (workflow) — customize the task contract template
- **`signal_keywords`** (workflow) — change pre-filter keywords
- **`domain_tags`** — modify retrieval tags

Override YAML files must follow the same schema as shipped defaults. Validation enforces:

- `skill_class` must be `system` or `workflow`
- `raw_prose` must be at least 80 characters
- Workflow skills must have `applies_to_phases`, `exit_gates`, and `contract_template`
- System skills must have `applies_when`

### What cannot be overridden

- **Domain skills** — domain-class skills are centrally curated. The customize CLI blocks them explicitly.
- **Shipped default files** — the `_packs/` directory is part of the installed package. Override instead.
- **`skill_class`** — overrides must match the original skill's class.

### Override lifecycle

1. **Create** — `agentalloy customize edit <name>` copies the skill from the next-higher layer (profile or default) into the target directory and opens it in `$EDITOR`.

2. **Validate** — `agentalloy customize validate <name>` checks YAML schema and field constraints. Returns errors for missing required fields, short prose, or invalid class.

3. **Ingest** — `agentalloy customize update <name>` validates and then writes the override into the profile's DuckDB `profile_skills` table. Running `update --all` re-ingests all overrides for a profile.

4. **Diff** — `agentalloy customize diff <name>` shows the diff between the current override and the next-higher layer.

5. **Reset** — `agentalloy customize reset <name>` deletes the override, reverting to the lower layer.

When an override's `raw_prose` matches the inherited default exactly, `update` automatically detects this and removes the override.

### Override file location

Override files are stored as YAML in the project or profile directory:

```
# Project-level (Layer 1)
<project>/.agentalloy/skills/system/<name>.yaml
<project>/.agentalloy/skills/workflow/<name>.yaml

# Profile-level (Layer 2)
~/.local/share/agentalloy/profiles/<profile>/skills/system/<name>.yaml
~/.local/share/agentalloy/profiles/<profile>/skills/workflow/<name>.yaml

# Shipped defaults (Layer 3 — read-only)
src/agentalloy/_packs/<pack>/<name>.yaml
```

### CLI reference

```bash
# List all customizable skills and their active layer
agentalloy customize list [--profile <name>]

# Edit a skill override (copies from lower layer if needed, opens in $EDITOR)
agentalloy customize edit <name> [--profile <name>] [--project]

# Validate an override's YAML schema
agentalloy customize validate <name> [--profile <name>] [--project]

# Validate and ingest into the profile datastore
agentalloy customize update <name> [--profile <name>] [--project]
agentalloy customize update --all [--profile <name>]

# Show diff vs next-higher layer
agentalloy customize diff <name> [--profile <name>]

# Delete override (revert to lower layer)
agentalloy customize reset <name> [--profile <name>] [--project] [--yes]
```

See [profiles-and-overrides.md](profiles-and-overrides.md) for profile resolution details and how profiles interact with overrides.
