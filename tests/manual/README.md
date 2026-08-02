# Manual live-model verification

Nothing here runs in CI. These are operator-driven checks against a **real
model over a real socket**, for the things the `harness_e2e` stub matrix
structurally cannot see: whether a harness's own agent loop keeps carrying the
injected legs across many turns, and what actually lands on the wire.

## `verify-legs.sh`

Verifies **leg 3** — the SDD workflow prose, which must re-send on *every*
carrier turn (#499, #506) — on both OpenAI surfaces in one run:

| harness | wire | leg-3 target |
|---|---|---|
| qwen-code | `/v1/chat/completions` | the `system` message |
| codex | `/v1/responses` | top-level `instructions` |

Four segments, two per harness at two different phases, so it checks both
every-turn delivery and phase *replacement*. Per segment it asserts the system
leg carries the block on every turn, exactly one block, only the current
phase, and byte-identically.

```
VERIFY_REPO=~/dev/scratch-legtest \
REAL_UPSTREAM=http://your-host:60002 \
UPSTREAM_MODEL=qwen3.6-35B-XL \
  bash tests/manual/verify-legs.sh
```

`VERIFY_REPO` is a throwaway git repo already wired with
`agentalloy add qwen-code`; the script wires codex into it itself. Give it a
couple of small source files (`src/x.py`, `tests/test_x.py`) so the tasks have
something to read. Run logs land in `$OUT` (a fresh `mktemp -d` by default).

### Things that will bite you

- **Stop the installed service first.** The script does this itself
  (`systemctl --user stop agentalloy`, restored by an `EXIT` trap), because
  only one process can hold the DuckDB write lock on
  `~/.local/share/agentalloy/corpus/agentalloy.duck`. Killing the process
  instead of stopping the unit just makes systemd respawn it.
- **`RESPONSES_UPSTREAM_URL` takes no `/v1` suffix.** The router appends
  `_UPSTREAM_PATH = "/v1/responses"`. `add --upstream-url` documents its value
  *with* `/v1`; the two are not the same knob. A doubled `/v1` shows up in the
  recorder log as a wrong `path`, which the smoke check catches.
- **The Responses route ignores `.agentalloy/upstream`** (issue #505) — it is
  bound to the process-wide setting above. That is why the script exports it
  rather than relying on `add --upstream-url`.
- **The phase walk is `qa → build → design → ship → qa`** and no other order:
  `phase set` can silently no-op while reporting success (issue #503). Every
  `set_phase` is read back and the run aborts on a mismatch. If you change the
  walk, re-verify each transition persists.
- **`qwen` needs `-m agentalloy-proxy`.** Without it the default provider is
  picked and the proxy is bypassed entirely (issue #504).

## `record_upstream.py`

The recording MITM `verify-legs.sh` sits behind — and useful standalone. It
relays to the real upstream byte-for-byte (chunked, so SSE still streams) and
appends one JSON object per request to `$UPSTREAM_LOG`: turn number, path,
headers, and the fields under test (`instructions`, `system_messages`,
`last_user`, `last_input_item`). It also prints a per-turn count of
`BEGIN AGENTALLOY-CONTEXT` blocks on the system leg as it goes.

```
REAL_UPSTREAM=https://api.openai.com UPSTREAM_LOG=/tmp/up.jsonl \
  python3 tests/manual/record_upstream.py 9999
```

## What this does and does not cover

Leg injection is implemented **once per wire**, not once per harness —
`proxy_router.py` for Chat Completions, `proxy_responses_router.py` for
Responses. Exercising qwen-code and codex therefore exercises both OpenAI
code paths for *every* harness that lands on them.

What stays per-harness forever is **reachability**: whether that harness's own
config format actually points at the proxy. That is not a property of proxy
code and no amount of leg verification covers it — #504 and #505 were both
reachability bugs. Reachability belongs in `tests/harness_e2e/`, which drives
each real harness binary against a stub upstream.
