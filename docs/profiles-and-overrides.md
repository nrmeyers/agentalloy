# Profiles and Overrides

Profiles let you maintain separate skill contexts for different kinds of work without reinstalling or forking the shipped defaults. Each profile carries its own DuckDB datastore and a directory of skill override files.

## What profiles are

A profile is a named bundle of system and workflow skill overrides, paired with its own DuckDB skills datastore (`skills.duck`). Think of it as a "persona" for AgentAlloy — a `work` profile might enforce stricter CI gates and use team-specific governance rules, while a `personal` profile might relax those constraints and include hobby-project domain skills.

Every profile shares the same domain corpus (`domain.duck` is global), so domain skill retrieval is consistent across profiles. What changes per-profile is:

- Which system/workflow skill overrides are active
- The profile-specific DuckDB skill datastore (used for override ingestion and retrieval)

## Profile resolution

When AgentAlloy runs, it detects the active profile for the current working directory. Resolution order (highest to lowest priority):

1. **Explicit project marker** — `.agentalloy/profile` in the project root, containing `profile: <name>`. This is the most specific signal: the project itself declares which profile it wants.

2. **Git remote URL match** — `match_remote` patterns in `profiles.yaml`. Uses fnmatch glob matching against the origin remote URL from `git remote get-url origin`. Example: `match_remote: ["*github.com/work/**"]` activates the profile for any repo under that organization.

3. **Path prefix match** — `match_path` patterns in `profiles.yaml`. Uses Python `Path.match()` against the resolved absolute path of the current directory. Supports `~` expansion. Example: `match_path: ["~/work/**"]`.

4. **Fallback** — the `default_profile` from `profiles.yaml` (defaults to `"default"`).

Resolution is fast (<10ms) and runs on every skill retrieval and hook fire. Stale project markers (referencing profiles that no longer exist in config) fall through to detection rules rather than erroring.

## Config file

**Path:** `~/.local/share/agentalloy/profiles.yaml` (honors `XDG_DATA_HOME`)

```yaml
profiles:
  work:
    match_remote:
      - "*github.com/acme-corp/*"
    match_path:
      - "~/work/**"
  personal:
    match_path:
      - "~/projects/**"
default_profile: default
```

If the file is missing or empty, AgentAlloy uses only the built-in `default` profile.

## Profile directory structure

```
~/.local/share/agentalloy/
  profiles.yaml                  # profile config
  profiles/
    default/
      skills.duck                # per-profile skills datastore
      skills/
        system/                  # system skill overrides
        workflow/                # workflow skill overrides
    work/
      skills.duck
      skills/
        system/
        workflow/
  domain.duck                    # shared domain corpus (all profiles)
```

Each profile's `skills/` subdirectory has `system/` and `workflow/` subdirectories for override YAML files. The `skills.duck` file is a DuckDB database containing ingested skill overrides.

The domain datastore (`~/.local/share/agentalloy/domain.duck`) is independent of profiles — all profiles share the same domain skill corpus.

## CLI: profile management

```bash
# List all profiles
agentalloy profile list

# Show active profile for current directory
agentalloy profile current

# Create a new profile
agentalloy profile init <name> [--match-remote <pattern>...] [--match-path <pattern>...]

# Change the fallback default
agentalloy profile set-default <name>

# Delete a profile
agentalloy profile delete <name> [--yes]
```

`agentalloy profile init` validates the name (letters, digits, hyphens, underscores; cannot be `"default"`), creates the directory structure, initializes the DuckDB datastore, and writes the profile entry to `profiles.yaml`. In interactive mode (TTY), it prompts for `match_remote` and `match_path` patterns if not provided via flags.

`agentalloy profile delete` refuses to delete the built-in default profile or the current `default_profile`. Use `agentalloy profile set-default` first to change the fallback.

## Three-layer skill overrides

Skill overrides work on a three-layer priority system. When resolving a skill, AgentAlloy checks layers from highest to lowest and uses the first match:

| Layer | Priority | Path | Scope |
|-------|----------|------|-------|
| 1. Project | Highest | `<project>/.agentalloy/skills/{system,workflow}/<name>.yaml` | Single repository |
| 2. Profile | Medium | `~/.local/share/agentalloy/profiles/<profile>/skills/{system,workflow}/<name>.yaml` | All repos using this profile |
| 3. Shipped default | Lowest | Bundled in `src/agentalloy/_packs/` | Always present |

