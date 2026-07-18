---
phase: intake
task_slug: auto-release-cut
route: fast
domain_tags: [ci]
scope:
  touches:
    - .github/workflows/                     # new workflow: cut the GitHub release when a version bump lands on main
    - RELEASE.md                             # §5 becomes "automated; here is the manual fallback" — one policy, no residue
  avoids:
    - container-build.yml release/web-dist upload logic   # consumer of the release; keep its contract, don't move it
    - auto-bumping the version                             # the bump stays a deliberate human decision in the PR; only the CUT is automated
    - publishing anything when the version already has a tag
success_criteria:
    - When a merge to main changes pyproject.toml's version to one with no existing tag, a GitHub release (tag v<version>) is created automatically — but only after main CI for that commit is green.
    - Release title/notes derive from the bump PR (title + body) or --generate-notes; a human can edit the release afterward without fighting the automation.
    - Ordering preserved: the release exists before container-build.yml's tag job uploads web-dist.tar.gz (today's manual-cut invariant, RELEASE.md §5).
    - Idempotent and safe on non-bump merges: no version change → no-op; tag already exists → no-op; never re-tags or force-moves a tag.
    - RELEASE.md updated so the automated path is THE path (manual gh release create documented only as fallback).
    - CI-only change → no version bump (shipped-surface rule).
related_contracts:
  - contracts/intake/codex-provider-toml-dependency.md  # v6.3.1 cycle where the manual cut friction showed up
created_at: 2026-07-07T16:40:00Z
---

# auto-release-cut

## What the user actually wants

Every version-bump merge to main should produce its GitHub release without a
human running `gh release create`. The manual step is pure mechanics with an
ordering constraint (release before the tag's container build finishes, so
web-dist lands on it) — forgetting it strands a bump untagged, which the
shipped-surface rule explicitly forbids. Motivated live during the v6.3.1
cycle: the cut was the only step that needed a human, and the first attempt
was permission-blocked.

## Shape

A workflow on push to main (path-filtered to pyproject.toml) that reads the
project version, exits if a matching tag exists, waits for / gates on the
commit's CI success, then creates release v<version> targeting the merge
commit with notes sourced from the bump PR. The version BUMP decision remains
human, in the PR; only the cut is mechanized.
