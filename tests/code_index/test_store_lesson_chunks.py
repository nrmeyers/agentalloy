"""``_collect_store_lesson_chunks`` — compound-engineering lessons read from the
SDD artifact store (phase=qa, name=solution) instead of ``docs/solutions/``.

The load-bearing assertion here is the emitted ``file_path``. It must stay
``docs/solutions/<slug>.md`` — that string is the implicit foreign key
``knowledge_push._solutions_slug`` parses back out of a decision's qualified
name, and it is what ``_DECISION_SOURCE_GLOBS`` matches. The collector soft-fails
to ``[]``, so a wrong store call here would silently stop producing lessons with
no error anywhere; these tests are what make that loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentalloy.api.state_router import _repo_key_for
from agentalloy.code_index.ingest.pipeline import (
    _collect_store_lesson_chunks,  # pyright: ignore[reportPrivateUsage]
    _is_decision_source,  # pyright: ignore[reportPrivateUsage]
)
from agentalloy.lessons_artifact import LESSON_NAME, LESSON_PHASE
from agentalloy.storage.state_store import process_store


def _scoped(root: Path):
    store = process_store()
    assert store is not None
    return store.for_repo(_repo_key_for(str(root)))


def test_no_bound_store_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentalloy.storage.state_store.process_store", lambda: None)
    assert _collect_store_lesson_chunks(tmp_path, set()) == []


def test_no_lesson_artifacts_returns_empty(tmp_path: Path) -> None:
    assert _collect_store_lesson_chunks(tmp_path, set()) == []


def test_stored_lesson_becomes_a_decision_source_chunk(tmp_path: Path) -> None:
    _scoped(tmp_path).set_artifact(
        LESSON_PHASE,
        "widget-feature",
        LESSON_NAME,
        "# widget-feature\n\n## Approach that worked\n\nGuard `pkg.mod.Widget` at the seam.\n",
    )

    chunks = _collect_store_lesson_chunks(tmp_path, set())

    assert chunks, "expected at least one synthesized chunk"
    paths = {c.file_path for c in chunks}
    # Exactly the pre-migration on-disk shape — the knowledge_push foreign key.
    assert paths == {"docs/solutions/widget-feature.md"}
    assert all(_is_decision_source(c.file_path) for c in chunks)
    assert any("pkg.mod.Widget" in c.body for c in chunks)


def test_qa_report_artifacts_are_not_collected_as_lessons(tmp_path: Path) -> None:
    """Only ``name=solution`` rows are lessons. The qa *report* lives in the same
    phase and must not be indexed as a decision source."""
    scoped = _scoped(tmp_path)
    scoped.set_artifact(LESSON_PHASE, "widget-feature", "report.md", "# qa\n\n## Checks\n\nok\n")

    assert _collect_store_lesson_chunks(tmp_path, set()) == []


def test_disk_lesson_wins_so_a_mid_migration_repo_does_not_double_index(tmp_path: Path) -> None:
    """A slug present both on disk and in the store is emitted once. ``seen_paths``
    carries what ``collect_markdown_chunks`` already produced from disk."""
    _scoped(tmp_path).set_artifact(
        LESSON_PHASE, "widget-feature", LESSON_NAME, "# stored\n\n## Approach\n\nstored body\n"
    )

    chunks = _collect_store_lesson_chunks(tmp_path, {"docs/solutions/widget-feature.md"})

    assert chunks == []


def test_empty_content_or_missing_slug_is_skipped(tmp_path: Path) -> None:
    _scoped(tmp_path).set_artifact(LESSON_PHASE, "empty-one", LESSON_NAME, "")

    assert _collect_store_lesson_chunks(tmp_path, set()) == []
