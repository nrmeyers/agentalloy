#!/usr/bin/env bash
set -euo pipefail

# AgentAlloy v2.0 — Install
#
#   ./install.sh          # install CLI globally via uv tool
#   ./install.sh --dev    # also install dev deps (ruff, mypy, pytest)
#
# After install, the `agentalloy` command works from any directory.
# For local dev with model servers, use: ./scripts/v2-bringup.sh up

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

DEV_MODE=false
[[ "${1:-}" == "--dev" ]] && DEV_MODE=true

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""
echo "  AgentAlloy v2.0 — Install"
echo "  =========================="
echo ""

# ── Prerequisites ────────────────────────────────────────────────────

echo "Checking prerequisites..."

command -v uv &>/dev/null || error "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
info "uv: $(uv --version)"

command -v cargo &>/dev/null || {
  [[ -f "$HOME/.cargo/bin/cargo" ]] && export PATH="$HOME/.cargo/bin:$PATH"
  command -v cargo &>/dev/null || error "Rust not found. Install: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
}
info "Rust: $(cargo --version | awk '{print $2}')"

echo ""

# ── Build Rust extension ─────────────────────────────────────────────

echo "Building Rust DataLayer extension..."
cd "$PROJECT_DIR/rust"
cargo build --release -p newagent-core 2>&1 | tail -1

# Detect Python version for .so naming
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')
ARCH=$(uname -m)
if [[ "$(uname)" == "Darwin" ]]; then
  DEST="$PROJECT_DIR/src/newagent/_core.cpython-${PYVER}-darwin.so"
  SRC="target/release/lib_core.dylib"
else
  DEST="$PROJECT_DIR/src/newagent/_core.cpython-${PYVER}-${ARCH}-linux-gnu.so"
  SRC="target/release/lib_core.so"
fi

[[ -f "$SRC" ]] && cp "$SRC" "$DEST" && info "Rust extension built" || warn "Rust lib not found at $SRC"
cd "$PROJECT_DIR"
echo ""

# ── Install CLI globally ─────────────────────────────────────────────

echo "Installing agentalloy CLI..."
if $DEV_MODE; then
  uv tool install "$PROJECT_DIR" --dev
else
  uv tool install "$PROJECT_DIR"
fi
info "CLI installed: $(which agentalloy 2>/dev/null || echo 'agentalloy')"

echo ""

# ── Verify ───────────────────────────────────────────────────────────

echo "Verifying..."
agentalloy status 2>/dev/null && info "CLI works" || warn "CLI check failed"

echo ""
echo "  ┌──────────────────────────────────────────────────────┐"
echo "  │  Install complete!                                   │"
echo "  │                                                      │"
echo "  │  Quick start (full stack with model servers):        │"
echo "  │    ./scripts/v2-bringup.sh up                        │"
echo "  │    ./scripts/v2-bringup.sh status                    │"
echo "  │    ./scripts/v2-bringup.sh smoke                     │"
echo "  │                                                      │"
echo "  │  Manual start:                                       │"
echo "  │    agentalloy serve                                  │"
echo "  │    open http://localhost:48950/dashboard             │"
echo "  │                                                      │"
echo "  │  Harness setup:                                      │"
echo "  │    agentalloy harness setup --type qwen-code         │"
echo "  │                                                      │"
echo "  │  Stop everything:                                    │"
echo "  │    ./scripts/v2-bringup.sh down                      │"
echo "  └──────────────────────────────────────────────────────┘"
echo ""
