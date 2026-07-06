# Release & Contribution Runbook

How we branch, commit, PR, version, and tag in this repo. Read this before opening
a PR or cutting a release. The conventions below are what CI and the build
pipeline actually enforce — follow them and merges/builds stay green.

The forge is **GitHub** (`git@github.com:nrmeyers/agentalloy.git`). Use the `gh`
CLI for PRs, merges, and checks.

---

## 1. Branching

- Never commit feature/fix work directly to `main`. Branch first.
- Branch name: `type/short-kebab-summary`, where `type` matches the commit type —
  e.g. `fix/orientation-delivery-and-build-gate`, `feat/tier1-instruction-viewer`.
- Keep one logical change per branch. Don't bundle unrelated work-in-progress; if
  the working tree has unrelated changes, `git stash` them before branching.

**Everything goes through a PR.** `main` has branch protection with required
status checks (since v6.0.0), so direct pushes to `main` are rejected — even
docs-only chores need a branch + PR.

**Stacked PRs** (a PR based on another feature branch) get CI from birth — the
`pull_request` trigger has no branch filter. But squash-merging still breaks
naive stacking: see the gotcha in §7 for the retarget/rebase recipe.

## 2. Commits

Conventional Commits with a scope: `type(scope): subject`.

- Types in use: `feat`, `fix`, `chore`, `docs`, `ci`, `perf`, `style`, `test`.
- Subject in imperative mood, lower-case, no trailing period.
- Body explains the *why* and the root cause, not just the *what*.
- End every commit with the trailer (use the actual authoring model's name):

  ```
  Co-Authored-By: Claude <model> <noreply@anthropic.com>
  ```

## 3. Pull requests

1. Push the branch: `git push -u origin <branch>`.
2. Open the PR against `main` with `gh pr create --base main`.
3. PR title = the same Conventional-Commit summary; append the target version when
   the PR carries a release, e.g. `... (v3.5.2)`.
4. PR body: Context → the problem/root cause → the fix → tests. End with:

   ```
   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   ```

5. Arm auto-merge (repo has it enabled; branches auto-delete on merge):

   ```
   gh pr merge <N> --auto --squash
   ```

   The merge fires when the required checks go green. Squash-merge is the
   convention — each commit on `main` reads `type(scope): subject (#N)`.
   Merging into `main` is gated; get explicit authorization first.

### CI gates (required checks, enforced by branch protection)

Four required checks on `main`: **`quality`**, **`container-tests`**,
**`pipx-smoke`**, **`web-build`** (`.github/workflows/ci.yml`). A PR cannot
merge red.

- `quality`: `uv sync --frozen --extra code-index` → ruff check → ruff format
  --check (formatting is checked **separately** from lint; run `uv run ruff
  format` before pushing) → pyright → `pytest -m "not integration and not
  container"` → pack version-bump guard.
- `container-tests`: builds the deploy image with real podman and runs the
  `container`-marked tests. Runs in parallel with `quality`; skips itself
  (still reporting green) when the diff is docs/images only.
- `pipx-smoke`: builds the core wheel, installs it isolated, smoke-tests the
  CLI surface (including that the `[code-index]` extra is genuinely optional).
- `web-build`: the same tsc + vite build the release pipeline uses.

Reproduce locally before pushing:

```
uv run ruff check . && uv run ruff format --check . && uv run pyright \
  && uv run pytest -m "not integration and not container"
```

The `-m integration` suite (needs a live embed server on 47951) never runs on
PRs — it runs nightly (`corpus-nightly.yml`, `integration-tests` job); failures
open an issue labeled `nightly-integration`. Run it locally before risky
retrieval/embedding changes: `uv run pytest -m integration`.

Tests live under `tests/` and cover the install pipeline (`tests/install/`),
retrieval, composition, applicability filtering, telemetry, and the
harness-wiring catalog.

## 4. Versioning (SemVer)

Version lives in `pyproject.toml` (`[project] version`). Bump per
[SemVer](https://semver.org/), where every tier refers to **shipped code
only**: **patch** = bug fix to shipped behavior, **minor** =
backward-compatible feature, **major** = breaking change. Changes outside the
shipped surface (CI, docs, tests, tooling) have no SemVer tier — they don't
version at all (see below).

### When a bump is required: shipped-surface lockstep

The invariant is NOT "main == last tag" — it is **"a tag's version tells the
truth about shipped content"**: two tags with different versions always differ
in what users actually run. Mechanically:

- A merge **requires a version bump** (in the same PR or a follow-up bump PR
  before the next tag) when its diff touches the **shipped surface**:
  `src/`, `src/agentalloy/_packs/`, `frontend/`, `Containerfile*` /
  `container/`, or dependency pins in `pyproject.toml` / `uv.lock`.
- Merges touching only CI workflows, docs, tests, or repo tooling do **not**
  bump. Main being ahead of the last tag by that class of change is not
  drift — it's the definition.
- Never cut a release tag while unversioned shipped-surface changes sit on
  `main`; bump first.

Rationale: upgrades are not free for users (multi-GB container pull, upgrade
paths with real failure modes), and the release-check nudges every install.
Don't spend that on housekeeping — release when shipped value has accumulated.
The test for "internal change" is just: *does the wheel or image change?* —
answerable from the diff paths. (v6.1.1 predates this rule: it shipped a
byte-identical wheel for CI-only changes; harmless, but the nudge was wasted.)

When you bump the version you MUST also:

- **Regenerate and commit `uv.lock`** in the same change — the lock pins
  `agentalloy`'s own version, so a `pyproject`-only bump leaves them drifted. CI
  uses `uv sync --frozen` (uses the lock as-is) and will **not** catch the drift;
  it only bites a `uv sync --locked` run or a merge. Verify with `uv lock --check`
  (output `Resolved N packages` = clean).

- **Bump the touched pack's `version`** if you edited any `src/agentalloy/_packs/<pack>/`
  content (e.g. `pack.yaml`). Pack propagation is version-gated by design (preserves
  the SkillVersion rollback chain), and a CI guard fails the PR on a content edit
  without a version bump.

## 5. Cutting a release

Create the GitHub **release** (not a bare tag) after the version-bump PR is
merged — the release must exist before the tag build finishes, because
`container-build.yml` uploads `web-dist.tar.gz` onto it with
`gh release upload`:

```
git checkout main && git pull --ff-only origin main
gh release create v<X.Y.Z> --target main --generate-notes \
  --title "v<X.Y.Z> — <one-line theme>"
```

`gh release create` makes the tag and the release in one step and pushing the
tag triggers everything downstream:

- `Container Build & Publish` (`.github/workflows/container-build.yml`) runs on
  both `push` to `main` and the `v*` tag. The tag build publishes the
  release-pinned `ghcr.io/nrmeyers/agentalloy:v<X.Y.Z>` image (corpus baked in)
  and attaches the version-matched `web-dist.tar.gz` to the release.
- The tag must point at the squash-merge commit on `main` (where `pyproject`
  already reads the new version). Don't tag a feature-branch commit.
- Confirm completion with `gh run list --workflow container-build.yml` and
  check the asset landed: `gh release view v<X.Y.Z> --json assets`.

Worked example (v6.0.0, 2026-07-06): bump PR #335 merged → `gh release create
v6.0.0 --target main --generate-notes` → tag build published both arches and
attached web-dist; corpus cache hit (no pack changes) kept it to minutes.

## 6. Quick checklist

- [ ] Branch off `main`, one logical change.
- [ ] Conventional-Commit messages + `Co-Authored-By` trailer.
- [ ] Version bumped in `pyproject.toml` (if releasing); `uv.lock` regenerated
      (`uv lock --check` clean); touched pack `version` bumped.
- [ ] Local gate green: ruff check + ruff format --check + pyright + pytest.
- [ ] PR opened against `main`, required checks green, squash-merged with
      authorization (`gh pr merge --auto --squash`).
- [ ] `gh release create v<X.Y.Z> --target main --generate-notes` on the merge
      commit; container build + web-dist asset confirmed.

## 7. Gotchas seen in past releases

These have bitten releases before; surface them up-front when planning a tag.

- **Merging a squash-based stacked-PR train.** Each PR targets its
  predecessor's branch; after the predecessor squash-merges, retarget the next
  PR at `main` — GitHub does NOT retarget for you here. A plain `git rebase
  main` usually works (patch-id detection skips already-squashed commits), but
  it CONFLICTS when the predecessor's squash contained extra commits touching
  the same files (patch-ids no longer match). Recipe that always works: replay
  only the branch's own commits — `git rebase --onto origin/main
  <old-parent-sha> <branch>` — then force-push (`--force-with-lease`),
  `gh pr edit <N> --base main`, wait for green, merge. Repeat down the stack.
  (Observed on the v6.0.0 train: PRs #332/#333 conflicted after #331's squash
  included two fix commits; `--onto` resolved it cleanly.)
- **Working on a worktree branch that was already merged.** When you stack new
  work on a branch whose previous head already got squash-merged into `main`,
  GitHub sees the still-unsquashed commit as "ahead of main" and the merge ref
  conflicts. CI then never runs on a `pull_request` event. Fix: merge `origin/main`
  into the branch (resolving the trivial `pyproject.toml` / `uv.lock` conflict by
  taking the new-release side) and push — CI registers on the next event. Or
  branch fresh off `main` for the new work instead of extending the merged branch.
- **`gh pr merge --delete-branch` fails from a non-primary worktree.** `gh` tries
  to check out `main` locally to delete the merged branch, which fails when the
  primary worktree already has `main` checked out (`fatal: 'main' is already used
  by worktree at …`). The remote merge still happened — verify with
  `gh pr view <N> --json state,mergeCommit`. Delete the branch on the remote
  manually: `git push origin --delete <branch>`. (Since v6.0.0 the repo has
  delete-branch-on-merge enabled, so `--delete-branch` is usually unnecessary —
  this gotcha only applies to branches kept alive deliberately, e.g. a stack.)
- **Container build is the long pole.** `Container Build & Publish`'s `build-corpus`
  job re-ingests + re-embeds every pack into the image; with new packs or
  resliced fragments this can run ~55 min (observed on v5.1.0, which added one
  pack skill; vs ~6 min for a code-only release). The workflow tolerates up to
  150 min — don't panic at 45-min marks. The
  `main`-push build and the tag-push build run concurrently and don't share the
  embed cache, so total wall time roughly doubles for big releases. Users get the
  new code via `:latest` from the `main`-push as soon as that finishes; the
  `:vX.Y.Z` pinned image follows when the tag build completes.
- **`enable-service` silently skips the rerank/embed units when `llama-server`
  isn't on PATH.** `shutil.which("llama-server")` returns `None` if the
  `pull-models`-generated `~/.local/bin/llama-server` shim was deleted (e.g. by
  `uv tool install --reinstall`). The fallout: rerank/embed services aren't
  registered, no warmup, Stage B disabled. Verify the shim exists before running
  `enable-service`, and recreate it via `agentalloy pull-models` if missing.
