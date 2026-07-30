"""``_collect_store_design_chunks`` — design approach.md bodies read from the
SDD artifact store (post specs/final_migration.md) instead of disk, for the
Knowledge module's decision-linkage ingest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.api.state_router import _repo_key_for
from agentalloy.code_index.ingest.pipeline import (
    _collect_store_design_chunks,  # pyright: ignore[reportPrivateUsage]
    _is_decision_source,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.storage.state_store import process_store


def test_no_bound_store_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentalloy.storage.state_store.process_store", lambda: None)
    assert _collect_store_design_chunks(tmp_path) == []


def test_no_design_artifacts_returns_empty(tmp_path: Path) -> None:
    assert _collect_store_design_chunks(tmp_path) == []


def test_design_artifact_becomes_decision_source_chunk(tmp_path: Path) -> None:
    store = process_store()
    assert store is not None
    scoped = store.for_repo(_repo_key_for(str(tmp_path)))
    scoped.set_artifact(
        "design",
        "widget-feature",
        "approach.md",
        "# widget-feature\n\n## Approach\n\nUse `pkg.mod.Widget` because it already owns state.\n",
    )

    chunks = _collect_store_design_chunks(tmp_path)

    assert chunks, "expected at least one synthesized chunk"
    paths = {c.file_path for c in chunks}
    assert paths == {"docs/design/widget-feature/approach.md"}
    assert all(_is_decision_source(c.file_path) for c in chunks)
    assert any("pkg.mod.Widget" in c.body for c in chunks)


def test_artifact_missing_content_or_slug_is_skipped(tmp_path: Path) -> None:
    store = process_store()
    assert store is not None
    scoped = store.for_repo(_repo_key_for(str(tmp_path)))
    # A row with empty content shouldn't produce a chunk.
    scoped.set_artifact("design", "empty-one", "approach.md", "")

    assert _collect_store_design_chunks(tmp_path) == []
