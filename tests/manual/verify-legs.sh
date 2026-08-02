#!/usr/bin/env bash
# Live leg-3 verification for BOTH OpenAI surfaces, in one run.
#
#   qwen-code -> /v1/chat/completions  (leg 3 on the system message)
#   codex     -> /v1/responses         (leg 3 on top-level `instructions`)
#
# Two runs per harness at two different phases, so we test both:
#   (a) every-turn delivery   -- the #499 deliver-once regression
#   (b) phase replacement     -- run B must carry ONLY phase B, never phase A
#
# Phase walk uses only transitions proven to persist (see issue #503):
#   qa -> build -> design -> ship -> qa
# and every `phase set` is READ BACK; a silent no-op aborts the run.
#
# Stops the installed agentalloy service (it holds the duckdb write lock),
# runs the WORKTREE build in its place, restores the service on exit, always.
#
# Required:
#   VERIFY_REPO      a scratch git repo, already `agentalloy add qwen-code`d
#   REAL_UPSTREAM    the real OpenAI-compatible server to relay to
# Optional:
#   UPSTREAM_MODEL   model id to ask for (default gpt-4o-mini)
#   RECPORT          recorder port (default 9999)
#   OUT              directory for run logs (default a fresh mktemp -d)
#
#   VERIFY_REPO=~/dev/scratch-legtest REAL_UPSTREAM=http://host:60002 \
#     UPSTREAM_MODEL=qwen3.6-35B-XL bash tests/manual/verify-legs.sh
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$HERE/../.." && pwd)

die() { echo "!! $*" >&2; exit 1; }

SCRATCH=${VERIFY_REPO:-}
[[ -n $SCRATCH ]] || die "set VERIFY_REPO to a scratch repo wired with 'agentalloy add qwen-code'"
[[ -d $SCRATCH ]] || die "VERIFY_REPO=$SCRATCH does not exist"
REAL=${REAL_UPSTREAM:-}
[[ -n $REAL ]] || die "set REAL_UPSTREAM to the OpenAI-compatible server to relay to"
MODEL=${UPSTREAM_MODEL:-gpt-4o-mini}
RECPORT=${RECPORT:-9999}
OUT=${OUT:-$(mktemp -d -t agentalloy-verify-legs-XXXXXX)}
LOG=$OUT/upstream-log.jsonl
echo "--- logs: $OUT"

SERVE_PID=""; REC_PID=""
cleanup() {
  echo
  echo "--- restoring"
  [[ -n $SERVE_PID ]] && kill "$SERVE_PID" 2>/dev/null
  [[ -n $REC_PID   ]] && kill "$REC_PID"   2>/dev/null
  sleep 1
  systemctl --user start agentalloy && echo "installed agentalloy service restarted"
}
trap cleanup EXIT

# --- 0. preflight -----------------------------------------------------------
echo "--- preflight"
if ss -ltn 2>/dev/null | grep -q ":$RECPORT "; then
  die "port $RECPORT already in use -- a stale recorder is running. \
Kill it (pkill -f record_upstream.py) and re-run."
fi
command -v qwen  >/dev/null || die "qwen not on PATH"
command -v codex >/dev/null || die "codex not on PATH"
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-local-dummy}"
echo "    ok"

echo "--- stopping installed service (it holds the duckdb write lock)"
systemctl --user stop agentalloy
sleep 2

# --- 1. recorder ------------------------------------------------------------
echo "--- starting recorder :$RECPORT -> $REAL"
rm -f "$LOG"
REAL_UPSTREAM=$REAL UPSTREAM_LOG=$LOG python3 "$HERE/record_upstream.py" "$RECPORT" \
  >"$OUT/recorder.out" 2>&1 &
REC_PID=$!
sleep 1
ss -ltn 2>/dev/null | grep -q ":$RECPORT " || {
  cat "$OUT/recorder.out"; die "recorder failed to bind :$RECPORT"; }
echo "    up"

# --- 2. worktree proxy ------------------------------------------------------
echo "--- starting WORKTREE build on :47950"
cd "$REPO" || die "no $REPO"
# The /v1/responses route does NOT read .agentalloy/upstream -- it is an
# auth-transparent passthrough bound to a process-wide setting that defaults to
# https://api.openai.com (config.py:97, app.py:278; issue #505). Point it at
# the recorder.
# NOTE: no /v1 suffix -- the router appends _UPSTREAM_PATH="/v1/responses".
export RESPONSES_UPSTREAM_URL="http://localhost:$RECPORT"
uv run agentalloy serve --port 47950 >"$OUT/serve.out" 2>&1 &
SERVE_PID=$!
for _ in $(seq 1 60); do
  curl -sf -m 2 http://127.0.0.1:47950/health >/dev/null 2>&1 && break
  sleep 1
done
curl -sf -m 2 http://127.0.0.1:47950/health >/dev/null 2>&1 || {
  tail -20 "$OUT/serve.out"; die "worktree service failed to start"; }
