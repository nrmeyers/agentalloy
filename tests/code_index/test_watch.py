"""ingest.watch — debounce collapse, relevance filter, watch cap."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from watchdog.events import FileModifiedEvent

from agentalloy.code_index.ingest import watch as watch_mod
from agentalloy.code_index.ingest.watch import (
    MAX_WATCHES,
    Debouncer,
    WatchCapacityError,
    WatchManager,
    _RepoEventHandler,
)


class DummyObserver:
    """Stands in for a watchdog Observer; records lifecycle + handlers."""

    def __init__(self) -> None:
        self.daemon = False
        self.started = False
        self.stopped = False
        self.scheduled: list[tuple[Any, str]] = []

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.scheduled.append((handler, path))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def wait_for_count(get: Any, expected: int, *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if get() == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"expected {expected}, got {get()}")


def test_debouncer_collapses_burst() -> None:
    fired: list[float] = []
    d = Debouncer(0.05, lambda: fired.append(time.monotonic()))
    for _ in range(10):
        d.poke()
        time.sleep(0.005)
    wait_for_count(lambda: len(fired), 1)
    time.sleep(0.15)
    assert len(fired) == 1, "a burst must collapse to exactly one fire"

    d.poke()  # a later, separate event fires again
    wait_for_count(lambda: len(fired), 2)


def test_debouncer_cancel() -> None:
    fired: list[int] = []
    d = Debouncer(0.05, lambda: fired.append(1))
    d.poke()
    d.cancel()
    time.sleep(0.15)
    assert fired == []


def test_handler_burst_triggers_single_on_change(tmp_path: Path) -> None:
    triggers: list[tuple[str, Path]] = []
    manager = WatchManager(
        lambda slug, path: triggers.append((slug, path)),
        debounce_s=0.05,
        observer_factory=DummyObserver,  # type: ignore[arg-type]
    )
    manager.start("demo", tmp_path)
    observer, _ = manager._watches["demo"]  # pyright: ignore[reportPrivateUsage]
    assert isinstance(observer, DummyObserver) and observer.started and observer.daemon
    handler = observer.scheduled[0][0]

    for i in range(5):
        handler.on_any_event(FileModifiedEvent(str(tmp_path / f"f{i}.py")))
    wait_for_count(lambda: len(triggers), 1)
    time.sleep(0.15)
    assert triggers == [("demo", tmp_path)]


def test_handler_ignores_noise_dirs(tmp_path: Path) -> None:
    pokes: list[int] = []

    class CountingDebouncer(Debouncer):
        def poke(self) -> None:
            pokes.append(1)

    handler = _RepoEventHandler(CountingDebouncer(0.05, lambda: None))
    handler.on_any_event(FileModifiedEvent(str(tmp_path / ".git" / "index")))
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "node_modules" / "x" / "a.js")))
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "__pycache__" / "m.pyc")))
    assert pokes == []
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "src" / "a.py")))
    assert pokes == [1]


def test_watch_cap_and_idempotent_start(tmp_path: Path) -> None:
    manager = WatchManager(
        lambda _slug, _path: None,
        debounce_s=0.05,
        observer_factory=DummyObserver,  # type: ignore[arg-type]
    )
    assert watch_mod.MAX_WATCHES == 32
    for i in range(MAX_WATCHES):
        manager.start(f"repo-{i}", tmp_path)
    assert len(manager.active()) == MAX_WATCHES

    # Restarting an existing slug is a no-op, not a capacity error.
    manager.start("repo-0", tmp_path)
    assert len(manager.active()) == MAX_WATCHES

    try:
        manager.start("one-too-many", tmp_path)
        raise AssertionError("expected WatchCapacityError")
    except WatchCapacityError:
        pass

    assert manager.stop("repo-0") is True
    assert manager.stop("repo-0") is False
    manager.start("one-too-many", tmp_path)  # freed slot is reusable

    manager.stop_all()
    assert manager.active() == []
