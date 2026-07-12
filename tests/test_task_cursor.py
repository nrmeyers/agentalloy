"""The ``task`` subcommand and the proxy's current-contract resolution.

``agentalloy task next`` walks the per-task build contracts in filename order,
writing ``.agentalloy/cursor``; the proxy resolves that cursor (or the phase's
incoming contract by default) for Tier 2 domain composition.
"""

from __future__ import annotations

from pathlib import Path

from agentalloy.api.proxy_signal import (
    _resolve_current_contract,  # type: ignore[reportPrivateUsage]
)
from agentalloy.install.subcommands.task import (
    run_task_next,
    run_task_start,
    run_task_status,
)
from agentalloy.signals.skill_loader import _read_cursor  # type: ignore[reportPrivateUsage]

# NOTE: the shared-cursor path assumed here is kept hermetic by the autouse
# `_no_ambient_session_id` fixture in tests/conftest.py (clears CLAUDE_CODE_SESSION_ID).
# Session-scoping is covered in tests/test_cursor_session_scope.py.


def _seed(root: Path, phase: str, names: list[str]) -> None:
    (root / ".agentalloy").mkdir(parents=True, exist_ok=True)
    (root / ".agentalloy" / "phase").write_text(f"phase: {phase}\n")
    d = root / ".agentalloy" / "contracts" / phase
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.md").write_text(
            f"---\nphase: {phase}\ntask_slug: {n}\ndomain_tags: [pytest]\n---\n# {n}\nbody\n"
        )


def test_task_next_walks_in_filename_order(tmp_path: Path) -> None:
    _seed(tmp_path, "build", ["02-api", "01-cache", "03-log"])
    # First next → first by filename (01-cache), not by seed/mtime order.
    assert run_task_next(tmp_path)["cursor"] == "build/01-cache.md"
    assert _read_cursor(tmp_path) == "build/01-cache.md"
    assert run_task_next(tmp_path)["cursor"] == "build/02-api.md"
    assert run_task_next(tmp_path)["cursor"] == "build/03-log.md"
    # Past the end → done, cursor unchanged.
    done = run_task_next(tmp_path)
    assert done.get("done") is True
    assert _read_cursor(tmp_path) == "build/03-log.md"


def test_task_start_points_cursor_by_slug(tmp_path: Path) -> None:
    _seed(tmp_path, "build", ["01-cache", "02-api"])
    assert run_task_start("02-api", tmp_path)["cursor"] == "build/02-api.md"
    assert _read_cursor(tmp_path) == "build/02-api.md"
    assert run_task_start("nope", tmp_path)["ok"] is False


def test_task_status_lists_worklist(tmp_path: Path) -> None:
    _seed(tmp_path, "build", ["01-cache", "02-api"])
    run_task_start("01-cache", tmp_path)
    status = run_task_status(tmp_path)
    assert status["worklist"] == ["build/01-cache.md", "build/02-api.md"]
    assert status["cursor"] == "build/01-cache.md"


def test_resolve_current_contract_uses_cursor(tmp_path: Path) -> None:
    _seed(tmp_path, "build", ["01-cache", "02-api"])
    run_task_start("02-api", tmp_path)
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid == "build/02-api.md"
    assert path is not None and path.name == "02-api.md"


def test_resolve_current_contract_single_item_phase(tmp_path: Path) -> None:
    # Exactly one contract (the single-item incoming work-item) → compose it.
    _seed(tmp_path, "spec", ["the-feature"])
    cid, path = _resolve_current_contract(tmp_path, "spec")
    assert cid == "spec/the-feature.md"
    assert path is not None and path.name == "the-feature.md"


def test_resolve_current_contract_fanout_is_strict_none(tmp_path: Path) -> None:
    # ≥2 contracts, no cursor → strict (None, None): the resolver never guesses.
    # In normal flow the cursor is seeded on phase entry (see the seeding tests
    # below); this asserts the fail-safe floor when a cursor is somehow absent.
    _seed(tmp_path, "build", ["01-cache", "02-api", "03-log"])
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid is None and path is None
    # ...an explicit cursor resolves the pointed-at work-item.
    run_task_start("02-api", tmp_path)
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid == "build/02-api.md" and path is not None and path.name == "02-api.md"