echo "    up"

TOKEN=$(cd "$REPO" && uv run python -c \
  "from agentalloy.api.proxy_context import encode_proj_token; from pathlib import Path; print(encode_proj_token(Path('$SCRATCH')))")
[[ -n $TOKEN ]] || die "could not compute proj token"
BASE="http://localhost:47950/proj/$TOKEN/v1"
echo "    proj base: $BASE"

# --- 3. wire codex (qwen is already wired) ----------------------------------
cd "$SCRATCH" || die "no $SCRATCH"
echo "--- wiring codex (Responses wire)"
uv --project "$REPO" run agentalloy add codex \
  --upstream-url "http://localhost:$RECPORT/v1" \
  --upstream-model "$MODEL" \
  --key-env OPENAI_API_KEY \
  --lifecycle-mode full 2>&1 | tail -5

echo "--- .agentalloy/upstream after add:"
sed 's/^/    /' "$SCRATCH/.agentalloy/upstream"
grep -q "localhost:$RECPORT" "$SCRATCH/.agentalloy/upstream" \
  || die "add rewrote the upstream away from the recorder -- chat/completions would be unrecorded"
echo "    (note: this file governs /v1/chat/completions only; the /v1/responses"
echo "     route is bound to RESPONSES_UPSTREAM_URL, exported above)"

# pre-trust the repo so `codex exec` never blocks on a trust prompt
python3 - "$SCRATCH" <<'PY'
import sys, pathlib
root = pathlib.Path(sys.argv[1])
cfg = root / ".codex" / "config.toml"
txt = cfg.read_text()
if "trust_level" not in txt:
    cfg.write_text(txt + f'\n[projects."{root}"]\ntrust_level = "trusted"\n')
    print("    pre-trusted the project in .codex/config.toml")
PY

# --- 4. smoke: hit the Responses route directly -----------------------------
echo "--- smoke: POST $BASE/responses"
# The Responses passthrough forwards the caller's credential verbatim and adds
# none of its own, so the smoke request must carry an Authorization header just
# like codex does (env_key = OPENAI_API_KEY).
curl -s -m 60 -X POST "$BASE/responses" -H 'content-type: application/json' \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d "{\"model\":\"$MODEL\",\"input\":\"say OK\",\"max_output_tokens\":16}" \
  >"$OUT/smoke.out" 2>&1
head -c 200 "$OUT/smoke.out"; echo
python3 - "$LOG" "$OUT/serve.out" <<'PY'
import json, os, sys
if not os.path.exists(sys.argv[1]):
    print("!! SMOKE FAIL: recorder log does not exist -- nothing reached the recorder.")
    print("   Where the proxy actually sent it (from serve.out):")
    for line in open(sys.argv[2]):
        if "HTTP Request: POST" in line and "responses" in line:
            print("     " + line.strip())
    sys.exit(1)
rows = [json.loads(l) for l in open(sys.argv[1])]
if not rows:
    sys.exit("!! SMOKE FAIL: recorder saw nothing -- the proxy never forwarded")
r = rows[-1]
ins = r.get("instructions")
n = (ins or "").count("BEGIN AGENTALLOY-CONTEXT")
print(f"    path={r['path']}  instructions_len={len(ins or '')}  leg3_blocks={n}")
if r["path"] != "/v1/responses":
    sys.exit(f"!! SMOKE FAIL: upstream path was {r['path']!r}, expected '/v1/responses' "
             "(check for a doubled /v1 in the upstream base)")
if n != 1:
    sys.exit(f"!! SMOKE FAIL: expected exactly 1 leg-3 block on `instructions`, got {n}")
print("    SMOKE PASS")
PY
[[ $? -eq 0 ]] || exit 1
: >"$LOG"   # discard the smoke row; the real runs start clean

# --- 5. four runs -----------------------------------------------------------
mark() { echo "{\"marker\":\"$1\",\"phase\":\"$2\"}" >>"$LOG"; }

set_phase() {   # set_phase <phase> ; aborts if it silently no-ops (#503)
  uv --project "$REPO" run agentalloy phase set "$1" >/dev/null 2>&1
  local got
  got=$(uv --project "$REPO" run agentalloy phase get 2>/dev/null | awk '/^Phase:/{print $2; exit}')
  [[ $got == "$1" ]] || die "phase set $1 did not persist (phase get -> '${got:-?}') -- issue #503"
  echo "    phase now: $got"
}

# Multi-step tasks on purpose: each tool call is another upstream turn, and the
# deliver-once class of bug is invisible on turn 1.
TASK_A="Read src/x.py. Then read tests/test_x.py. Then run 'ls -1' with the shell. Then say DONE and stop."
TASK_B="Run 'ls -1' with the shell, then run 'pwd', then read src/x.py, then say DONE and stop."
export QWEN_HOME="$SCRATCH/.qwen"
export CODEX_HOME="$SCRATCH/.codex"

