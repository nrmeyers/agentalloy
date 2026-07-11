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

from agentalloy.install.corpus_write_route import CorpusWriteRoute
from agentalloy.install.subcommands.lessons import (
    add_parser,
    probe_lesson_duplicates,
    promote_lesson,
)

_WRITE_HOST = lambda: CorpusWriteRoute("write_host")  # noqa: E731 — test seam

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
        route_fn=_WRITE_HOST,
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
        route_fn=_WRITE_HOST,
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
        route_fn=_WRITE_HOST,
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
        route_fn=_WRITE_HOST,
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
        route_fn=_WRITE_HOST,
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
        route_fn=_WRITE_HOST,
    )
    assert res["action"] == "dedup_probe_failed"
    assert installed == []


# --- #390: honest install propagation + corpus-writability preflight --------


def test_install_failure_propagates_not_promoted(tmp_path: Path):
    """The rail rolled back (e.g. corpus lock) -> action is install_failed, never
    "promoted" — the misleading-success render bug (#390)."""
    _write_lesson(tmp_path, "locked-lesson")
    res = promote_lesson(
        "locked-lesson",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([]),
        install=lambda *a, **k: {
            "action": "ingested_with_errors",
            "skills_ingested": 0,
            "ingest_results": [
                {
                    "yaml": "x.yaml",
                    "outcome": "failed",
                    "stderr_tail": "error: failed to open the skill store: Could not set lock",
                }
            ],
            "remediation": "Batch install failed — rolled back all ingested skills.",
        },
        route_fn=_WRITE_HOST,
    )
    assert res["action"] == "install_failed"
    assert "Could not set lock" in res["error"]
    assert "rolled back" in res["remediation"]
    assert res["install"]["action"] == "ingested_with_errors"


def test_install_ok_actions_still_promote(tmp_path: Path):
    """Non-failure rail outcomes (already_installed / version_unchanged) stay
    "promoted" — only genuine failures flip the action."""
    for ok_action in ("ingested", "already_installed", "version_unchanged"):
        _write_lesson(tmp_path, f"ok-{ok_action.replace('_', '-')}")
        res = promote_lesson(
            f"ok-{ok_action.replace('_', '-')}",
            root=tmp_path,
            embed=_embed,
            vector_store=_FakeStore([]),
            install=lambda *a, **k: {"action": ok_action},  # noqa: B023
            route_fn=_WRITE_HOST,
        )
        assert res["action"] == "promoted", ok_action


def test_blocked_route_blocks_before_probe_and_install(tmp_path: Path):
    """A blocked route fails fast: the probe never embeds, the rail never runs,
    and the result carries the reason + a hand-install remediation (AC-6)."""
    _write_lesson(tmp_path, "blocked-lesson")
    installed: list[Path] = []

    def _never_embed(_text: str) -> list[float]:
        raise AssertionError("probe must not run when the corpus is blocked")

    reason = "the corpus is locked by the running AgentAlloy service."
    res = promote_lesson(
        "blocked-lesson",
        root=tmp_path,
        embed=_never_embed,
        vector_store=_FakeStore([]),
        install=lambda pack_dir, **_k: installed.append(pack_dir) or {"ok": True},
        route_fn=lambda: CorpusWriteRoute("blocked", reason=reason),
    )
    assert res["action"] == "install_blocked"
    assert installed == []
    assert "locked" in res["error"]
    assert "install-pack" in res["remediation"]
    assert Path(res["pack_dir"]).is_dir()  # the generated pack survives for hand-install


