"""Task 10 `gate-store-context` — the CLI gate path must see the contract store.

`_forward_gate_blocks` used to build ``PredicateContext(project_root, current_phase)``
with no ``store``. ``_query_store_contracts`` returns ``[]`` when ``ctx.store is None``,
so ``build_contracts_cover_tasks`` and ``build_contract_tag_focus`` both evaluated
UNKNOWN and failed open. The design→build contract-coverage gate has therefore been
vacuous for every CLI ``phase set`` since contracts moved into the store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentalloy.install.subcommands import phase as phase_mod
from agentalloy.signals.predicates import (
    PredicateContext,
    PredicateResult,
    _query_store_contracts,  # pyright: ignore[reportPrivateUsage]
    eval_build_contract_tag_focus,
    eval_build_contracts_cover_tasks,
    eval_contract_exists,
)
from agentalloy.storage.state_store import DuckDBStateStore

SLUG = "widget"


@pytest.fixture()
def design_repo(tmp_path: Path) -> Path:
    """A repo whose design artifacts satisfy every non-contract design exit gate."""
    d = tmp_path / "docs" / "design" / SLUG
    d.mkdir(parents=True)
    (d / "approach.md").write_text("# x\n\n## Approach\n\nprose\n")
    (d / "test-plan.md").write_text("# x\n\n## Test Cases\n\n- a test\n")
    (d / "tasks.md").write_text("# x\n\n## Tasks\n\n- 01 alpha\n- 02 beta\n- 03 gamma\n")

    spec = tmp_path / "docs" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{SLUG}.md").write_text("# spec\n")

    appr = tmp_path / ".agentalloy" / "approved"
    appr.mkdir(parents=True)
    (appr / "design").write_text("approved by test\n")
    return tmp_path


def _store_with(tmp_path: Path, n_build: int, build_tags: str = '["state"]') -> DuckDBStateStore:
    """Store carrying one design contract for SLUG and ``n_build`` build contracts."""
    store = DuckDBStateStore(tmp_path / "gate_state.db")
    store.open()
    store.migrate()
    repo = store._repo()  # type: ignore[attr-defined]  # noqa: SLF001

    rows = [
        f"('{repo}', 'design/{SLUG}', '{SLUG}', '[\"state\"]', NULL, "
        f"'design', 'active', CURRENT_TIMESTAMP)"
    ]
    for i in range(n_build):
        rows.append(
            f"('{repo}', 'build/t{i}', 't{i}', '{build_tags}', NULL, "
            f"'build', 'active', CURRENT_TIMESTAMP)"
        )
    store.execute(
        "INSERT INTO sdd_contract "
        "(repo, contract_id, slug, domain_tags, work_item, phase, status, updated_at) "
        "VALUES " + ", ".join(rows) + ";"
    )
    return store


class TestQueryStoreContractsErrorPosture:
    """store-absent and store-errored must not look alike to the caller."""

    def test_absent_store_returns_empty(self, tmp_path: Path) -> None:
        ctx = PredicateContext(project_root=tmp_path, current_phase="design")
        assert _query_store_contracts(ctx, phase="build") == []

    def test_errored_store_yields_unknown_not_not_met(self, tmp_path: Path) -> None:
        """An erroring store must fail OPEN.

        Without this, a service blip collapses to ``[]`` and trips the
        "store exists but returned nothing → NOT_MET" branch, turning an infra
        failure into a hard gate refusal.
        """

        class Boom:
            def list_contracts(self, **_: Any) -> list[dict[str, Any]]:
                raise RuntimeError("service exploded")

        ctx = PredicateContext(project_root=tmp_path, current_phase="design", store=Boom())
        # Aimed at eval_contract_exists specifically: it is the ONLY caller whose
        # empty-result branch returns NOT_MET, so it is the one that fails closed
        # if the errored store collapses to []. The slug-resolving predicates
        # short-circuit to UNKNOWN before that branch and would pass vacuously.
        result = eval_contract_exists({"phase": "build"}, ctx)
        assert result == PredicateResult.UNKNOWN

    def test_errored_store_unknown_in_cover_tasks(self, tmp_path: Path) -> None:
        class Boom:
            def list_contracts(self, **_: Any) -> list[dict[str, Any]]:
                raise RuntimeError("service exploded")

        ctx = PredicateContext(project_root=tmp_path, current_phase="design", store=Boom())
        result = eval_build_contracts_cover_tasks(
            {"tasks": "docs/design/{slug}/tasks.md", "phase": "design", "slug": SLUG}, ctx
        )
        assert result == PredicateResult.UNKNOWN


class TestForwardGateSeesContracts:
    """The load-bearing pair: coverage must actually decide the design→build gate."""

    def test_full_coverage_passes(self, design_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _store_with(design_repo, n_build=3)
        blocked, _ = phase_mod._forward_gate_blocks(  # noqa: SLF001
            "design", "build", design_repo, store
        )
        assert blocked is False

    def test_short_coverage_blocks(
        self, design_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 task items, 2 build contracts → refused.

        This is the assertion that fails on the pre-fix tree: with no store the
        predicate returned UNKNOWN and the gate allowed the advance.
        """
        store = _store_with(design_repo, n_build=2)
        blocked, advisories = phase_mod._forward_gate_blocks(  # noqa: SLF001
            "design", "build", design_repo, store
        )
        assert blocked is True
        assert advisories

    def test_wide_tags_block(self, design_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Coverage is satisfied (3/3) but a contract carries 3 domain_tags → refused.

        The other half of the same fix: `build_contract_tag_focus` resolves its
        work-item slug through the same path as cover_tasks, so it was equally
        vacuous without a store. Coverage is held constant here so the block can
        only come from tag focus.
        """
        store = _store_with(design_repo, n_build=3, build_tags='["state","api","signals"]')
        # Direct predicate assertion: the gate-level block alone can't distinguish
        # NOT_MET-from-tag-focus from NOT_MET-for-any-other-reason.
        ctx = PredicateContext(project_root=design_repo, current_phase="design", store=store)
        assert (
            eval_build_contract_tag_focus({"phase": "design", "slug": SLUG}, ctx)
            == PredicateResult.NOT_MET
        )
        blocked, _ = phase_mod._forward_gate_blocks(  # noqa: SLF001
            "design", "build", design_repo, store
        )
        assert blocked is True

    def test_no_store_handle_fails_open(self, design_repo: Path) -> None:
        """A ``None`` handle still fails open at the predicate level.

        This is now an internal invariant rather than a reachable state: the
        caller (`run_phase_set`) obtains the handle from `phase_access`, which
        has already exited non-zero if no store is reachable.  It is asserted
        because the predicates -- not the gate -- own the fail-open rule, and a
        future caller that legitimately has no handle must not start blocking.
        """
        blocked, _ = phase_mod._forward_gate_blocks(  # noqa: SLF001
            "design", "build", design_repo, None
        )
        assert blocked is False