echo; echo "--- run 1/4: qwen @ build"
set_phase build
mark qwen-A build
# -m is required: without it qwen picks its default provider and bypasses the
# proxy entirely (issue #504).
timeout 300 qwen -m agentalloy-proxy -p "$TASK_A" >"$OUT/run-qwen-a.out" 2>&1
echo "    done (exit $?)"

echo; echo "--- run 2/4: qwen @ design  (replacement check vs build)"
set_phase design
mark qwen-B design
timeout 300 qwen -m agentalloy-proxy -p "$TASK_B" >"$OUT/run-qwen-b.out" 2>&1
echo "    done (exit $?)"

echo; echo "--- run 3/4: codex @ ship"
set_phase ship
mark codex-A ship
timeout 300 codex exec --ephemeral -s read-only -m "$MODEL" \
  "$TASK_A" >"$OUT/run-codex-a.out" 2>&1
echo "    done (exit $?)"

echo; echo "--- run 4/4: codex @ qa  (replacement check vs ship)"
set_phase qa
mark codex-B qa
timeout 300 codex exec --ephemeral -s read-only -m "$MODEL" \
  "$TASK_B" >"$OUT/run-codex-b.out" 2>&1
echo "    done (exit $?)"

# --- 6. analysis ------------------------------------------------------------
echo
echo "================ RESULT ================"
python3 - "$LOG" <<'PY'
import json, re, sys

rows = [json.loads(l) for l in open(sys.argv[1])]
segs, cur = [], None
for r in rows:
    if "marker" in r:
        cur = {"name": r["marker"], "phase": r["phase"], "rows": []}
        segs.append(cur)
    elif cur is not None:
        cur["rows"].append(r)

def system_leg(r):
    """The text the leg-3 block lands on, whichever surface this is."""
    if r["path"].endswith("/responses"):
        return r.get("instructions") or ""
    return "\n".join(m for m in (r.get("system_messages") or []) if isinstance(m, str))

def user_leg(r):
    lu = r.get("last_user")
    if isinstance(lu, list):
        return " ".join(b.get("text", "") for b in lu if isinstance(b, dict))
    if isinstance(lu, str):
        return lu
    return json.dumps(r.get("last_input_item") or "")

failures = []
for pair, surface in ((("qwen-A", "qwen-B"), "chat/completions"),
                      (("codex-A", "codex-B"), "responses")):
    print(f"\n######## {surface} ########")
    seen = {s["name"]: s for s in segs}
    for name in pair:
        s = seen.get(name)
        if s is None or not s["rows"]:
            print(f"  {name}: NO UPSTREAM REQUESTS RECORDED")
            failures.append(f"{name}: no traffic")
            continue
        want, carried, lens = s["phase"], 0, set()
        print(f"\n  -- {name} (phase={want}, {len(s['rows'])} turns)")
        for r in s["rows"]:
            leg = system_leg(r)
            phases = re.findall(r"BEGIN AGENTALLOY-CONTEXT phase=(\w+)", leg)
            carried += bool(phases)
            if phases:
                lens.add(len(leg))
            print(f"     turn {r['turn']:>2}  model={str(r.get('model'))[:22]:<22} "
                  f"tools={str(r.get('n_tools')):<3} leg3={len(phases)} {phases} "
                  f"leglen={len(leg):<6} banner={'AGENTALLOY-BANNER' in user_leg(r)}")

        n = len(s["rows"])
        if carried != n:
            failures.append(f"{name}: prose on only {carried}/{n} turns -- deliver-once regression")
            print(f"     FAIL every-turn: {carried}/{n}")
        else:
            print(f"     PASS every-turn: {n}/{n}")

        multi = [r["turn"] for r in s["rows"]
                 if len(re.findall(r"BEGIN AGENTALLOY-CONTEXT phase=", system_leg(r))) > 1]
        if multi:
            failures.append(f"{name}: accumulated >1 block on turns {multi}")
            print(f"     FAIL single-block: turns {multi}")
        else:
            print("     PASS single-block")

        wrong = sorted({p for r in s["rows"]
                        for p in re.findall(r"BEGIN AGENTALLOY-CONTEXT phase=(\w+)", system_leg(r))
                        if p != want})
        if wrong:
            failures.append(f"{name}: carried foreign phase(s) {wrong}, expected {want}")
            print(f"     FAIL phase-replacement: saw {wrong}, expected only '{want}'")
        else:
            print(f"     PASS phase-replacement: only '{want}'")

        if len(lens) > 1:
            failures.append(f"{name}: leg length varied across turns {sorted(lens)}")
            print(f"     FAIL byte-identical: lengths {sorted(lens)}")
        elif lens:
            print(f"     PASS byte-identical: leglen {lens.pop()} on every turn")

print("\n========================================")
if failures:
    print("OVERALL: FAIL")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("OVERALL: PASS -- leg 3 delivers every turn, once, with the current phase, "
      "byte-identical, on BOTH surfaces")
PY
