#!/usr/bin/env bash
# Reproduce CI quality gates locally before pushing.
# Usage: scripts/local-ci.sh
# Exit codes: 0 = all green, 1 = check(s) failed
set -euo pipefail

# Ensure code-index extra is available (CI quality job installs it too).
uv sync --frozen --no-dev 2>/dev/null || true

PASS=0
FAIL=0
TOTAL=0

check() {
  local label="$1"; shift
  TOTAL=$((TOTAL + 1))
  echo ">>> $label"
  if "$@"; then
    echo "  ✓ $label"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $label"
    FAIL=$((FAIL + 1))
  fi
}

# Lint
check "ruff check" uv run ruff check .
# Format check
check "ruff format --check" uv run ruff format --check .
# Type check
check "pyright" uv run pyright
# Unit tests (exclude integration + container)
check "pytest (fast)" uv run pytest -m "not integration and not container"

echo ""
echo "Results: $PASS/$TOTAL passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
  echo "Local CI failed — fix the above before pushing."
  exit 1
fi

echo "All checks passed."
exit 0
