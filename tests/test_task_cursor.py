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


def test_resolve_current_contract_fanout_waits_for_cursor(tmp_path: Path) -> None:
    # Build fans out into many per-task contracts; with no cursor the proxy must
    # not guess which task is current — Tier 2 stays silent until `task next`.
    _seed(tmp_path, "build", ["01-cache", "02-api", "03-log"])
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid is None and path is None
    # ...but an explicit cursor selects the work-item.
    run_task_start("02-api", tmp_path)
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid == "build/02-api.md"


def test_resolve_current_contract_none_when_absent(tmp_path: Path) -> None:
    (tmp_path / ".agentalloy").mkdir()
    cid, path = _resolve_current_contract(tmp_path, "build")
    assert cid is None and path is None
