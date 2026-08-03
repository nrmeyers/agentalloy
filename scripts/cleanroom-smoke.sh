#!/usr/bin/env bash
# Clean-room CLI output-shape smoke for AgentAlloy.
#
# Runs each verb in a throwaway HOME and enforces the stdout contract:
#   * user-facing lifecycle verbs (wire/unwire/update/reset) print concise
#     HUMAN text by default — never a raw JSON dump — and valid JSON under --json;
#   * machine verbs (detect) emit valid JSON by contract.
#
# No service, no corpus, no models. It never touches real host state — HOME and
# the XDG dirs are redirected to a temp tree that's removed on exit. Safe to run
# anywhere: inside the clean-room container, in CI, or on your laptop.
set -uo pipefail

WORK="$(mktemp -d)"
export HOME="$WORK/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
REPO="$WORK/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email smoke@example.com
git -C "$REPO" config user.name smoke
trap 'rm -rf "$WORK"' EXIT

fail=0
_is_json() { python3 -c 'import sys,json; json.load(sys.stdin)' >/dev/null 2>&1; }

# assert_human <label> -- <command...>
assert_human() {
  local label="$1"
  shift 2 # drop label + the literal "--"
  local out
  out="$("$@" 2>/dev/null)"
  if [ -z "$out" ]; then
    echo "FAIL [$label]: produced no stdout (expected human summary)"
    fail=1
  elif printf '%s' "$out" | _is_json; then
    echo "FAIL [$label]: default stdout is raw JSON (should be human text)"
    fail=1
  else
    echo "ok   [$label]: human by default"
  fi
}

# assert_json <label> -- <command...>
assert_json() {
  local label="$1"
  shift 2
  if "$@" 2>/dev/null | _is_json; then
    echo "ok   [$label]: valid JSON"
  else
    echo "FAIL [$label]: expected valid JSON on stdout"
    fail=1
  fi
}

cd "$REPO"

echo "== user-facing lifecycle verbs (human by default, --json opt-in) =="
agentalloy wire --harness claude-code >/dev/null 2>&1 || true
assert_human "unwire"       -- agentalloy unwire
agentalloy wire --harness claude-code >/dev/null 2>&1 || true
assert_json  "unwire --json" -- agentalloy unwire --json

assert_human "update"       -- agentalloy update
assert_json  "update --json" -- agentalloy update --json

assert_human "reset"        -- agentalloy reset --yes
assert_json  "reset --json"  -- agentalloy reset --yes --json

echo "== machine verbs (JSON by contract) =="
assert_json  "detect"       -- agentalloy detect

echo "== wire + unwire every advertised harness (isolated repo each) =="
# Enumerate from the CLI itself so this stays in sync with the registry.
harnesses="$(agentalloy wire --help 2>&1 | grep -oE '\{[a-z0-9,-]+\}' | head -1 | tr -d '{}' | tr ',' ' ')"
if [ -z "$harnesses" ]; then
  echo "FAIL: could not enumerate harnesses from 'wire --help'"
  fail=1
fi
for h in $harnesses; do
  hrepo="$WORK/wire-$h"
  mkdir -p "$hrepo"
  git -C "$hrepo" init -q
  # stderr goes to a file rather than /dev/null: it is the ONLY diagnostic this
  # loop produces, and discarding it makes an intermittent failure here
  # undebuggable from the CI log alone (aider flaked once on main at 0120870
  # and passed on a bare re-run, with no evidence left behind). stdout is still
  # captured separately — the raw-JSON assertion below applies to stdout only.
  werr="$WORK/wire-$h.err"
  out="$(cd "$hrepo" && agentalloy wire --harness "$h" 2>"$werr")"
  wrc=$?
  uerr="$WORK/unwire-$h.err"
  urc=0
  (cd "$hrepo" && agentalloy unwire >/dev/null 2>"$uerr") || urc=$?
  if [ "$wrc" -ne 0 ]; then
    echo "FAIL [$h]: wire exit $wrc"
    sed 's/^/       wire stderr| /' "$werr"
    fail=1
  elif printf '%s' "$out" | _is_json; then
    echo "FAIL [$h]: wire dumped raw JSON"
    fail=1
  elif [ "$urc" -ne 0 ]; then
    echo "FAIL [$h]: unwire exit $urc"
    sed 's/^/       unwire stderr| /' "$uerr"
    fail=1
  else
    echo "ok   [$h]: wire + unwire"
  fi
