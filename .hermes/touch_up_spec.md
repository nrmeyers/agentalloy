# Setup polish: AMD detection, embed log noise, real query smoke test

## Context

Three issues observed during a fresh `agentalloy` setup on a Strix Point (AMD APU) host:

1. **AMD GPU not detected as `radeon` target.** `_derive_host_target` only maps *discrete* AMD cards to `"radeon"`. AMD APUs (Strix Point Radeon 890M, Phoenix 780M, etc.) appear in the `integrated` list, so the function falls through to `"cpu"` and the user is offered the CPU compose file / preset instead of the Radeon one.
2. **Per-request httpx logs spam the embedding phase.** Each fragment embed emits `INFO HTTP Request: POST .../v1/embeddings "HTTP/1.1 200 OK"` from `httpx`, plus a `embedded N/M` tally every 10 fragments. With hundreds of fragments the httpx lines drown the tally.
3. **Verify-manually step shows a meaningless `"hello"` curl.** The current `_test_embed_endpoint` POSTs a string to `/v1/embeddings` (good as a server liveness check) but the printed manual-verify command is just a raw embeddings curl. The user wants a second, end-to-end test that exercises an actual *skill query* through the retrieval pipeline, and to print that test as the "Verify Manually" example.

## Changes

### 1. AMD APU → `radeon` target

**File:** `src/agentalloy/install/subcommands/simple_setup.py:449-473` (`_derive_host_target`)

Add a fourth priority rule **between** the existing discrete-AMD and Apple-Silicon checks:

```python
# AMD integrated (APU: Strix Point, Phoenix, Hawk Point, etc.)
for card in integrated:
    if str(card.get("vendor") or "").lower() == "amd":
        return "radeon"
```

Updated priority: NVIDIA discrete → AMD discrete → **AMD integrated (new)** → Apple integrated → `cpu`. Update the docstring to match.

No change needed in `detect.py`: AMD APU GPUs are already correctly labeled `vendor="amd"` and pushed to `integrated` (lines 282-283, 295-299). The bug is purely the target derivation.

### 2. Quieter embedding progress

**File:** `src/agentalloy/reembed/cli.py`

- In `main()` around line 427 (after `logging.basicConfig`), silence `httpx` request logs:
  ```python
  logging.getLogger("httpx").setLevel(logging.WARNING)
  ```
- Replace the every-10 tally at line 409-410 with a single-line, in-place updating progress write to stderr using `\r`:
  ```python
  print(f"\r  embedded {stats.embedded}/{stats.discovered}", end="", file=sys.stderr, flush=True)
  ```
  Then emit a single newline once the loop exits so the summary line below isn't pinned to the progress line. Skip the `\r` form when stderr isn't a TTY (CI / log capture) — fall back to the existing periodic logger.info.
- Keep `stats.log_summary()` at line 562 as the post-run summary; it already prints `discovered / skipped / embedded / failed`. No change needed there — that *is* the "summary of successful and unsuccessful" the user asked for.

### 3. End-to-end skill-query smoke test under "Verify Manually"

**File:** `src/agentalloy/install/subcommands/simple_setup.py`

In `_test_embed_endpoint` (lines 558-602):

- Keep the existing `/v1/embeddings` POST as the first liveness check (unchanged success line).
- After it passes, add a second test that performs a real retrieval-pipeline query. Reuse the existing function:
  - `agentalloy.retrieval.domain.retrieve_domain_candidates(task=..., ...)` (`src/agentalloy/retrieval/domain.py:171`).
  - Use a short, realistic task string like `"add a new pytest test for a CLI subcommand"`.
  - Surface the count of returned candidates and the top skill_id as the success message: `  Skill query test: OK -- returned N candidates (top: <skill_id>)`.
- Replace the existing `Verify manually:` block (lines 592-596) with a `curl` against the running AgentAlloy proxy. The only `[project.scripts]` entry is `agentalloy = agentalloy.install.__main__:main` (pyproject.toml:36), so there is no public skill-query CLI to point users at. The proxy itself (`src/agentalloy/api/proxy_router.py`) drives retrieval on every chat completion request, so a curl that hits the proxy `/v1/chat/completions` with a short coding task is the most faithful "what a real client does" verification. Form:
  ```
  curl -s http://localhost:<port>/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"<model>","messages":[{"role":"user","content":"add a pytest for the CLI"}]}'
  ```
  Use `cfg.port` and `cfg.upstream_model` (or whatever model the proxy advertises) for the substitutions.

If the skill-query test fails (e.g. DuckDB not populated yet, model dim mismatch), print a yellow warning but do not fail setup — same pattern the embed test already uses.

## Files touched

- `src/agentalloy/install/subcommands/simple_setup.py` — `_derive_host_target` (AMD APU), `_test_embed_endpoint` (add real query, swap "Verify manually" body).
- `src/agentalloy/reembed/cli.py` — silence httpx, in-place progress line.

## Verification

1. **AMD detection** — on the Strix Point host: run `agentalloy setup` (or just the auto-detect path) and confirm the recommended host renders as **AMD GPU (Vulkan/ROCm)** and `default_compose` (line 650) picks `compose.radeon.yaml`. Optionally call `_derive_host_target` against a synthetic `detect.json` with an integrated AMD entry to confirm `"radeon"`.
2. **Log noise** — run `python -m agentalloy.reembed --force --limit 50`. Confirm no `INFO HTTP Request:` lines appear and the embedded count updates on a single line, followed by the `re-embed complete: ...` summary.
3. **Skill query smoke test** — run `agentalloy setup` end-to-end. Confirm the new `Skill query test: OK` line appears under "Testing embed endpoint" and the printed "Verify manually" command, when copy-pasted into the shell, returns the same kind of result.
