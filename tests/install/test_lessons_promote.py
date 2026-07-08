"""Task 04: ``lessons promote`` CLI — the pre-ingest dedup probe (AC 5).

AC 5: promoting a lesson whose fragments duplicate an existing corpus skill
(cosine >= 0.92) is refused BEFORE install (the near-duplicate is never written),
unless ``--allow-duplicates`` is passed. Verified with injected embed/store/install
seams so the logic is exercised without the real embed model or corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentalloy.install.subcommands.lessons import (
    add_parser,
    probe_lesson_duplicates,
    promote_lesson,
)

LESSON = "# A lesson\n\n## Approach\n\nDo the thing that worked, carefully and in order, then confirm it.\n"


@dataclass
class _Hit:
    fragment_id: str
    skill_id: str
    distance: float


class _FakeStore:
    """A FragmentStore stub whose search returns a fixed hit list."""

    def __init__(self, hits: list[_Hit]):
        self._hits = hits
        self.closed = False

    def search_similar(
        self, query_vec: Any, *, k: int = 20, categories: Any = None, fragment_types: Any = None
    ) -> list[_Hit]:
        return list(self._hits)

    def close(self) -> None:
        self.closed = True


def _embed(_text: str) -> list[float]:
    return [0.1, 0.2, 0.3]


def _write_lesson(root: Path, slug: str) -> None:
    p = root / "docs" / "solutions" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LESSON, encoding="utf-8")


# --- the probe in isolation ------------------------------------------------


def test_probe_flags_hard_hit():
    store = _FakeStore([_Hit("existing-f0", "existing-skill", distance=0.05)])  # sim 0.95 >= 0.92
    hits = probe_lesson_duplicates(
        ["frag a", "frag b"],
        embed=_embed,
        vector_store=store,
        hard_similarity=0.92,
        soft_similarity=0.80,
    )
    assert hits and hits[0].skill_id == "existing-skill"


def test_probe_ignores_soft_only():
    store = _FakeStore([_Hit("existing-f0", "existing-skill", distance=0.15)])  # sim 0.85 -> soft
    hits = probe_lesson_duplicates(
        ["frag a"], embed=_embed, vector_store=store, hard_similarity=0.92, soft_similarity=0.80
    )
    assert hits == []


# --- promote flow: refuse vs install ---------------------------------------


def test_ac5_hard_duplicate_refused_and_not_installed(tmp_path: Path):
    _write_lesson(tmp_path, "dup-lesson")
    installed: list[Path] = []

    def _install(pack_dir: Path, **_kw: Any) -> dict[str, Any]:
        installed.append(pack_dir)
        return {"ok": True}

    res = promote_lesson(
        "dup-lesson",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([_Hit("x-f0", "existing-skill", distance=0.02)]),
        install=_install,
    )
    assert res["action"] == "duplicate_refused"
    assert res["duplicates"] == ["existing-skill"]
    assert installed == []  # the rail was never reached -> nothing written to the corpus


def test_ac5_allow_duplicates_installs(tmp_path: Path):
    _write_lesson(tmp_path, "dup-lesson")
    installed: list[Path] = []

    def _install(pack_dir: Path, **_kw: Any) -> dict[str, Any]:
        installed.append(pack_dir)
        return {"ok": True}

    res = promote_lesson(
        "dup-lesson",
        root=tmp_path,
        allow_duplicates=True,
        embed=_embed,
        vector_store=_FakeStore([_Hit("x-f0", "existing-skill", distance=0.02)]),
        install=_install,
    )
    assert res["action"] == "promoted"
    assert len(installed) == 1


def test_unique_lesson_installs(tmp_path: Path):
    _write_lesson(tmp_path, "fresh-lesson")
    installed: list[Path] = []

    def _install(pack_dir: Path, **_kw: Any) -> dict[str, Any]:
        installed.append(pack_dir)
        return {"ok": True}

    res = promote_lesson(
        "fresh-lesson",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([]),  # nothing similar in the corpus
        install=_install,
    )
    assert res["action"] == "promoted"
    assert len(installed) == 1


def test_unknown_slug_reported(tmp_path: Path):
    res = promote_lesson(
        "does-not-exist",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([]),
        install=lambda *a, **k: {},
    )
    assert res["action"] == "lesson_not_found"


def test_no_corpus_skips_probe_and_installs(tmp_path: Path, monkeypatch):
    """When the fragment store cannot be opened (fresh install, no corpus), the
    probe is skipped — there is nothing to duplicate against — and install runs."""
    _write_lesson(tmp_path, "fresh-lesson")
    installed: list[Path] = []

    def _boom(*_a, **_k):
        raise RuntimeError("no corpus")

    monkeypatch.setattr("agentalloy.storage.open.open_fragments", _boom)
    res = promote_lesson(
        "fresh-lesson",
        root=tmp_path,
        embed=_embed,
        vector_store=None,  # None -> tries open_fragments (patched to fail) -> skip
        install=lambda pack_dir, **_k: installed.append(pack_dir) or {"ok": True},
    )
    assert res["action"] == "promoted"
    assert len(installed) == 1


def test_probe_failure_fails_closed(tmp_path: Path):
    """If the probe itself errors (e.g. embed server down), promotion is refused
    rather than installing unchecked — fail closed."""
    _write_lesson(tmp_path, "err-lesson")
    installed: list[Path] = []

    def _bad_embed(_text: str) -> list[float]:
        raise RuntimeError("embed server down")

    res = promote_lesson(
        "err-lesson",
        root=tmp_path,
        embed=_bad_embed,
        vector_store=_FakeStore([]),
        install=lambda pack_dir, **_k: installed.append(pack_dir) or {"ok": True},
    )
    assert res["action"] == "dedup_probe_failed"
    assert installed == []


# --- CLI registration ------------------------------------------------------


def test_promote_registered_and_parses():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="subcommand")
    add_parser(sub)
    args = parser.parse_args(["lessons", "promote", "my-slug", "--allow-duplicates"])
    assert args.slug == "my-slug"
    assert args.allow_duplicates is True
    assert callable(args.func)
