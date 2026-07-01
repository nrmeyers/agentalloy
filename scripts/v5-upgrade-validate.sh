#!/usr/bin/env bash
# Validate a real v4 -> v5 in-place host (native) upgrade.
#
# The consequential risk is NOT the package-swap mechanics (storage-agnostic,
# already covered) — it's that v4 stored vectors in DuckDB and v5 stores them in
# fragments.lance, at the SAME embedding dim (768). The native upgrade re-embeds
# only on a dim CHANGE, so without the empty-index guard it leaves fragments.lance
# EMPTY: skills present in agentalloy.duck, zero vectors, retrieval silently dead.
#
# This harness builds a genuine v4.0.4 corpus in an isolated instance, then runs
# the REAL (fixed) `upgrade.py:_upgrade_native` under v5 — with only the package
# swap and service control neutralized, so install-packs / update / reembed run
# for real — and asserts the load-bearing invariant: fragments.lance is populated.
#
# Deviations from a literal `agentalloy upgrade` (both storage-irrelevant):
#   * each version installs into its own venv (a uv-tool swap would clobber the
#     user's real `agentalloy` shim);
#   * _swap_command is stubbed to `true` and service start/stop are no-ops.
# The reembed DECISION logic under test — the actual fix — runs unmodified.
#
# Prereqs: an embedding server reachable at $EMBED_URL (nomic, 768-dim, e.g. the
# one scripts/v5-bringup.sh starts on :47953).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTANCE="${INSTANCE:-$HOME/.local/share/agentalloy-v4to5-upgrade}"
EMBED_URL="${EMBED_URL:-http://localhost:47953}"
V4_TAG="${V4_TAG:-v4.0.4}"
GIT_URL="${GIT_URL:-https://github.com/nrmeyers/agentalloy.git}"
PORT="${PORT:-47962}"

export XDG_CONFIG_HOME="$INSTANCE/config"
export XDG_DATA_HOME="$INSTANCE/data"
export RUNTIME_EMBED_BASE_URL="$EMBED_URL"
export RUNTIME_EMBEDDING_BASE_URL="$EMBED_URL"   # v4 name, if it differs
CORPUS="$XDG_DATA_HOME/agentalloy/corpus"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*"; exit 1; }

MODE="${1:-run}"
case "$MODE" in
  clean) rm -rf "$INSTANCE"; echo "removed $INSTANCE"; exit 0 ;;
esac

if [ "$MODE" = "resume" ]; then
  [ -d "$INSTANCE/corpus-v4-snapshot" ] || fail "no snapshot to resume from — run without 'resume' first"
  say "0. RESUME — restore v4 corpus + install-state from snapshot"
  rm -rf "$CORPUS"; mkdir -p "$(dirname "$CORPUS")"
  cp -a "$INSTANCE/corpus-v4-snapshot" "$CORPUS"
  cp -f "$INSTANCE/install-state-v4-snapshot.json" "$XDG_CONFIG_HOME/agentalloy/install-state.json"
  echo "   restored $(ls "$CORPUS" | tr '\n' ' ')"
else
  say "0. reset isolated instance: $INSTANCE"
  rm -rf "$INSTANCE"; mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME"
fi

if [ "$MODE" != "resume" ]; then
say "1. install v4 ($V4_TAG) into .venv-v4"
uv venv "$INSTANCE/.venv-v4" >/dev/null
V4PY="$INSTANCE/.venv-v4/bin/python"
V4BIN="$INSTANCE/.venv-v4/bin/agentalloy"
uv pip install --python "$V4PY" "git+$GIT_URL@$V4_TAG" >/dev/null
echo "v4: $("$V4BIN" --version)"

say "2. build a genuine v4 corpus (ladybug + skills.duck + duckdb vectors)"
"$V4BIN" install-packs --packs all --no-restart
"$V4BIN" reembed --force --no-restart
# Preserve the v4-written install-state (installed_packs is a list of DICTS, same
# shape v5 reads); only stamp deployment=native so the upgrade branches correctly.
"$V4PY" - "$XDG_CONFIG_HOME/agentalloy/install-state.json" "$PORT" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
st = json.loads(p.read_text()) if p.exists() else {}
st["deployment"] = "native"; st.setdefault("port", int(sys.argv[2]))
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(st))
ip = st.get("installed_packs") or []
print(f"   install-state installed_packs: {len(ip)} entries, first={ip[:1]}")
PY
say "   v4 on-disk layout:"; ls -la "$CORPUS"

say "3. snapshot the v4 corpus + install-state (for 'resume')"
rm -rf "$INSTANCE/corpus-v4-snapshot"
cp -a "$CORPUS" "$INSTANCE/corpus-v4-snapshot"
cp -f "$XDG_CONFIG_HOME/agentalloy/install-state.json" "$INSTANCE/install-state-v4-snapshot.json"
fi