def test_via_service_pushes_and_promotes(tmp_path: Path):
    """via_service: the host never probes; the pack is pushed and a good service
    result promotes (AC-1)."""
    _write_lesson(tmp_path, "svc-lesson")
    pushed: list[Any] = []

    def _never_embed(_text: str) -> list[float]:
        raise AssertionError("host must not probe on the via_service path")

    def _push(pack_dir, *, route, allow_duplicates, reembed):
        pushed.append((pack_dir, route.port, reembed))
        return {"action": "already_installed", "skills_ingested": 1}

    res = promote_lesson(
        "svc-lesson",
        root=tmp_path,
        embed=_never_embed,
        vector_store=_FakeStore([_Hit("x", "would-dup", 0.0)]),  # ignored — no host probe
        route_fn=lambda: CorpusWriteRoute("via_service", port=47950),
        push_fn=_push,
    )
    assert res["action"] == "promoted"
    assert pushed and pushed[0][1] == 47950 and pushed[0][2] is True


def test_via_service_duplicate_surfaces(tmp_path: Path):
    """The endpoint's duplicate verdict surfaces as duplicate_refused (AC-4/AC-10)."""
    _write_lesson(tmp_path, "svc-dup")
    res = promote_lesson(
        "svc-dup",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([]),
        route_fn=lambda: CorpusWriteRoute("via_service", port=47950),
        push_fn=lambda *a, **k: {
            "action": "duplicate_refused",
            "duplicates": ["existing-skill"],
            "error": "duplicates existing corpus skill(s)",
        },
    )
    assert res["action"] == "duplicate_refused"
    assert res["duplicates"] == ["existing-skill"]
    assert res["slug"] == "svc-dup"  # promote enriches the endpoint result


def test_via_service_http_failure_maps_to_install_failed(tmp_path: Path):
    """A transport/HTTP failure from the client maps to install_failed (AC-10)."""
    _write_lesson(tmp_path, "svc-down")
    res = promote_lesson(
        "svc-down",
        root=tmp_path,
        embed=_embed,
        vector_store=_FakeStore([]),
        route_fn=lambda: CorpusWriteRoute("via_service", port=47950),
        push_fn=lambda *a, **k: {
            "action": "install_failed",
            "error": "could not reach the ingest service",
        },
    )
    assert res["action"] == "install_failed"
    assert "ingest service" in res["error"]


def test_default_blocker_detects_duck_lock(tmp_path: Path, monkeypatch):
    """_corpus_write_blocker: a held write connection on the store -> lock message.

    DuckDB's lock conflict is process-level (same-process connects share the
    instance), so the "running service" is a real child process holding an rw
    connection, readiness-signaled via a marker file."""
    import subprocess
    import sys
    import time

    from agentalloy.install.subcommands import lessons

    db = tmp_path / "agentalloy.duck"
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, pathlib, time, sys; "
            f"c = duckdb.connect({str(db)!r}); "
            f"pathlib.Path({str(ready)!r}).touch(); "
            "time.sleep(30)",
        ]
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert time.monotonic() < deadline, "lock-holder child never became ready"
            assert holder.poll() is None, "lock-holder child exited early"
            time.sleep(0.05)
        monkeypatch.setattr(
            "agentalloy.config.get_settings",
            lambda: type("S", (), {"duckdb_path": str(db)})(),
        )
        reason = lessons._corpus_write_blocker()
        assert reason is not None and "locked" in reason and "server-stop" in reason
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_default_blocker_flags_serving_container(tmp_path: Path, monkeypatch):
    """_corpus_write_blocker: container deployment + reachable port -> blocked
    (the live corpus is in the container volume); unreachable port -> no block."""
    from agentalloy.install.subcommands import lessons

    db = tmp_path / "agentalloy.duck"  # absent -> lock probe passes
    monkeypatch.setattr(
        "agentalloy.config.get_settings",
        lambda: type("S", (), {"duckdb_path": str(db)})(),
    )

    class _Target:
        deployment = "container"
        port = 47950

    monkeypatch.setattr(
        "agentalloy.install.server_proc.resolve_deployment", lambda *a, **k: _Target()
    )
    monkeypatch.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **k: True)
    reason = lessons._corpus_write_blocker()
    assert reason is not None and "container" in reason

    monkeypatch.setattr("agentalloy.install.server_proc.port_reachable", lambda *a, **k: False)
    assert lessons._corpus_write_blocker() is None


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
