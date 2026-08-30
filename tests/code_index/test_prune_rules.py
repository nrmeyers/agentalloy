"""Unit tests for the shared prune safety rules (``code_index/api/prune_rules``).

These are the primitives behind BOTH prune paths (``/code/migrate-layout`` and
``/code/prune``); testing them directly pins the rule itself, independent of
either endpoint (spec AC-8).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agentalloy.code_index.api.prune_rules import (
    PRUNE_GRACE_SECONDS,
    absence_is_corroborated,
    leftover_slug_parent,
    prunable_store_dir,
)
from agentalloy.code_index.api.state import CodeIndexState
from agentalloy.code_index.store import IndexedRepo, code_index_paths, open_jobs

from .conftest import FakeEmbedClient


@pytest.fixture
def state(tmp_path: Path) -> Iterator[CodeIndexState]:
    from agentalloy.config import Settings

    settings = Settings(code_index_data_dir=str(tmp_path / "code-index-data"))
    st = CodeIndexState(settings=settings, embed_client=FakeEmbedClient(), jobs=open_jobs(settings))
    yield st
    st.jobs.close()


def _repo(slug: str, repo_path: Path, data_dir: Path) -> IndexedRepo:
    return IndexedRepo(
        slug=slug,
        repo_path=str(repo_path),
        data_dir=str(data_dir),
        last_indexed_at=1,
        head_sha="abc",
        watch_enabled=False,
        missing_since=None,
        created_at=1,
        updated_at=1,
    )


# ---------------------------------------------------------------------------
# absence_is_corroborated (TC-1.1 .. TC-1.3)
# ---------------------------------------------------------------------------


def test_corroborated_when_parent_exists(tmp_path: Path) -> None:
    parent = tmp_path / "worktrees"
    parent.mkdir()
    gone = parent / "demo"  # absent, parent present
    assert absence_is_corroborated(gone) is True


def test_not_corroborated_when_parent_also_gone(tmp_path: Path) -> None:
    # A down mount takes the whole ancestor chain with it.
    gone = tmp_path / "no-such-mount" / "demo"
    assert absence_is_corroborated(gone) is False


def test_root_path_is_never_corroborated() -> None:
    # parent == self: no parent directory to witness the deletion.
    assert absence_is_corroborated(Path("/")) is False


# ---------------------------------------------------------------------------
# prunable_store_dir (TC-1.4 .. TC-1.6)
# ---------------------------------------------------------------------------


def test_dead_dir_with_no_survivors_is_prunable(state: CodeIndexState, tmp_path: Path) -> None:
    store = tmp_path / "repos" / "demo"
    store.mkdir(parents=True)
    repo = _repo("demo", tmp_path / "worktrees" / "demo", store)
    assert prunable_store_dir(state, repo, []) == store


def test_legacy_parent_of_a_live_sibling_is_never_prunable(state: CodeIndexState) -> None:
    # The dead row is still in the legacy layout: its data_dir is
    # repos/{slug}/, the *parent* of every live sibling's per-checkout store.
    legacy = code_index_paths(state.settings, "demo").repo_dir
    legacy.mkdir(parents=True)
    dead = _repo("demo", Path("/worktrees/gone"), legacy)

    live_path = Path("/worktrees/live")
    live_dir = code_index_paths(state.settings, "demo", repo_path=str(live_path)).repo_dir
    live_dir.mkdir(parents=True)
    live = _repo("demo", live_path, live_dir)

    assert prunable_store_dir(state, dead, [live]) is None


def test_legacy_parent_with_only_other_slugs_as_survivors_is_prunable(
    state: CodeIndexState,
) -> None:
    # A survivor of a DIFFERENT slug does not protect the dead row's dir.
    legacy = code_index_paths(state.settings, "demo").repo_dir
    legacy.mkdir(parents=True)
    dead = _repo("demo", Path("/worktrees/gone"), legacy)

    other_path = Path("/worktrees/other")
    other_dir = code_index_paths(state.settings, "other", repo_path=str(other_path)).repo_dir
    other = _repo("other", other_path, other_dir)

    assert prunable_store_dir(state, dead, [other]) == legacy


def test_dead_dir_absent_on_disk_is_not_prunable(state: CodeIndexState, tmp_path: Path) -> None:
    repo = _repo("demo", tmp_path / "worktrees" / "gone", tmp_path / "repos" / "demo")
    assert prunable_store_dir(state, repo, []) is None


# ---------------------------------------------------------------------------
# leftover_slug_parent (TC-1.7 .. TC-1.9)
# ---------------------------------------------------------------------------


def test_slug_parent_of_a_per_checkout_dir_is_leftover(
    state: CodeIndexState,
) -> None:
    per_checkout = code_index_paths(state.settings, "demo", repo_path="/worktrees/gone").repo_dir
    assert leftover_slug_parent(state, per_checkout) == per_checkout.parent


def test_legacy_layout_dir_never_exposes_a_parent(
    state: CodeIndexState,
) -> None:
    # A legacy row's data_dir IS the slug dir: own.parent is repos/ — the
    # slug dir's contents are not prune's to judge, so nothing to rmdir.
    legacy = code_index_paths(state.settings, "demo").repo_dir
    assert leftover_slug_parent(state, legacy) is None


def test_arbitrary_dir_never_exposes_a_parent(state: CodeIndexState, tmp_path: Path) -> None:
    # Not under repos/ at all: no structural claim, no rmdir.
    odd = tmp_path / "elsewhere" / "demo"
    assert leftover_slug_parent(state, odd) is None


def test_grace_constant_is_seven_days() -> None:
    assert PRUNE_GRACE_SECONDS == 7 * 24 * 3600
