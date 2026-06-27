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

Direct-to-`main` is acceptable only for trivial, low-risk chores (docs, a
`uv.lock` sync) — and even then, prefer a PR when in doubt.

## 2. Commits

Conventional Commits with a scope: `type(scope): subject`.

- Types in use: `feat`, `fix`, `chore`, `docs`, `ci`, `perf`, `style`, `test`.
- Subject in imperative mood, lower-case, no trailing period.
- Body explains the *why* and the root cause, not just the *what*.
- End every commit with the trailer:

  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
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

5. **Wait for CI to pass**, then squash-merge and delete the branch:

   ```
   gh pr merge <N> --squash --delete-branch
   ```

   Squash-merge is the convention — each commit on `main` reads
   `type(scope): subject (#N)`. Merging into `main` is gated; get explicit
   authorization, and only merge on green CI.

### CI gates (must be green before merge)

The `quality` job (`.github/workflows/ci.yml`) runs, in order:

- `uv sync --frozen`
- `uv run ruff check .`
- `uv run ruff format --check .`  ← formatting is checked **separately** from lint;
  run `uv run ruff format` before pushing
- `uv run pyright`
- `uv run pytest -m "not integration and not container"`
- `uv build`

Plus a `pipx-smoke` job. Reproduce locally before pushing:

```
uv run ruff check . && uv run ruff format --check . && uv run pyright \
  && uv run pytest -m "not integration and not container"
```

## 4. Versioning (SemVer)

Version lives in `pyproject.toml` (`[project] version`). Bump per
[SemVer](https://semver.org/): **patch** = bug fix / internal change, **minor** =
backward-compatible feature, **major** = breaking change.

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

## 5. Tagging a release

Tags trigger the container & package build, so tag **after** the version-bump PR
is merged, on `main`:

```
git checkout main && git pull --ff-only origin main
git tag -a v<X.Y.Z> -m "v<X.Y.Z>"     # annotated; message is just the version
git push origin v<X.Y.Z>
```

- The tag must point at the squash-merge commit on `main` (where `pyproject` already
  reads the new version). Don't tag a feature-branch commit.
- `Container Build & Publish` (`.github/workflows/container-build.yml`) runs on both
  `push` to `main` and `push` of a `v*` tag, publishing images to `ghcr.io`. The
  tag build produces the release-pinned image; allow it ~minutes (it bakes the
  corpus). Confirm with `gh run list`.

## 6. Quick checklist

- [ ] Branch off `main`, one logical change.
- [ ] Conventional-Commit messages + `Co-Authored-By` trailer.
- [ ] Version bumped in `pyproject.toml` (if releasing); `uv.lock` regenerated
      (`uv lock --check` clean); touched pack `version` bumped.
- [ ] Local gate green: ruff check + ruff format --check + pyright + pytest.
- [ ] PR opened against `main`, CI green, squash-merged with authorization.
- [ ] Annotated `v<X.Y.Z>` tag pushed on the merge commit; container build confirmed.

## 7. Gotchas seen in past releases

These have bitten releases before; surface them up-front when planning a tag.

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
  manually: `git push origin --delete <branch>`.
- **Container build is the long pole.** `Container Build & Publish`'s `build-corpus`
  job re-ingests + re-embeds every pack into the image; with new packs or
  resliced fragments this can run 25–35 min (vs ~5 min for a code-only release).
  The workflow tolerates up to 150 min — don't panic at 15-min marks. The
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