say "4. install v5 (this branch) into .venv-v5"
V5PY="$INSTANCE/.venv-v5/bin/python"
V5BIN="$INSTANCE/.venv-v5/bin/agentalloy"
[ -x "$V5PY" ] || uv venv "$INSTANCE/.venv-v5" >/dev/null   # reuse on resume (editable install is live)
uv pip install --python "$V5PY" -e "$REPO_ROOT" >/dev/null
echo "v5: $("$V5BIN" --version)"

# Everything below runs the v5 binary; put its venv first on PATH so the
# subprocesses _run_cli shells ("agentalloy ...") resolve to v5.
export PATH="$INSTANCE/.venv-v5/bin:$PATH"

say "5. run the REAL v5 _upgrade_native (swap + service control stubbed only)"
"$V5PY" - "$V4_TAG" <<'PY'
import sys
from unittest.mock import patch
from agentalloy.install.subcommands import upgrade as up

# Load the REAL v4-written state from disk (as `agentalloy upgrade` does), then
# force the native branch. installed_packs stays the v4 dict list.
state = up.install_state.load_state()
state["deployment"] = "native"
with (
    patch.object(up, "_detect_install_method", return_value="uv-tool"),
    patch.object(up, "_swap_command", return_value=["true"]),  # neutralize pkg swap
    patch.object(up, "_stop_service", return_value="manual"),
    patch.object(up, "_start_inference_servers"),
    patch.object(up, "_start_service"),
):
    actions, warnings = up._upgrade_native("v5.0.0", state, assume_yes=True)

print("\n  actions:")
for a in actions:
    print(f"    - {a}")
if warnings:
    print("  warnings:")
    for w in warnings:
        print(f"    ! {w}")
# The fix must have fired: engine-migration reembed, no skip.
assert any("engine migration" in a or "re-embedded" in a for a in actions), \
    "upgrade did NOT rebuild the vector index"
assert not any("re-embed skipped" in w for w in warnings), "reembed was skipped"
PY

say "6. ASSERT — the load-bearing invariant"
read -r SKILLS FRAGS VECS < <("$V5PY" - <<'PY'
from agentalloy.config import get_settings
from agentalloy.install.subcommands import seed_corpus
from agentalloy.storage.open import open_skills
s = get_settings()
sk = open_skills(s, read_only=True)
try:
    skills = int(sk.scalar("SELECT count(*) FROM skills WHERE deprecated = false") or 0)
    frags = int(sk.scalar("SELECT count(*) FROM fragments") or 0)
finally:
    sk.close()
print(skills, frags, seed_corpus.corpus_embedding_count())
PY
)
echo "   skills in agentalloy.duck : $SKILLS"
echo "   fragments in agentalloy.duck: $FRAGS"
echo "   vectors in fragments.lance : $VECS"
[ "$VECS" -gt 0 ] 2>/dev/null || fail "fragments.lance is EMPTY after upgrade — the fix did not take."
[ "$VECS" -ge "$FRAGS" ] 2>/dev/null || echo "   (note: vectors < fragments — includes synthetic cards; inspect if far off)"
say "PASS — upgrade produced a populated Lance dataset ($VECS vectors)."

say "6b. ASSERT — legacy v4 engine files reclaimed"
LEGACY=""
for f in skills.duck skills.duck.wal ladybug ladybug.wal; do
  [ -e "$CORPUS/$f" ] && LEGACY="$LEGACY $f"
done
if [ -n "$LEGACY" ]; then
  fail "v4 files still present after upgrade:$LEGACY"
fi
echo "   v4 files removed; corpus now:"; ls "$CORPUS" | sed 's/^/     /'
say "PASS — legacy v4 files (ladybug, skills.duck) removed."

say "7. serve + health/retrieve smoke (user-visible impact)"
"$V5BIN" serve --port "$PORT" --host 127.0.0.1 &
SVPID=$!; trap 'kill $SVPID 2>/dev/null || true' EXIT
"$V5PY" - "$PORT" <<'PY'
import sys, time, json, urllib.request
port = sys.argv[1]
for _ in range(30):
    try:
        h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2))
        break
    except Exception:
        time.sleep(1)
else:
    print("service never became healthy"); sys.exit(1)
print("  health:", h.get("status"), {k: v for k, v in (h.get("dependencies") or {}).items()})
body = json.dumps(
    {"task": "how do I handle database migrations", "phase": "build", "k": 4}
).encode()
req = urllib.request.Request(f"http://127.0.0.1:{port}/retrieve", body,
                            {"Content-Type": "application/json"})
r = json.load(urllib.request.urlopen(req, timeout=10))
hits = r.get("results") or r.get("hits") or []
print("  retrieve hits:", len(hits))
sys.exit(0 if hits else 2)
PY
say "ALL CHECKS PASSED — v4->v5 in-place native upgrade produces a working v5 corpus."
