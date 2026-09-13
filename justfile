# AgentAlloy v11 — dev tasks. Run `just --list` for all tasks.
#
# Quality gates mirror scripts/local-ci.sh (which CI runs); `ci` is the
# pre-push gate. The Rust core crate (rust/agentalloy-core) is part of
# the gates: it must compile and its unit tests must pass.

default: ci

# --- Quality gates ---

ci: lint typecheck test

lint:
  uv run ruff check .
  uv run ruff format --check .
  cargo clippy --release --locked --all-targets -- -D warnings

typecheck:
  uv run pyright

test:
  cargo test --locked
  uv run pytest

# --- Build ---

# Release-build the crate and install the package editable so the built
# extension lands in the venv (maturin builds agentalloy._core).
build:
  cargo build --release --locked
  uv pip install -e .

clean:
  cargo clean

# --- Formatting ---

fmt:
  uv run ruff format
  cargo fmt --all