**Layer 1 (Project)** — Highest priority. Overrides live inside the project's `.agentalloy/` directory. Use this for project-specific skill customizations that should not affect other repos.

**Layer 2 (Profile)** — Medium priority. Overrides live in the active profile's skills directory. Use this for personal or organizational customizations that apply across multiple projects sharing the same profile.

**Layer 3 (Shipped default)** — Lowest priority. These are the authoritative skills bundled with AgentAlloy. Shipped defaults are immutable; you cannot edit them directly. Instead, you create an override at a higher layer.

### What can be overridden

- **Prose** (`raw_prose`) — the main skill instruction text
- **Gates** (`exit_gates`) — exit gate definitions for workflow skills
- **Applicability** (`applies_when`, `applies_to_phases`) — when and where the skill applies

Override YAML files use the same schema as shipped defaults. Run `agentalloy customize validate <name>` after editing to check your override.

### What cannot be overridden

- **Domain skills** — domain-class skills are centrally curated and not exposed to the customize CLI. Attempting to customize a domain skill returns an error pointing to `docs/skill-authoring-and-overrides-spec.md`.
- **Shipped default files** — the `_packs/` directory is part of the installed package and should not be edited directly.

## CLI: customize

```bash
# List all customizable skills and their active layer
agentalloy customize list [--profile <name>]

# Edit a skill override in $EDITOR (copies from lower layer if needed)
agentalloy customize edit <name> [--profile <name>] [--project]

# Validate an override
agentalloy customize validate <name> [--profile <name>] [--project]

# Validate and ingest into the profile datastore
agentalloy customize update <name> [--profile <name>] [--project]
agentalloy customize update --all [--profile <name>]   # re-ingest all overrides

# Show diff vs next-higher layer
agentalloy customize diff <name> [--profile <name>]

# Delete an override (revert to lower layer)
agentalloy customize reset <name> [--profile <name>] [--project] [--yes]
```

`--profile <name>` targets a specific profile. `--project` targets the project-level override directory. Without either flag, it defaults to the active profile for the current directory.

When you run `customize update`, the override is validated and then ingested into the profile's DuckDB `profile_skills` table. Running `update --all` re-ingests all override files for a profile, useful after bulk edits.

When an override's `raw_prose` is identical to the inherited default, `update` automatically detects this and deletes the override (reverting to the lower layer).

## Profiles and the sidecar watcher

The sidecar watcher (used for harnesses that can't be proxy-wired) is profile-aware:

- **Config file:** `~/.agentalloy/watch/<profile_name>.yaml`
- **PID file:** `~/.agentalloy/watch/<profile_name>.pid`
- **Log file:** `~/.agentalloy/watch/<profile_name>.log`

The watcher uses `profile_name` from its config to load the correct workflow skill prose for phase transitions. When wiring a harness, the watcher config is created with `profile_name: "default"` (see `agentalloy wire-harness`).

See `docs/sidecar-experience.md` for full watcher documentation.

## Profiles and wiring

Wiring is per-repo: `agentalloy wire` injects sentinels into each project's harness config files (e.g., `.cursor/rules/agentalloy-context.mdc`). However, the datastores and skills used by a wired project are determined by the active profile, not by the wiring itself.

This means you can wire the same harness in multiple repos and have them use different skill overrides based on which profile resolves for each repo's directory.

## Example: work vs personal setup

```bash
# Create a work profile that activates for work repos
agentalloy profile init work \
  --match-remote "*github.com/my-company/*" \
  --match-path "~/work/**"

# Override a system skill for work (e.g., stricter commit rules)
agentalloy customize edit commit-safety --profile work
# ... edit in $EDITOR ...
agentalloy customize update commit-safety --profile work

# Personal profile activates for ~/projects/**
agentalloy profile init personal --match-path "~/projects/**"

# Check which profile is active in a given directory
cd ~/work/my-project && agentalloy profile current
# → Profile: work

cd ~/projects/homepage && agentalloy profile current
# → Profile: personal
```

## FORCED_PROFILE environment variable

Set `FORCED_PROFILE=<name>` to override profile auto-detection. Useful for testing and scripted workflows. When set, AgentAlloy always uses the specified profile regardless of cwd, git remote, or project markers.
