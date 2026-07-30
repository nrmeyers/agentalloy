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

Five required checks on `main`: **`quality`**, **`container-tests`**,
**`pipx-smoke`**, **`web-build`** (`.github/workflows/ci.yml`), and
**`version-bump`** (`.github/workflows/version-bump.yml`, §4 — derives the
version bump; required so auto-merge can't fire on an un-bumped SHA). A PR
cannot merge red.

- `quality`: `uv sync --frozen --no-dev` → ruff check → ruff format
  --check (formatting is checked **separately** from lint; run `uv run ruff
  format` before pushing) → pyright → `pytest -m "not integration and not
  container"` → pack version-bump guard.
- `container-tests`: builds the deploy image with real podman and runs the
  `container`-marked tests. Runs in parallel with `quality`; skips itself
  (still reporting green) when the diff is docs/images only.
- `pipx-smoke`: builds the core wheel, installs it isolated, smoke-tests the
  CLI surface (including that the `[code-index]` extra is genuinely optional).
- `web-build`: the same tsc + vite build the release pipeline uses.

Reproduce locally before pushing (recommended — one command):

```
scripts/local-ci.sh
```

Or run the checks individually:

```
uv run ruff check . && uv run ruff format --check . && uv run pyright \
  && uv run pytest -m "not integration and not container"
```

**Pre-commit hooks.** Install them once with `uv run pre-commit install` — they
auto-fix lint issues and format on every commit, catching the formatting
mistakes that previously leaked into PRs. The hooks are configured in
`.pre-commit-config.yaml` (ruff auto-fix + ruff-format). After installing,
every `git commit` will automatically run `ruff --fix` and `ruff format` on
staged files.

The `-m integration` suite (needs a live embed server on 47951) never runs on
PRs — it runs nightly (`corpus-nightly.yml`, `integration-tests` job); failures
open an issue labeled `nightly-integration`. Run it locally before risky
retrieval/embedding changes: `uv run pytest -m integration`.

Tests live under `tests/` and cover the install pipeline (`tests/install/`),
retrieval, composition, applicability filtering, telemetry, and the
harness-wiring catalog.

## 4. Versioning (SemVer)

Version lives in `pyproject.toml` (`[project] version`). Every tier refers to
**shipped code only**: **patch** = bug fix to shipped behavior, **minor** =
backward-compatible feature, **major** = breaking change. Changes outside the
shipped surface (CI, docs, tests, tooling) have no SemVer tier — they don't
version at all.

### The bump is automatic — you do not choose the tier or edit `pyproject`

`Version Bump` (`.github/workflows/version-bump.yml`) derives and applies the
bump on every PR. You never open a `chore(release)` PR and never hand-edit the
version. Two deterministic gates (`scripts/version_bump.py`, unit-tested in
`tests/test_version_bump.py`):

- **Whether to bump — shipped-surface path gate.** A bump happens only when the
  PR's diff touches the **shipped surface**: `src/` (incl. `src/agentalloy/_packs/`),
  `frontend/`, `Containerfile*` / `container/`, or dependency pins in
  `pyproject.toml` / `uv.lock`. PRs touching only CI, docs, tests, or tooling do
  **not** bump — main being ahead of the last tag by that class of change is not
  drift, it's the definition.
- **Which tier — from the PR title's Conventional-Commit type.** `feat!` or a
  `BREAKING CHANGE` marker → **major**; `feat` → **minor**; **everything else**
  touching shipped code (`fix`, `perf`, `refactor`, `chore`, …) → **patch**. So
  the only lever you touch is the PR title — get the type right and the version
  follows.

When it fires, the workflow rewrites `pyproject.toml`, regenerates `uv.lock`,
and commits both to your PR branch (`chore: bump version to X.Y.Z`). CI re-runs
on that commit; merging the PR IS cutting the release (§5). The bump commit is
squashed away on merge, so `main` reads `type(scope): subject (#N)` as always.

The invariant this upholds: **"a tag's version tells the truth about shipped
content"** — two tags with different versions always differ in what users run.
"else → patch" honors it (any shipped-surface change versions), while `feat`
and `feat!`/`BREAKING` still escalate. Rationale for gating on shipped surface:
upgrades aren't free for users (multi-GB container pull, real upgrade failure
modes) and the release-check nudges every install — don't spend that on
housekeeping. The test for "internal change" is just: *does the wheel or image
change?*

Notes and escapes:

