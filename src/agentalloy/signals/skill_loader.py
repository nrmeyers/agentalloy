"""Pure domain helpers for phase management and workflow skill loading.

Extracted from ``install/subcommands/signal.py`` so that the proxy path
(see plan Pass 1) can reuse the same logic without pulling in CLI
dependencies (argparse, Rich, etc.).

Public API
----------
_read_phase, _write_phase_atomic, _load_workflow_skill_for_phase,
_load_workflow_skill_from_packs, _build_predicate_context, _write_telemetry
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agentalloy.signals.predicates import PredicateContext

__all__ = [
    "LIFECYCLE_MODES",
    "_build_predicate_context",
    "_load_workflow_skill_for_phase",
    "_load_workflow_skill_from_packs",
    "_read_announced",
    "_read_lifecycle_mode",
    "_read_phase",
    "_write_announced_atomic",
    "_write_lifecycle_mode",
    "_write_phase_atomic",
    "_write_telemetry",
    "exit_gates_for_phase",
]

# Per-repo lifecycle modes (see ``_read_lifecycle_mode``). ``full`` is the
# historical default; ``off`` lets a repo with its own agents and workflows
# opt out of AgentAlloy's intake front-door, phase machine, and composition.
LIFECYCLE_MODES = ("full", "off")
_DEFAULT_LIFECYCLE_MODE = "full"


# ---------------------------------------------------------------------------
# Phase file helpers
# ---------------------------------------------------------------------------


def _read_phase(project_root: Path) -> str | None:
    """Read the active phase from ``.agentalloy/phase``.

    Returns ``None`` when the file is absent, unreadable, or malformed.
    """
    phase_file = project_root / ".agentalloy" / "phase"
    if not phase_file.exists():
        return None
    try:
        import yaml

        raw = yaml.safe_load(phase_file.read_text(encoding="utf-8"))
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw_dict = cast("dict[str, Any]", raw)
            phase_val = raw_dict.get("phase")
            return str(phase_val).strip() if phase_val else None
        return str(raw).strip() or None
    except Exception:
        return None


def _write_phase_atomic(project_root: Path, phase: str) -> None:
    """Atomically write *phase* to ``.agentalloy/phase``.

    Uses a temp file + ``os.replace`` so concurrent writers never leave
    a partially-written file.
    """
    phase_file = project_root / ".agentalloy" / "phase"
    phase_file.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer: the watcher and the async proxy both call this with
    # no shared lock, so a fixed tmp name lets two writers race on the same file
    # and defeat the os.replace atomicity. A per-writer tmp keeps it atomic.
    tmp = phase_file.with_name(f"phase.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"phase: {phase}\n", encoding="utf-8")
        os.replace(tmp, phase_file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# Announce-state helpers (once-per-entry injection cadence)
# ---------------------------------------------------------------------------
#
# `.agentalloy/announced` records the last phase whose orientation block was
# injected. The proxy announces a phase's workflow block exactly once on entry
# (when `announced != phase`), then stays quiet until a transition changes the
# phase — at which point `announced` no longer matches and the new phase is
# announced. This decouples the heavy orientation block from per-turn injection:
# the marker-echo dedup it replaces was structurally dead (Claude Code never
# persists injected markers back into the next request), so cadence must live in
# durable state here, not in the request body.


def _read_announced(project_root: Path) -> str | None:
    """Read the last-announced phase from ``.agentalloy/announced``.

    Returns ``None`` when the file is absent, unreadable, or empty — which the
    proxy treats as "nothing announced yet", so the current phase announces.
    """
    announced_file = project_root / ".agentalloy" / "announced"
    if not announced_file.exists():
        return None
    try:
        return announced_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_announced_atomic(project_root: Path, phase: str) -> None:
    """Atomically record *phase* as the last-announced phase.

    Mirrors ``_write_phase_atomic``: a per-writer temp file + ``os.replace`` so
    the watcher and the async proxy never leave a half-written file when they
    race without a shared lock.
    """
    announced_file = project_root / ".agentalloy" / "announced"
    announced_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = announced_file.with_name(f"announced.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"{phase}\n", encoding="utf-8")
        os.replace(tmp, announced_file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# Lifecycle mode helpers (per-repo deferral)
# ---------------------------------------------------------------------------


def _read_lifecycle_mode(project_root: Path) -> str:
    """Read the per-repo lifecycle mode from ``.agentalloy/config``.

    Returns one of ``full`` | ``off``. Defaults to ``full``
    (historical behavior) whenever the file is absent, unreadable, malformed,
    or holds an unrecognized value — a missing/garbled config must never
    silently disable the lifecycle.

    - ``full`` — intake front-door + phase machine + composition.
    - ``off``  — compose nothing.

    The legacy ``assist`` mode was defined entirely by hook behavior; with the
    hook transport gone it has no distinct meaning and reads back as ``off``.
    """
    config_file = project_root / ".agentalloy" / "config"
    if not config_file.exists():
        return _DEFAULT_LIFECYCLE_MODE
    # Hand-parse the flat `key: value` file rather than yaml.safe_load — YAML 1.1
    # coerces bare `off`/`on`/`no` to booleans, which would silently turn the
    # `off` mode into `full`. Partition on the first colon, like ``_read_phase``.
    try:
        for line in config_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip() == "lifecycle_mode":
                mode = value.strip().strip('"').strip("'").lower()
                # Legacy ``assist`` collapsed to ``off`` when the hook transport
                # was removed; map it explicitly so it does not fall through to
                # the ``full`` default and wrongly re-enable composition.
                if mode == "assist":
                    return "off"
                return mode if mode in LIFECYCLE_MODES else _DEFAULT_LIFECYCLE_MODE
    except OSError:
        return _DEFAULT_LIFECYCLE_MODE
    return _DEFAULT_LIFECYCLE_MODE


def _write_lifecycle_mode(project_root: Path, mode: str) -> None:
    """Persist *mode* to ``.agentalloy/config`` (creating the dir as needed).

    Raises ``ValueError`` on an unrecognized mode so callers fail loudly
    rather than writing a value the reader will silently ignore.
    """
    if mode not in LIFECYCLE_MODES:
        raise ValueError(f"invalid lifecycle mode {mode!r}; expected one of {LIFECYCLE_MODES}")
    config_file = project_root / ".agentalloy" / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f"lifecycle_mode: {mode}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Workflow skill loading
# ---------------------------------------------------------------------------


def _load_workflow_skill_for_phase(phase: str, cwd: Path | None = None) -> dict[str, Any] | None:
    """Load the active workflow skill for the given phase from the profile datastore.

    Tries the DuckDB-backed profile store first; falls back to ``_packs``.

    Args:
        phase: The current phase (e.g. "build").
        cwd: The working directory for profile detection. Defaults to ``Path.cwd()``.
    """
    if cwd is None:
        cwd = Path.cwd()
    try:
        import duckdb

        from agentalloy.profiles import detect_profile, profile_datastore_path

        profile = detect_profile(cwd=cwd)
        db_path = profile_datastore_path(profile.name if profile else "default")
        if db_path.exists():
            with duckdb.connect(str(db_path), read_only=True) as con:
                row = con.execute(
                    """
                    SELECT skill_id, raw_prose, applies_to_phases, exit_gates, signal_keywords
                    FROM profile_skills
                    WHERE skill_class = 'workflow'
                    """,
                ).fetchall()
            for r in row:
                skill_id, raw_prose, applies_to_phases, exit_gates_raw, signal_keywords_raw = r
                applies: list[str] = list(applies_to_phases or [])
                if phase in applies:
                    exit_gates: dict[str, Any] = {}
                    if exit_gates_raw:
                        import contextlib

                        with contextlib.suppress(Exception):
                            exit_gates = json.loads(exit_gates_raw)
                    signal_keywords: list[str] = list(signal_keywords_raw or [])
                    return {
                        "skill_id": skill_id,
                        "raw_prose": raw_prose,
                        "applies_to_phases": applies,
                        "exit_gates": exit_gates,
                        "signal_keywords": signal_keywords,
                    }
    except Exception:
        pass
    # Fallback: load from _packs
    return _load_workflow_skill_from_packs(phase)


def _intake_route_hint(project_root: Path) -> str | None:
    """Next-phase hint when leaving intake, from the active intake contract's route.

    Returns ``"sdd-fast"`` when the contract chose the fast lane, else ``None``
    (the linear graph then advances intake → spec). Best-effort: any read/parse
    failure falls back to the default full route.
    """
    contracts_dir = project_root / ".agentalloy" / "contracts" / "intake"
    if not contracts_dir.is_dir():
        return None
    try:
        from agentalloy.contracts import parse_contract

        md = sorted(contracts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in md:
        try:
            return "sdd-fast" if parse_contract(path).route == "fast" else None
        except Exception:
            continue
    return None


def _load_workflow_skill_from_packs(phase: str) -> dict[str, Any] | None:
    """Fallback: load a workflow skill from the shipped ``_packs/sdd`` directory."""
    try:
        import yaml

        import agentalloy

        packs_root = Path(agentalloy.__file__).resolve().parent / "_packs" / "sdd"
        for f in packs_root.glob("sdd-*.yaml"):
            data: dict[str, Any] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if data.get("skill_class") == "workflow" and phase in (
                data.get("applies_to_phases") or []
            ):
                return data
    except Exception:
        pass
    return None


def exit_gates_for_phase(phase: str) -> dict[str, Any] | None:
    """Exit-gate spec for ``phase``, read from the wheel-bundled ``_packs/sdd`` YAML.

    Corpus/DB-free: this reads the packaged skill YAML directly (via
    ``_load_workflow_skill_from_packs``) rather than the DuckDB corpus, so the
    guarded ``phase set`` can check a phase's deterministic exit gates without
    touching the database or the embed server. Returns ``None`` when the phase
    has no packaged workflow skill, or that skill declares no ``exit_gates``.
    """
    skill = _load_workflow_skill_from_packs(phase)
    if not skill:
        return None
    gates = skill.get("exit_gates")
    return cast("dict[str, Any]", gates) if isinstance(gates, dict) else None


# ---------------------------------------------------------------------------
# Predicate context builder
# ---------------------------------------------------------------------------


def _build_predicate_context(
    project_root: Path,
    phase: str | None,
    prompt_text: str | None = None,
    tool_name: str | None = None,
    tool_path: str | None = None,
    file_events: list[Path] | None = None,
) -> PredicateContext:
    """Build a ``PredicateContext`` for gate evaluation."""
    from agentalloy.signals.predicates import PredicateContext

    recent_tool_use: dict[str, Any] | None = None
    if tool_name:
        recent_tool_use = {"tool": tool_name, "path": tool_path or "", "args": {}}

    return PredicateContext(
        project_root=project_root,
        current_phase=phase,
        recent_prompt_text=prompt_text,
        recent_tool_use=recent_tool_use,
        file_events_since=file_events or [],
        contracts_root=project_root / ".agentalloy" / "contracts",
    )


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _write_telemetry(record: dict[str, Any]) -> None:
    """Write a telemetry record to the vector store (soft-fail)."""
    try:
        from agentalloy.profiles import domain_datastore_path
        from agentalloy.storage.vector_store import CompositionTrace, append_trace

        db_path = domain_datastore_path()
        if not db_path.exists():
            return
        trace = CompositionTrace(
            trace_id=str(uuid.uuid4()),
            request_ts=int(time.time() * 1000),
            phase=record.get("phase", ""),
            task_prompt=record.get("task", "")[:500],
            status="signal",
            event_type=record.get("event_type", "phase_eval"),
            pre_filter_matched=record.get("pre_filter_matched"),
            gates_met=record.get("gates_met", []),
            gates_unmet=record.get("gates_unmet", []),
            qwen_calls=record.get("qwen_calls", 0),
        )
        append_trace(db_path, trace)
    except Exception:
        pass