def test_resolve_current_contract_none_when_absent(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid is None and path is None


# ---------------------------------------------------------------------------
# B2 (Outcome B): a phase transition SEEDS the work-item cursor to the new
# phase's first work-item (filename order), so "which task is current" is
# reliably set — the single source of truth both the proxy and the codify gate
# read. A phase with no contracts clears the cursor (nothing to seed).
# ---------------------------------------------------------------------------


def _seed_qa_contract(root: Path, slug: str) -> None:
    qa = root / ".agentalloy" / "contracts" / "qa"
    qa.mkdir(parents=True, exist_ok=True)
    (qa / f"{slug}.md").write_text(
        f"---\nphase: qa\ntask_slug: {slug}\ndomain_tags: [pytest]\n---\n# {slug}\nbody\n"
    )


def test_phase_transition_seeds_cursor_proxy_path(tmp_path: Path) -> None:
    # The proxy advances the phase via _write_phase_atomic; that must SEED the
    # cursor to the new phase's first work-item, not inherit the prior slug.
    from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
        _write_phase_atomic,
    )

    _seed(tmp_path, "build", ["01-cache", "02-date-tests"])
    run_task_start("02-date-tests", tmp_path)
    assert _read_cursor(tmp_path) == "build/02-date-tests.md"
    _seed_qa_contract(tmp_path, "the-feature")

    _write_phase_atomic(tmp_path, "qa")

    assert _read_cursor(tmp_path) == "qa/the-feature.md"  # seeded, not the stale build slug
    cid, path = _resolve_current_contract(tmp_path, "qa")
    assert cid == "qa/the-feature.md"
    assert path is not None and path.name == "the-feature.md"


def test_phase_transition_seeds_first_build_task(tmp_path: Path) -> None:
    # Entering a build fan-out seeds the cursor to the FIRST task by filename order
    # (01-…), regardless of write order — not mtime, not the prior phase's slug.
    from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
        _write_phase_atomic,
    )

    _seed(tmp_path, "build", ["02-api", "01-cache", "03-log"])
    (tmp_path / ".agentalloy" / "phase").write_text("phase: design\n")  # enter build from elsewhere
    _write_phase_atomic(tmp_path, "build")
    assert _read_cursor(tmp_path) == "build/01-cache.md"


def test_phase_transition_no_contracts_clears_cursor(tmp_path: Path) -> None:
    # Transition into a phase with no contracts yet → cursor cleared (nothing to seed).
    from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
        _write_phase_atomic,
    )

    _seed(tmp_path, "build", ["01-cache", "02-api"])
    run_task_start("02-api", tmp_path)
    _write_phase_atomic(tmp_path, "qa")  # qa/ has no contracts
    assert _read_cursor(tmp_path) is None


def test_phase_idempotent_rewrite_keeps_cursor(tmp_path: Path) -> None:
    # An in-phase rewrite (prev == phase) must not disturb a deliberately-set cursor.
    from agentalloy.signals.skill_loader import (  # type: ignore[reportPrivateUsage]
        _write_phase_atomic,
    )

    _seed(tmp_path, "build", ["01-cache", "02-api"])
    run_task_start("02-api", tmp_path)
    _write_phase_atomic(tmp_path, "build")
    assert _read_cursor(tmp_path) == "build/02-api.md"


def test_phase_set_cli_clears_cursor(tmp_path: Path) -> None:
    # The CLI `phase set` path (run_phase_set) clears the cursor on a transition too.
    from agentalloy.install.subcommands.phase import run_phase_set

    _seed(tmp_path, "build", ["01-cache", "02-api"])
    run_task_start("02-api", tmp_path)
    run_phase_set("qa", tmp_path, force=True)
    assert _read_cursor(tmp_path) is None
