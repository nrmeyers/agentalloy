"""File-system watcher loop for sidecar harnesses.

Sidecar harnesses are those whose LLM traffic cannot be intercepted by the
AgentAlloy proxy (they ignore base-URL overrides or route to their own
backends). The watcher keeps their static rules files in sync with the
current project phase and contract state.

Watches:
  - .agentalloy/phase        → regenerate on change
  - .agentalloy/contracts/** → regenerate when new contract written
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
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
            _load_workflow_skill_for_phase,  # pyright: ignore[reportPrivateUsage]
        )

        skill = _load_workflow_skill_for_phase(phase, Path.cwd())
        if skill:
            return skill.get("raw_prose", "")
    except Exception as exc:
        _log.debug("skill load failed: %s", exc)
    return ""


def _compose_from_contract(contract_path: Path) -> str:
    """Run agentalloy compose --contract and return output."""
    try:
        from agentalloy.install import state as install_state

        st = install_state.load_state()
        port = st.get("port", 47950)
        result = subprocess.run(
            [
                "agentalloy",
                "compose",
                "--contract",
                str(contract_path),
                "--inject",
                "--port",
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception as exc:
        _log.debug("compose failed: %s", exc)
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

        project_root = self._config.project_root
        phase_file = project_root / ".agentalloy" / "phase"
        # Determine what changed
        phase_changed = any("phase" in e for e in events)
        contract_paths = [
            Path(e.split(":", 1)[1])
            for e in events
            if ".agentalloy/contracts" in e and e.split(":", 1)[1].endswith(".md")
        ]

        content_parts: list[str] = []

        if phase_changed and phase_file.exists():
            try:
                import yaml

                raw_data: Any = yaml.safe_load(phase_file.read_text()) or {}
                phase: str | None = None
                if isinstance(raw_data, dict):
                    data: dict[str, Any] = cast(dict[str, Any], raw_data)
                    phase_val = data.get("phase")
                    phase = str(phase_val) if phase_val else None
                else:
                    phase = str(raw_data).strip() or None
                if phase:
                    prose = _load_workflow_skill_prose(phase, self._config.profile_name)
                    if prose:
                        content_parts.append(f"# Active Phase: {phase}\n\n{prose}")
            except Exception as exc:
                _log.warning("phase reload failed: %s", exc)

        for cp in contract_paths:
            if cp.exists():
                composed = _compose_from_contract(cp)
                if composed:
                    content_parts.append(composed)

        if content_parts:
            content = "\n\n---\n\n".join(content_parts)
            try:
                self._regenerate(content, project_root)
                _log.info("Regenerated %s rules file", self._config.harness)
            except Exception as exc:
                _log.warning("Regeneration failed: %s", exc)

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
        "Watching %s for harness=%s profile=%s", watch_path, config.harness, config.profile_name
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
