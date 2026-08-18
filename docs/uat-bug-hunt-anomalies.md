# UAT Bug Hunt — Anomaly Log

Worktree: `bold-sky-59f87f` (branch `worktree-bold-sky-59f87f`)
Route: full SDD (intake → spec → design → plan → build → qa → ship)
Mode: bare LLM (AgentAlloy proxy OFF for the LLM wrapper; state service still up, so the `agentalloy` CLI drives phase over HTTP).

This log is the primary deliverable of the UAT cycle. Each anomaly has a repro, severity, evidence, and (where a fix is in scope) a root cause + fix.

---

## A1 — Context banner is keyed to the main repo root, not the session's worktree

**Severity:** High (silent, misleading state; the operator sees the wrong phase for the session they're in)

**Symptom:** A session running in the worktree `bold-sky-59f87f` shows a context banner reflecting the **main root's** phase (`plan`), while the worktree's own stream has phase `None`. The banner does not respect the session's worktree.

**Repro:**
1. Start a qwen-code session in the worktree (`.qwen/worktrees/bold-sky-59f87f`).
2. Observe the AgentAlloy context banner.
3. It shows the main root's phase, not the worktree's.

**Root cause (confirmed):**
- The qwen-code `agentalloy` model provider is wired **once, globally**, in `~/.qwen/settings.json`:
  ```
  "baseUrl": "http://127.0.0.1:47950/proj/L2hvbWUvbm1leWVycy9kZXYvYWdlbnRhbGxveQ/v1"
  ```
- The `/proj/<token>` is `base64url(realpath)` of the working dir. `L2hvbWUvbm1leWVycy9kZXYvYWdlbnRhbGxveQ` decodes to **`/home/nmeyers/dev/agentalloy`** (the main root).
- The worktree has **no `.qwen/` of its own** (`.qwen/` is gitignored and not inherited by worktrees), so it falls back to the **global** config and carries the **main-root token** on every request.
- In the proxy, `resolve_working_dir` (`api/proxy_context.py`) has precedence: (0) decoded `/proj` token, (1) `request.metadata["cwd"]`, (2) `AGENTALLOY_PROJECT_DIR` env, (3) `Path.cwd()`. The **token (precedence 0) wins**, so the proxy resolves to the main root and reads the main root's phase.
- Net: the banner is keyed to the main root for **every** session that uses the global `agentalloy` provider, regardless of which worktree the session is actually in.

**Fix (in scope):** see Design — per-worktree harness wiring (each worktree gets its own `/proj` token) and/or proxy preferring the session's actual cwd over a stale global token.

---

## A2 — Worktree is never registered with Agentalloy (no stream, no workflow injection)

**Severity:** High (worktrees are a first-class git concept but Agentalloy treats them as unregistered; no phase state, no workflow instructions)

**Symptom:** The worktree `bold-sky-59f87f` has no `.agentalloy/` directory, no `.stream` binding, and no per-worktree harness config. `agentalloy phase get` in the worktree returns `None` (no phase row), and no workflow instructions are injected.

**Repro:**
1. `ls -a` in the worktree → no `.agentalloy/`, no `.qwen/`.
2. `agentalloy phase get` in the worktree → `None`.
3. `agentalloy phase set intake` → succeeds (creates a phase row on the worktree's stream), confirming the worktree has a distinct store stream but was never *wired*.

**Root cause (confirmed):**
- `.agentalloy/` is **gitignored**, so a git worktree does **not** inherit it from the main checkout.
- The worktree was created by **Qwen Code** (`.qwen/worktrees/`), **not** by `agentalloy worktree <harness> <branch>`. The latter is the intended path: it creates the worktree **and** calls `add.adopt_and_wire(harness, target, ...)`, which writes the per-worktree `.agentalloy/` (stream binding + harness config + its own `/proj` token).
- Because the worktree was never wired, it has no stream binding and no harness config → no workflow injection, phase `None`.
- Note: the store-level `stream_id` *does* isolate worktrees (worktree `71120ebf17fbb914` vs main root `b176e297ab9817cf`), so the *storage* layer is worktree-aware; only the **wiring/registration** layer is not.

**Fix (in scope):** auto-register unregistered git worktrees — detect a worktree (via `git rev-parse --git-common-dir` pointing outside the checkout), create `.agentalloy/`, and `bind_stream_id`. See Design.

---

## A3 — Daemon shell guard over-broad: denies legitimate cross-root CLI mutations

**Severity:** Medium (false positive; blocks a valid operation and forces a workaround)

**Symptom:** A single shell command that chained `cd /home/nmeyers/dev/agentalloy` and `cd .../worktrees/bold-sky-59f87f` (two different repo roots) was denied:
> "Daemon shell guard denied a mutating Git command with a dynamic repository location."

**Repro:**
1. Run one `run_shell_command` that `cd`s into the main root, runs a command, then `cd`s into the worktree and runs a mutating command.
2. The guard denies it.

**Root cause:** The guard treats a command that changes directory across two different repo roots as a "dynamic repository location" and denies mutating Git commands. This is over-broad — the two roots are independent and the mutation is legitimate.

**Workaround (used):** run each repo-root command as a **separate** `run_shell_command` call (with the `directory` param), never chaining `cd`s across repo roots in one command.

**Fix:** out of scope for this cycle — note as a follow-up (tighten the guard to allow distinct, explicitly-targeted repo roots).

---

## A4 — README doc drift: phase is store-backed, not `.agentalloy/phase` — FALSE POSITIVE

**Severity:** Low (documentation accuracy) — **reclassified: no defect, no change**

**Symptom (as reported):** The README was reported to state that phase lives in `.agentalloy/phase`, but that file does not exist in the worktree.

**Resolution:** The current README is already correct. `README.md:157` reads:
> **The phase store.** Phase lives in a per-repo DuckDB state store (not a disk file). …

The only `.agentalloy/phase` mention is the legitimate legacy-migration note ("Legacy repos can migrate their `.agentalloy/phase` file via `POST /import`"). Phase state is store-backed (DuckDB `state.duck`), keyed by `(repo_slug, stream_id)`.

**Root cause of the report:** the observed "phase file absent in the worktree" is actually **A2** (the worktree was never wired, so it has no `.agentalloy/` at all) — not README drift.

---

## A6 — `agentalloy contract artifact-show` can't retrieve a stored artifact (wrong repo)

**Severity:** High (a stored spec/design/plan artifact is un-retrievable via the CLI; breaks the SDD read-back path)

**Symptom:** After `agentalloy contract artifact-set --phase spec --slug uat-bug-hunt --name spec.artifact`, `agentalloy contract artifact-list` shows `spec/uat-bug-hunt/spec.artifact`, but `agentalloy contract artifact-show spec/uat-bug-hunt/spec.artifact` fails:
> Error: artifact spec/uat-bug-hunt/spec.artifact not found
>   Available artifacts for spec/uat-bug-hunt:
>     - spec.artifact

The artifact is listed but not retrievable.

**Repro:**
1. In a worktree (whose repo differs from the service's default repo), `artifact-set` a spec artifact.
2. `artifact-list` → shows it.
3. `artifact-show <phase>/<slug>/<name>` → "not found", even though it is listed.

**Root cause (confirmed):** The service serves every repo from one store and disambiguates by a `repo_root` query param that `StateClient._url()` appends to **every** call. `set_artifact` (via `_put`) and `list_artifacts` (via `_url`) both carry `repo_root`, but `StateClient.get_artifact` (`api/state_client.py`) builds the request as `f"{self.base_url}{path}"` **directly** — it never calls `_url`, so the `repo_root` param is dropped. The get therefore lands in the service's *default* repo (the main root), which does not have the worktree's artifact → 404.

**Fix (in scope):** make `get_artifact` build its URL via `self._url(path)` so it carries `repo_root` like every other state call. One-line change.

---

## A5 — `agentalloy` binary not on PATH

**Severity:** Low (ergonomics; the CLI is not discoverable without the full path)

**Symptom:** `agentalloy` is not on `PATH`. The binary lives at `/home/nmeyers/.local/share/uv/tools/agentalloy/bin/agentalloy` (a uv tool install).

**Repro:** `which agentalloy` → not found; the full path works.

**Root cause:** `preflight`'s `_check_cli_on_path` correctly detects the missing CLI and prints an `export PATH=…` line, but it hard-assumes the binary is in the canonical `~/.local/bin`. A uv tool install whose entry point was not linked there (`--no-install-bin` / a custom `--bin-dir` / a wiped `~/.local/bin`) leaves the real binary in the tool venv's own `bin/`, so the printed line pointed at a dir that does not hold the binary — a wrong, copy-pasteable remediation.

**Fix (in scope):** add `_find_agentalloy_binary()` to `preflight`, which resolves the binary via `shutil.which` and, failing that, searches the known uv tool venv locations (`$UV_TOOL_DIR/agentalloy/bin`, `$XDG_DATA_HOME/uv/tools/agentalloy/bin`). `_check_cli_on_path` now points its `export PATH=…` line at the dir that **actually** holds the binary (falling back to `~/.local/bin` only when the binary cannot be located). Non-invasive: it prints the exact line, it does not rewrite shell profiles. Verified against the live env — it now emits `export PATH="/home/nmeyers/.local/share/uv/tools/agentalloy/bin:$PATH"`.

---

## Summary

| ID | Anomaly | Severity | In scope this cycle |
|----|---------|----------|---------------------|
| A1 | Banner keyed to main root (stale global `/proj` token) | High | Yes (with A2) |
| A2 | Worktree never registered (no `.agentalloy/` / stream / wiring) | High | Yes |
| A3 | Shell guard over-broad (cross-root `cd` denied) | Medium | No (follow-up) |
| A4 | README doc drift (phase is store-backed) | Low | No (false positive) |
| A5 | `agentalloy` binary not on PATH | Low | Yes (preflight PATH remediation) |
| A6 | `artifact-show` drops `repo_root` → 404 on a listed artifact | High | Yes |

The High anomalies split into two roots. **A1 + A2** share one: **AgentAlloy lacks first-class git-worktree support** — worktrees created outside `agentalloy worktree` are never wired, and the global harness config carries a stale `/proj` token that pins the banner to the main root. **A6** is a separate, independent defect: `StateClient.get_artifact` is the only state call that bypasses `_url()`, so it drops the `repo_root` disambiguator and reads the wrong repo.
