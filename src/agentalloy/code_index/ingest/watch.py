"""Minimal watchdog-based repo watcher with debounced index triggers.

Slim adaptation of codebase-indexer's ``watch_manager`` essence: one watchdog
observer per watched repo, a per-repo trailing-edge debouncer (a burst of
filesystem events collapses to ONE trigger), a hard cap on concurrent
watches. Everything else — partial-index hash caches, per-watch job plumbing
— stays out; the trigger callback simply asks the service to start an
incremental index job.

The callback fires on a watchdog observer thread; the wiring layer
(``CodeIndexState.enable_watch``) marshals it onto the event loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from agentalloy.code_index.ingest.markdown import EXCLUDED_DIRS

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5
MAX_WATCHES = 32


class WatchCapacityError(RuntimeError):
    """Raised when starting a watch would exceed :data:`MAX_WATCHES`."""


class Debouncer:
    """Trailing-edge debounce: ``fire`` runs once, ``delay_s`` after the last
    :meth:`poke`. Thread-safe (watchdog delivers events on its own thread)."""

    def __init__(self, delay_s: float, fire: Callable[[], None]) -> None:
        self._delay_s = delay_s
        self._fire = fire
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def poke(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay_s, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def _is_relevant(path_str: str) -> bool:
    """Skip noise dirs (VCS, deps, build output) — same set markdown uses."""
    return not any(part in EXCLUDED_DIRS for part in Path(path_str).parts)


class _RepoEventHandler(FileSystemEventHandler):
    def __init__(self, debouncer: Debouncer) -> None:
        self._debouncer = debouncer

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if _is_relevant(str(event.src_path)):
            self._debouncer.poke()


class WatchManager:
    """Tracks up to :data:`MAX_WATCHES` repo watches, each with its own
    observer + debouncer. ``on_change(slug, repo_path)`` fires (on a watchdog
    thread) once per settled burst of file events."""

    def __init__(
        self,
        on_change: Callable[[str, Path], None],
        *,
        debounce_s: float = DEBOUNCE_SECONDS,
        observer_factory: Callable[[], BaseObserver] = Observer,
    ) -> None:
        self._on_change = on_change
        self._debounce_s = debounce_s
        self._observer_factory = observer_factory
        self._lock = threading.Lock()
        self._watches: dict[str, tuple[BaseObserver, Debouncer]] = {}

    def start(self, slug: str, repo_path: Path) -> None:
        """Begin watching ``repo_path`` under ``slug``. Idempotent per slug;
        raises :class:`WatchCapacityError` beyond the per-process cap."""
        with self._lock:
            if slug in self._watches:
                return
            if len(self._watches) >= MAX_WATCHES:
                raise WatchCapacityError(
                    f"watch cap reached ({MAX_WATCHES}); stop another repo first"
                )
            debouncer = Debouncer(self._debounce_s, lambda: self._on_change(slug, repo_path))
            observer = self._observer_factory()
            observer.schedule(_RepoEventHandler(debouncer), str(repo_path), recursive=True)
            observer.daemon = True
            observer.start()
            self._watches[slug] = (observer, debouncer)
        logger.info("code_index.watch started slug=%s path=%s", slug, repo_path)

    def stop(self, slug: str) -> bool:
        """Stop one watch. True iff it was active."""
        with self._lock:
            entry = self._watches.pop(slug, None)
        if entry is None:
            return False
        observer, debouncer = entry
        debouncer.cancel()
        observer.stop()
        return True

    def stop_all(self) -> None:
        with self._lock:
            entries = list(self._watches.values())
            self._watches.clear()
        for observer, debouncer in entries:
            debouncer.cancel()
            observer.stop()

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._watches)