done

echo "== auto-detection from repo markers (wire with no --harness) =="
# marker:expected — mirrors wire.py _HARNESS_MARKERS (representative subset).
for pair in "CLAUDE.md:claude-code" "GEMINI.md:antigravity" ".cursorrules:cursor" \
            ".clinerules:cline" ".aider.conf.yml:aider"; do
  marker="${pair%%:*}"
  expect="${pair##*:}"
  drepo="$WORK/detect-$expect"
  mkdir -p "$drepo"
  git -C "$drepo" init -q
  : >"$drepo/$marker"
  got="$(cd "$drepo" && agentalloy wire --json 2>/dev/null \
         | python3 -c 'import sys,json; print(json.load(sys.stdin).get("harness",""))' 2>/dev/null)"
  (cd "$drepo" && agentalloy unwire >/dev/null 2>&1) || true
  if [ "$got" = "$expect" ]; then
    echo "ok   [detect $marker -> $got]"
  else
    echo "FAIL [detect $marker]: expected $expect, got '$got'"
    fail=1
  fi
done

# Phase seeding is no longer observable from this smoke.  `.agentalloy/phase` is
# gone — the state store owns phase — and reading the store needs a running
# service, which this clean-room deliberately does not have.  The seeding
# behaviour both branches used to assert here is covered by unit tests
# (`test_off_seeds_no_phase_in_a_fresh_repo` and the phase-access suite); what
# remains below is the part the clean room can actually see: the files `wire`
# writes.
echo "== per-repo lifecycle mode (off defers) =="
orepo="$WORK/lifecycle-off"
mkdir -p "$orepo"
git -C "$orepo" init -q
omode="$(cd "$orepo" && agentalloy wire --harness claude-code --lifecycle-mode off --json 2>/dev/null \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("lifecycle_mode",""))' 2>/dev/null)"
if [ "$omode" = "off" ] \
   && grep -q "lifecycle_mode: off" "$orepo/.agentalloy/config" 2>/dev/null; then
  echo "ok   [lifecycle off: config=off]"
else
  echo "FAIL [lifecycle off]: mode='$omode', config state unexpected"
  fail=1
fi
(cd "$orepo" && agentalloy unwire >/dev/null 2>&1) || true

echo "== full mode: soft-precedence note + clean-room excludes =="
frepo="$WORK/lifecycle-full"
mkdir -p "$frepo"
git -C "$frepo" init -q
: >"$frepo/CLAUDE.md"
(cd "$frepo" && agentalloy wire --harness claude-code --lifecycle-mode full --clean-room >/dev/null 2>&1) || true
if grep -q "lifecycle_mode: full" "$frepo/.agentalloy/config" 2>/dev/null \
   && grep -q "BEGIN agentalloy install" "$frepo/.claude/CLAUDE.md" 2>/dev/null \
   && grep -q "claudeMdExcludes" "$frepo/.claude/settings.json" 2>/dev/null; then
  echo "ok   [full: soft note + clean-room excludes written]"
else
  echo "FAIL [full]: missing soft note / clean-room excludes"
  fail=1
fi
(cd "$frepo" && agentalloy unwire >/dev/null 2>&1) || true
if [ -e "$frepo/.claude/CLAUDE.md" ]; then
  echo "FAIL [full unwire]: .claude/CLAUDE.md soft note left behind"
  fail=1
else
  echo "ok   [full unwire: soft note removed]"
fi

echo "== telemetry savings verb (human by default, --json opt-in) =="
assert_human "telemetry savings"        -- agentalloy telemetry savings
assert_json  "telemetry savings --json" -- agentalloy telemetry savings --json

echo
if [ "$fail" -ne 0 ]; then
  echo "cleanroom smoke: FAILED"
  exit 1
fi
echo "cleanroom smoke: PASSED"
