# pyright: reportPrivateUsage=false
"""``reset-step`` subcommand.

Clears a step's entry from ``install-state.json`` so the next install
run will re-execute it.  Also clears dependent steps.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from agentalloy.install import state as install_state
from agentalloy.install.output import add_json_flag, print_rich, write_result

SCHEMA_VERSION = 1

# Dependency map from contracts.md — a step depends on the listed steps.
STEP_DEPENDENCIES: dict[str, list[str]] = {
    "detect": [],
    "recommend-host-targets": ["detect"],
    "recommend-models": ["recommend-host-targets"],
    "seed-corpus": [],
    "pull-models": ["recommend-models"],
    "write-env": ["recommend-models"],
    "wire-harness": ["write-env"],
    "verify": ["wire-harness", "pull-models", "seed-corpus"],
}

VALID_STEPS = frozenset(STEP_DEPENDENCIES.keys())

# Top-level install-state.json keys owned by each step. When a step is
# cleared, its owned keys are also removed so the next run starts from a
# clean slate (otherwise tamper-detection or idempotency paths read stale
# data and either falsely error or skip work).
_STEP_OWNED_KEYS: dict[str, list[str]] = {
    "detect": [],
    "recommend-host-targets": [],
    "recommend-models": [],
    "seed-corpus": [],
    "pull-models": ["models_pulled"],
    "write-env": ["env_path", "port"],
    "wire-harness": ["harness", "harness_files_written"],
    "verify": ["last_verify_passed_at"],
}


def _dependents_of(step: str) -> set[str]:
    """Find all steps that transitively depend on the given step."""
    dependents: set[str] = set()
    for name, deps in STEP_DEPENDENCIES.items():
        if step in deps:
            dependents.add(name)
            dependents |= _dependents_of(name)
    return dependents


def reset_step(
    step: str,
    root: None | Any = None,
) -> dict[str, Any]:
    """Clear a step (and its dependents) from install state."""

    from agentalloy.install.state import _repo_root

    root = root or _repo_root()

    if step not in VALID_STEPS:
        print(f"ERROR: Unknown step: '{step}'", file=sys.stderr)
        print(f"FIX:   Use one of: {', '.join(sorted(VALID_STEPS))}", file=sys.stderr)
        raise SystemExit(1)

    st = install_state.load_state(root)

    if not install_state.is_step_completed(st, step):
        print(f"Step '{step}' is not in completed_steps — nothing to clear.", file=sys.stderr)
        raise SystemExit(4)

    # Compute which steps to clear
    to_clear = {step} | _dependents_of(step)
    cleared: list[str] = []

    original = st.get("completed_steps", [])
    st["completed_steps"] = [s for s in original if s["step"] not in to_clear]
    cleared = [s["step"] for s in original if s["step"] in to_clear]

    # Clear top-level fields owned by each cleared step
    keys_cleared: list[str] = []
    for s in to_clear:
        for k in _STEP_OWNED_KEYS.get(s, []):
            if k in st:
                del st[k]
                keys_cleared.append(k)

    install_state.save_state(st, root)

    return {
        "schema_version": SCHEMA_VERSION,
        "step_cleared": step,
        "dependent_steps_also_cleared": sorted(set(cleared) - {step}),
        "state_keys_cleared": sorted(set(keys_cleared)),
    }


# ---------------------------------------------------------------------------
# Subcommand interface
# ---------------------------------------------------------------------------


def add_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p: argparse.ArgumentParser = subparsers.add_parser(
        "reset-step",
        help="Clear a step from install state so it re-runs on next install.",
    )
    p.add_argument(
        "step",
        choices=sorted(VALID_STEPS),
        help="The step to clear.",
    )
    add_json_flag(p)
    p.set_defaults(func=_run)


def _render_human(result: dict[str, Any]) -> None:
    """Render reset step result in human-readable format."""
    step = result.get("step_cleared", "unknown")
    dependents = result.get("dependent_steps_also_cleared", [])
    keys = result.get("state_keys_cleared", [])

    print_rich("\n  [bold]Reset Step[/bold]\n")
    print_rich(f"  Cleared: [bold]{step}[/bold]")

    if dependents:
        print_rich(f"  Also cleared: {', '.join(dependents)}")

    if keys:
        print_rich(f"  State keys cleared: {', '.join(keys)}")

    print_rich()


def _run(args: argparse.Namespace) -> int:
    result = reset_step(args.step)
    write_result(result, args, human_fn=_render_human)
    return 0
