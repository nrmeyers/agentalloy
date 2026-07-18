---
phase: build
task_slug: cli-slug-registry-authority
route: fast
domain_tags: [code-index-cli, slug-resolution]
scope:
  touches:
    - src/agentalloy/install/subcommands/code.py       # _slug_from_registry (new) + _resolve_repo_slug(registry-first) + reorder callers to compute port first
    - src/agentalloy/install/subcommands/knowledge.py  # _run_why: compute port before slug
    - tests/install/test_code_cli.py                   # TestResolveSlug
  avoids:
    - the server code_index module        # host-CLI-only fix
    - shipping git into the container      # separate decision (higher blast radius + forced re-index)
    - the indexer's repo_slug()           # left canonical; CLI defers to the registry, doesn't re-derive
  success_criteria:
    - CLI resolves the slug from the service registry (GET /code/repos, repo_path match) so `code search/symbol/...` reach an index the indexer created under a path-derived slug even when the host derives a git-remote slug.
    - Explicit `--repo <slug>` (non-path) short-circuits with ZERO network calls.
    - Graceful fallback to local repo_slug() when the repo isn't registered or the service is down (no crash; service-down error still surfaces).
    - CLI and indexer agree for a repo WITH a git remote (registry slug == what's registered).
    - Version bump 6.9.0 -> 6.9.1 (shipped wheel source changed); no re-index / re-embed.
related_contracts: []
created_at: 2026-07-12T04:08:02Z
---

# cli-slug-registry-authority (Bug B)

Fast-lane fix: the host CLI (`code search/symbol/structural/bundle/remove/watch`, and
`knowledge why`) re-derives the repo slug locally (git-remote → `nrmeyers__agentalloy`)
while the index lives under the indexer's path-derived slug (`agentalloy`), so the CLI
can't reach an index that demonstrably exists. Make the **service registry authoritative**:
resolve the slug from `GET /code/repos` by matching `repo_path`, falling back to local
derivation only on a miss/error. Host-CLI-only; no server change, no re-index.

## Test cases

TestResolveSlug: registry-wins-over-rederive; agreement-with-git-remote; unindexed-fallback;
explicit-slug-short-circuits-with-zero-network; service-down→fallback→normal error.

## Plan

Design provenance: parallel design workflow (Bug B), sequenced as PR-B (first, lowest blast
radius). Rationale + rejected alternatives (code-agreement, git-in-container) in the workflow output.