- **`BUMP_TOKEN` is required infra.** The bump commit is pushed with the
  fine-grained PAT in repo secret `BUMP_TOKEN` (Contents + Pull requests: write),
  not `GITHUB_TOKEN` — the default token's pushes don't trigger workflows
  (GitHub anti-recursion, same rule as tags in §5), which would leave the bumped
  commit with no checks and deadlock auto-merge. If bumps stop happening, check
  that secret first.
- **`version-bump` is a required status check.** It only goes green on the
  fully-bumped SHA, so auto-merge can't fire on an un-bumped commit.
- **A manual bump still works.** If you (or a tool) already bumped `pyproject` on
  the branch, the workflow is a clean no-op (it compares against the PR base).
- **First tier wins.** Once the bump commit has landed on the branch, editing the
  PR title does **not** re-derive the version (idempotency stops it). If you got
  the type wrong, drop the bump commit (`git rebase`/reset the branch) and let it
  recompute — or hand-set `pyproject` to the version you want. Adding shipped code
  later via a new commit is fine: an un-bumped PR always computes fresh.
- **Touched-pack version.** If you edited any `src/agentalloy/_packs/<pack>/`
  content, still bump that pack's own `version` — pack propagation is
  version-gated by design (SkillVersion rollback chain), and the `quality` job's
  pack guard fails the PR on a content edit without it. This is separate from the
  project version and is **not** automated.

## 5. Cutting a release

The cut is **automated**: when a PR carrying a version bump (auto-derived and
committed to the branch by `Version Bump`, per §4) merges to main and that
commit's CI goes green, `Release Cut` (`.github/workflows/release-cut.yml`)
creates the GitHub release `v<X.Y.Z>` (tag on the merge commit, title themed
from the bumping PR, generated notes) and dispatches `Container Build & Publish`
on the new tag. Merging the bumped PR IS cutting the release — nothing to run.

What the automation guarantees, and why it's shaped this way:

- The **release exists before the tag build finishes** — `container-build.yml`
  uploads `web-dist.tar.gz` onto it with `gh release upload`, so the release
  is created first and the build dispatched second.
- The tag build publishes the release-pinned
  `ghcr.io/nrmeyers/agentalloy:v<X.Y.Z>` image (corpus baked in) and attaches
  the version-matched `web-dist.tar.gz` to the release.
- The build is **dispatched explicitly** (`workflow_dispatch --ref v<X.Y.Z>`)
  because tags created with `GITHUB_TOKEN` do not fire `on: push: tags`
  workflows.
- Non-bump merges and re-runs are no-ops (version already tagged); a red CI
  run cuts nothing. The version bump itself is derived automatically on the PR
  (§4); both the bump and the cut are mechanized.
- Release title/notes are editable after the fact (`gh release edit`); the
  automation never touches an existing release or tag.

Confirm completion with `gh run list --workflow container-build.yml` and
check the asset landed: `gh release view v<X.Y.Z> --json assets`.

**Manual fallback** (automation down or cutting from an unusual state):

```
git checkout main && git pull --ff-only origin main
gh release create v<X.Y.Z> --target main --generate-notes \
  --title "v<X.Y.Z> — <one-line theme>"
```

The tag must point at the squash-merge commit on `main` (where `pyproject`
already reads the new version) — never a feature-branch commit. A manually
pushed tag (your credentials, not `GITHUB_TOKEN`) triggers the container
build itself; don't also dispatch it.

## 6. Quick checklist

- [ ] Branch off `main`, one logical change.
- [ ] Conventional-Commit messages + `Co-Authored-By` trailer.
- [ ] PR title's Conventional-Commit type is correct — it drives the automatic
      version bump (§4). `Version Bump` writes `pyproject.toml` + `uv.lock` on the
      branch; you don't hand-bump. Touched pack `version` still bumped by hand.
- [ ] Pre-commit hooks installed (`uv run pre-commit install`) — they auto-format
      on every commit, preventing formatting leaks into PRs.
- [ ] Local gate green: `scripts/local-ci.sh` (or the individual commands below).
- [ ] PR opened against `main`, required checks green, squash-merged with
      authorization (`gh pr merge --auto --squash`).
- [ ] If the PR bumped the version: `Release Cut` created `v<X.Y.Z>` after CI
      went green; container build + web-dist asset confirmed.

## 7. Gotchas seen in past releases

These have bitten releases before; surface them up-front when planning a tag.

- **pre-commit hooks not installed.** If `.git/hooks/pre-commit` doesn't exist,
  the `.pre-commit-config.yaml` hooks never run — formatting and lint issues
  leak into PRs and fail CI. Always run `uv run pre-commit install` before
  committing. The hooks auto-fix (`ruff --fix`) and auto-format (`ruff format`)
  on every commit, so issues are caught at commit time, not in CI.
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
