# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""File-system watcher loop for sidecar harnesses.

Sidecar harnesses are those whose LLM traffic cannot be intercepted by the
AgentAlloy proxy (they ignore base-URL overrides or route to their own
backends). The watcher keeps their static rules files in sync with the
current project phase.

Watches:
  - .agentalloy/phase        → regenerate on change

Contract watching has been removed — the service write is now the compose
trigger. The ``_compose_from_contract`` shell-out and
``.agentalloy/contracts/**`` watching path are deleted.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

_log = logging.getLogger(__name__)


@dataclass
class WatchConfig:
    project_root: Path
    profile_name: str
    harness: str
    poll_interval_s: float = 1.0
    debounce_ms: int = 500


def _load_watch_config(config_path: Path) -> WatchConfig | None:
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text())
        return WatchConfig(
            project_root=Path(data["project_root"]),
            profile_name=data.get("profile_name", "default"),
            harness=data["harness"],
            poll_interval_s=data.get("poll_interval_s", 1.0),
            debounce_ms=data.get("debounce_ms", 500),
        )
    except Exception as exc:
        _log.error("Failed to load watch config: %s", exc)
        return None


def _load_workflow_skill_prose(phase: str, profile_name: str) -> str:
    """Load raw_prose for the workflow skill matching the given phase."""
    try:
        from agentalloy.signals.skill_loader import (
            _load_workflow_skill_for_phase,
        )

        skill = _load_workflow_skill_for_phase(phase, Path.cwd())
        if skill:
            return skill.get("raw_prose", "")
    except Exception as exc:
        _log.debug("skill load failed: %s", exc)
    return ""


class _AgentAlloyHandler(FileSystemEventHandler):
    def __init__(
        self,
        config: WatchConfig,
        regenerate: Callable[[str, Path], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._regenerate = regenerate
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending_events: list[str] = []

    def _schedule(self, event_type: str, path: str) -> None:
        with self._lock:
            self._pending_events.append(f"{event_type}:{path}")
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            delay = self._config.debounce_ms / 1000.0
            self._debounce_timer = threading.Timer(delay, self._flush)
            self._debounce_timer.start()

    def _flush(self) -> None:
        with self._lock:
            events = list(self._pending_events)
            self._pending_events.clear()
            self._debounce_timer = None

        if not events:
            return

        # The phase file (.agentalloy/phase) was deleted during the store
        # migration (slice 08).  Its branch is dead code — regeneration now
        # happens via the in-process store hook (register_watcher) for
        # proxy-wired harnesses.  The sidecar watcher path is deprecated;
        # keep the handler as a no-op so `agentalloy watch` stays honest
        # rather than silently exiting.
        _log.debug(
            "Watcher tick for %s but phase file no longer exists — "
            "regeneration is handled by the store hook",
            self._config.harness,
        )

    def shutdown(self) -> None:
        """Cancel any pending debounce timer so late callbacks cannot fire."""
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("modified", str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule("created", str(event.src_path))


def run_watcher(config: WatchConfig) -> None:
    """Long-running watcher loop. Blocks until SIGTERM/SIGINT."""
    from agentalloy.watch.regenerators import REGENERATORS

    regen = REGENERATORS.get(config.harness)
    if regen is None:
        _log.error("No regenerator for harness '%s'. Known: %s", config.harness, list(REGENERATORS))
        return

    # Deprecation warning for the hooks/sidecar model
    print(
        "DEPRECATION: the hooks/sidecar watch model is deprecated. "
        "The proxy model is the recommended approach. "
        "See docs for migration.",
        file=sys.stderr,
    )

    # Set up log file
    log_dir = Path.home() / ".agentalloy" / "watch"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{config.profile_name}.log"
    fh = logging.FileHandler(str(log_file))
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.INFO)

    # Write pidfile atomically (temp + os.replace) so readers never see
    # a partial write.
    pid_file = log_dir / f"{config.profile_name}.pid"
    pid_tmp = pid_file.with_suffix(".pid.tmp")
    pid_tmp.write_text(str(os.getpid()))
    os.replace(pid_tmp, pid_file)

    watch_path = config.project_root / ".agentalloy"
    watch_path.mkdir(parents=True, exist_ok=True)

    handler = _AgentAlloyHandler(config, regen)
    observer = Observer()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()
    _log.info(
        "Watching %s for harness=%s profile=%s",
        watch_path,
        config.harness,
        config.profile_name,
    )

    stop_event = threading.Event()

    def _on_signal(signum: int, frame: object) -> None:
        _log.info("Received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=config.poll_interval_s)
    finally:
        handler.shutdown()
        observer.stop()
        observer.join()
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)
        _log.info("Watcher stopped")


# ---------------------------------------------------------------------------
# In-process store hook (slice 07: watch-store-hook)
# ---------------------------------------------------------------------------


def register_watcher(
    store: Any,  # DuckDBStateStore
    project_root: Path,
    profile_name: str,
    harness: str,
) -> None:
    """Register an in-process callback on the store so the watcher fires
    post-commit when the phase row changes.

    The callback loads the workflow skill prose for the new phase and
    regenerates the harness's rules file through the appropriate
    regenerator.  The store registry is harness-agnostic — it knows only
    kinds and callables.  Per-harness output from ``wire_harness`` is
    unchanged and stays.

    This replaces the sidecar's file-based watch for harnesses that run
    inside the service process (proxy-wired harnesses).  Sidecar harnesses
    that run as a separate process keep their file-based watch.
    """
    from agentalloy.watch.regenerators import REGENERATORS  # noqa: PLC0415

    regen = REGENERATORS.get(harness)
    if regen is None:
        _log.warning("No regenerator for harness '%s'; skipping store hook", harness)
        return

    def _on_phase_write(kind: str, value: str, repo: str, stream: str) -> None:  # noqa: ARG001
        new_phase = _phase_from_blob(value)
        if new_phase is not None:
            _regenerate(regen, harness, project_root, profile_name, new_phase)

    store.on_write("phase", _on_phase_write)


def _phase_from_blob(value: str) -> str | None:
    """Extract the phase name from a stored phase value, or ``None``."""
    import json  # noqa: PLC0415

    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return (
        cast(str | None, data.get("phase")) if isinstance(data, dict) else str(data).strip() or None
    )


def _regenerate(
    regen: Any,
    harness: str,
    project_root: Path,
    profile_name: str,
    new_phase: str,
) -> None:
    """Rewrite *harness*'s rules file in *project_root* for *new_phase*."""
    prose = _load_workflow_skill_prose(new_phase, profile_name)
    if not prose:
        return
    content = f"# Active Phase: {new_phase}\n\n{prose}"
    try:
        regen(content, project_root)
        _log.info("Regenerated %s rules file via store hook (phase=%s)", harness, new_phase)
    except Exception:
        _log.warning("Regeneration failed via store hook for harness '%s'", harness, exc_info=True)


def register_wired_repos_watcher(store: Any, *, profile_name: str = "default") -> None:
    """Register one phase hook covering every wired repo, resolved at fire time.

    Registering a per-repo callback at startup snapshots
    ``harness_files_written``, and that snapshot goes stale the moment a repo or
    harness is wired against a running service: the phase row still changes, the
    rules file silently stops tracking it, and only a restart fixes it. Reading
    the wiring records on each fire keeps late wiring covered.

    Scoped by repo and stream — only the worktree whose row changed is
    regenerated. One store serves every repo (and, via a shared repo key, every
    worktree of a repo) on the machine, so an unscoped hook would rewrite every
    wired repo's rules file on any repo's phase advance, and a repo-only scope
    would still cross-contaminate sibling worktrees of the same repo.
    """

    def _on_phase_write(kind: str, value: str, repo: str, stream: str) -> None:  # noqa: ARG001
        new_phase = _phase_from_blob(value)
        if new_phase is None:
            return

        from agentalloy.api.state_router import _repo_key_for, _stream_key_for  # noqa: PLC0415
        from agentalloy.install import state as install_state  # noqa: PLC0415
        from agentalloy.watch.regenerators import REGENERATORS  # noqa: PLC0415

        seen: set[tuple[str, str]] = set()
        for entry in install_state.load_state().get("harness_files_written") or []:
            harness = entry.get("harness")
            root = entry.get("repo_root")
            if not harness or not root or (harness, root) in seen:
                continue
            seen.add((harness, root))
            if _repo_key_for(root) != repo or _stream_key_for(root) != stream:
                continue
            regen = REGENERATORS.get(harness)
            if regen is None:
                _log.warning("No regenerator for harness '%s'; skipping store hook", harness)
                continue
            _regenerate(regen, harness, Path(root), profile_name, new_phase)

    store.on_write("phase", _on_phase_write)
