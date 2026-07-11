# Knowledge Module UAT — three-leg practice run (PR #380)

Prove the branch delivers **all three context legs** — skills (compose), codebase
(code index), knowledge (decisions) — through the live proxy in a real harness
session, with negative controls proving the additive guarantee, before merge.

(This lives in `docs/spec-contracts/` — not `docs/qa/`, which is gitignored
per-repo working state — so it rides the PR as reviewable QA evidence.)

## Principles

- Test the **branch code**, not the installed stack (the installed/container proxy
  runs old code).
- Use a **sandbox repo**, never agentalloy itself — `.agentalloy/phase` is one
  shared file per repo; UAT must not contend with dogfood sessions.
- Every leg gets a named **observable** and a named **blind spot**. A leg passes
  only on its observable, not on vibes.

## Rig

### 1. Service under test (branch, from source)

```bash
agentalloy server-stop || true          # stop the installed service first —
                                        # a stale uvicorn holds the DuckDB lock,
                                        # a stale llama-server squats 47951/52
cd ~/dev/claude/agentalloy              # branch checkout (claude/knowledge-module-slice-2)
git status -sb                          # confirm branch + clean
uv sync
CODE_INDEX_ENABLED=1 uv run python -m agentalloy   # uvicorn on 47950, foreground
```

Embed server (llama-server, 47951) stays as installed — the branch doesn't change it.

Preflight: `agentalloy status` / `GET /health` → expect
`modules.code_index == "enabled"` and compose healthy.

### 2. Sandbox repo (purpose-built, deterministic GOVERNS edges)

Pre-scaffolded at `~/dev/claude/uat-sandbox`:

```
uat-sandbox/
  app/limiter.py          # class TokenBucket  (governed code)
  app/retry.py            # def with_backoff   (governed code, dedup case)
  docs/design/rate-limiting/approach.md
      ## Why token bucket        <- backticks `TokenBucket` → GOVERNS edge
  docs/solutions/retry-policy.md
      ## Retry policy            <- backticks `with_backoff` → GOVERNS edge
                                    (docs/solutions/ = the promotable-lesson source)
```

Linker contract the fixtures rely on (`_index_decisions`, DK2): decision sources
are exactly `docs/solutions/*.md` / `docs/design/*/approach.md`, and a decision
chunk governs a symbol only via a **backtick-fenced span** — either the exact
FQN, or a code-shaped short name (`TokenBucket`, `with_backoff`) matching exactly
one code symbol. Each fixture name is unique in the sandbox by construction.

```bash
cd ~/dev/claude/uat-sandbox
agentalloy add                          # wire the sandbox to the proxy
agentalloy code index . --wait          # build graph + vectors
```

### 3. Preflight assertions (no session yet — pull paths only)

| # | Command | Expect | Leg |
|---|---------|--------|-----|
| P1 | `agentalloy code status` | sandbox listed, not stale | codebase |
| P2 | `agentalloy code search "token bucket"` | hits `app/limiter.py` | codebase |
| P3 | `agentalloy code symbol <TokenBucket-fqn>` (fqn from P2's hit) | full symbol row | codebase |
| P4 | `agentalloy knowledge why <TokenBucket-fqn>` | "Why token bucket" decision | knowledge |
| P5 | `agentalloy knowledge why <with_backoff-fqn>` | "Retry policy" decision | knowledge |

(Resolve the exact FQNs from P2's output — the indexer's qualified-name scheme,
not a guess.) Any preflight failure = stop; the session script can't be meaningful.

## Session script (colleague drives Claude Code in the sandbox — fresh eyes are the point)

Observation instrument for injected context: ask the agent, at each checkpoint,
to **quote verbatim every `#`-heading it received this turn that it did not write
itself**. (Blind spot: paraphrase — the instruction must say *verbatim*.)
Cross-check every checkpoint against telemetry: `agentalloy telemetry` →
one `proxy_composed` row per composing request.

| Step | Action | Expected observable | Leg(s) |
|------|--------|--------------------|--------|
| S1 | Fresh session, real prompt ("add a burst-allowance option to the rate limiter") | Intake orientation block; agent routes + writes a contract, does NOT build | skills (Tier 1) |
| S2 | Advance to design; work-item contract carries `domain_tags` (≤2) and `scope.touches: ["app/limiter.py"]` | On the **cursor-entry turn**: workflow prose (skills) + `sys-code-index` guidance (codebase) + `# Decisions governing this work` containing "Why token bucket" (knowledge) — all three legs in ONE turn | all three |
| S3 | Next ordinary turn (same work-item) | NO repeated decision block, no repeated Tier-2 — cadence is once per work-item entry | knowledge (cadence) |
| S4 | During build, agent runs `agentalloy code search` / `symbol` / `callers` on TokenBucket | Correct symbols/locations returned; agent uses them | codebase (pull) |
| S5 | Dedup case A: promote `docs/solutions/retry-policy.md` to a lesson skill; new work-item touching `app/retry.py` whose tags MATCH the lesson | Lesson skill composes in Tier-2; decision block **omits** the retry decision (deferred — no double-inject) | knowledge × skills (DK4) |
| S6 | Dedup case B: same touch, tags that DON'T match the lesson | Lesson absent from Tier-2 → decision **pushed** (the no-silent-gap case) | knowledge (DK4) |

## Negative controls (the additive guarantee — what makes it safe to merge)

| Step | Action | Expected |
|------|--------|----------|
| N1 | Restart service with `CODE_INDEX_ENABLED=0`; repeat S2's work-item entry | Composes with NO decision block, NO sys-code-index; skills leg intact and byte-equivalent to pre-Knowledge behavior |
| N2 | `CODE_INDEX_ENABLED=1` but sandbox index removed (`agentalloy code remove`) | Same as N1 — unindexed degrades identically (fail-closed) |
| N3 | Work-item contract with empty `scope.touches` at design | Skills + codebase legs fire; no decision block |

## Pass / fail

- **Pass** = P1–P5, S1–S6, N1–N3 all green. Merge #380.
- Any S-step red = feature defect → fix on the branch, re-run the failed step.
- Any N-step red = **additivity defect → hard stop** (this is the hot-path risk
  the full-feature-merge policy exists for).

## Cleanup

```bash
agentalloy code remove ~/dev/claude/uat-sandbox --yes
cd ~/dev/claude/uat-sandbox && agentalloy unwire
# stop the source uvicorn (Ctrl-C), restart the installed service
```

Time-box: ~1–2 h with the sandbox prepared; sandbox prep ~20 min.
