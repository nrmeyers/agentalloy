"""Pure domain helpers for phase management and workflow skill loading.

The proxy path (``proxy_signal``) and the watcher reuse this logic without
pulling in CLI dependencies (argparse, Rich, etc.).

Public API
----------
_read_phase, _write_phase_atomic, _load_workflow_skill_for_phase,
_load_workflow_skill_from_packs, _build_predicate_context
"""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agentalloy.signals.predicates import PredicateContext

__all__ = [
    "LIFECYCLE_MODES",
    "_build_predicate_context",
    "_load_workflow_skill_for_phase",
    "_load_workflow_skill_from_packs",
    "_read_announced",
    "_read_announced_state",
    "_read_composed",
    "_read_cursor",
    "_read_lifecycle_mode",
    "_read_phase",
    "_write_announced_atomic",
    "_write_composed_atomic",
    "_write_cursor_atomic",
    "_write_lifecycle_mode",
    "_write_phase_atomic",
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


def _read_state(project_root: Path, name: str) -> str | None:
    """Read a single-line ``.agentalloy/<name>`` cadence-state file.

    Returns ``None`` when the file is absent, unreadable, or empty. Shared by the
    announce-state (``announced``), the work-item cursor (``cursor``), and the
    last-composed cursor (``composed``) — all single-value durable cadence keys.
    """
    state_file = project_root / ".agentalloy" / name
    if not state_file.exists():
        return None
    try:
        return state_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_state_atomic(project_root: Path, name: str, value: str) -> None:
    """Atomically write *value* to ``.agentalloy/<name>``.

    Mirrors ``_write_phase_atomic``: a per-writer temp file + ``os.replace`` so
    the watcher and the async proxy never leave a half-written file when they
    race without a shared lock.
    """
    state_file = project_root / ".agentalloy" / name
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f"{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"{value}\n", encoding="utf-8")
        os.replace(tmp, state_file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# Cap on how many distinct session keys we remember as "already oriented" for a
# phase. Bounds the announced file and lets a few concurrent sessions in the same
# repo+phase coexist without re-announcing each other every turn (LRU-ish: oldest
# dropped first). New phases reset the set.
_MAX_ANNOUNCED_SESSIONS = 8


def _read_announced_state(project_root: Path) -> tuple[str | None, list[str]]:
    """Read ``.agentalloy/announced`` as ``(phase, [session_keys])``.

    The file stores ``"<phase>\\t<key1>,<key2>,..."`` — the phase plus the set of
    sessions already oriented for it — so orientation is keyed per *(phase,
    session)*: a new session on an already-announced phase still re-orients, while
    a session already in the set stays quiet. A legacy bare-``phase`` file (no tab)
    parses to ``(phase, [])``, so the next real request re-announces once (benign).
    ``(None, [])`` means nothing announced yet.
    """
    raw = _read_state(project_root, "announced")
    if raw is None:
        return None, []
    phase, _, keys_csv = raw.partition("\t")
    keys = [k for k in keys_csv.split(",") if k]
    return (phase or None), keys


def _read_announced(project_root: Path) -> str | None:
    """Read just the last-announced phase from ``.agentalloy/announced``.

    ``None`` (absent/unreadable/empty) means "nothing announced yet". Kept as the
    phase-only view over :func:`_read_announced_state` for callers/tests that only
    care about the phase.
    """
    return _read_announced_state(project_root)[0]


def _write_announced_atomic(
    project_root: Path, phase: str, session_keys: list[str] | None = None
) -> None:
    """Atomically record *(phase, session_keys)* as announced (Tier 1 cadence).

    Writes ``"<phase>\\t<key1>,<key2>,..."``; a bare ``phase`` when no session keys
    (back-compat with the historical single-value format).
    """
    keys = [k for k in (session_keys or []) if k]
    value = f"{phase}\t{','.join(keys)}" if keys else phase
    _write_state_atomic(project_root, "announced", value)


def _read_cursor(project_root: Path) -> str | None:
    """Read the current work-item cursor from ``.agentalloy/cursor``.

    The value is a contract id relative to ``.agentalloy/contracts/`` (e.g.
    ``build/cache-write.md``). ``None`` means no explicit cursor — the proxy
    falls back to the phase's incoming contract.
    """
    return _read_state(project_root, "cursor")


def _write_cursor_atomic(project_root: Path, cursor: str) -> None:
    """Atomically set the current work-item cursor (advanced by ``task next``)."""
    _write_state_atomic(project_root, "cursor", cursor)


def _read_composed(project_root: Path) -> str | None:
    """Read the last-composed cursor from ``.agentalloy/composed``.

    Records the cursor whose Tier 2 (domain) block was last injected. Tier 2
    fires once per work-item: when ``composed != cursor``.
    """
    return _read_state(project_root, "composed")


def _write_composed_atomic(project_root: Path, cursor: str) -> None:
    """Atomically record *cursor* as the last-composed work-item (Tier 2 cadence)."""
    _write_state_atomic(project_root, "composed", cursor)


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
    """Load the active workflow skill for the given phase.

    Shipped-first: the skill's load-bearing structured fields (``exit_gates``,
    ``applies_to_phases``, ``contract_template``, ``signal_keywords``) ALWAYS come
    from the shipped ``_packs`` skill — they are product-owned mechanics. A
    profile override may contribute only ``raw_prose`` (+ ``domain_tags``), and
    only if that prose retains every load-bearing invariant (file/contract paths
    + authored command tokens). If the override prose drops an invariant, the
    shipped prose is served instead (the runtime fall-back guard).

    Args:
        phase: The current phase (e.g. "build").
        cwd: The working directory for profile detection. Defaults to ``Path.cwd()``.
    """
    from agentalloy.signals.invariants import overlay_prose

    if cwd is None:
        cwd = Path.cwd()
    shipped = _load_workflow_skill_from_packs(phase)
    if shipped is None:
        return None

    override_prose, override_tags = _load_workflow_prose_override(
        str(shipped.get("skill_id", "")), cwd
    )
    eff, missing = overlay_prose(shipped, override_prose, override_tags)
    if missing:
        logger.warning(
            "workflow override for '%s' dropped load-bearing token(s) %s; serving shipped prose",
            shipped.get("skill_id"),
            missing,
        )
    return eff


def _load_workflow_prose_override(skill_id: str, cwd: Path) -> tuple[str | None, list[str] | None]:
    """Return ``(raw_prose, domain_tags)`` from the active profile override for
    ``skill_id``, or ``(None, None)`` when there is no enabled override.

    Only the customizable fields are read; structured fields are deliberately
    ignored (re-sourced from the shipped skill by the caller).
    """
    if not skill_id:
        return None, None
    try:
        import duckdb

        from agentalloy.profiles import detect_profile, profile_datastore_path

        profile = detect_profile(cwd=cwd)
        db_path = profile_datastore_path(profile.name if profile else "default")
        if not db_path.exists():
            return None, None
        base = (
            "SELECT raw_prose, domain_tags FROM profile_skills "
            "WHERE skill_class = 'workflow' AND skill_id = ?"
        )
        with duckdb.connect(str(db_path), read_only=True) as con:
            try:
                # Skip overrides disabled by upgrade re-validation.
                row = con.execute(base + " AND enabled", [skill_id]).fetchone()
            except Exception:
                # Pre-migration profile DB without the `enabled` column.
                row = con.execute(base, [skill_id]).fetchone()
    except Exception:
        return None, None
    if not row:
        return None, None
    raw_prose, domain_tags = row
    return (
        str(raw_prose) if raw_prose is not None else None,
        list(domain_tags) if domain_tags else None,
    )


def _read_intake_route(project_root: Path) -> str | None:
    """The ``route`` field declared by the intake contract, or ``None``.

    Reads the newest contract under ``.agentalloy/contracts/intake/`` and returns
    its ``route`` (``"full"`` | ``"fast"``). Best-effort: any failure (no dir, no
    contract, malformed frontmatter, unreadable) returns ``None``. Never raises.
    """
    intake_dir = project_root / ".agentalloy" / "contracts" / "intake"
    try:
        candidates = sorted(intake_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not candidates:
        return None
    try:
        from agentalloy.contracts import parse_contract

        return parse_contract(candidates[0]).route
    except Exception:
        return None


def _intake_route_hint(project_root: Path) -> str | None:
    """Next-phase hint when leaving intake — the intake contract's ``route`` rules.

    Routing is authoritative on the intake contract's ``route`` field: ``fast``
    selects the compressed ``sdd-fast`` lane, ``full`` (the default) advances the
    linear graph intake → spec. The field is trusted directly — intake's exit gate
    is route-agnostic (``contracts/**/*.md``), so the destination phase composes
    against whatever work-item exists.

    When no intake contract is readable, fall back to the prior-authors-next
    cascade signal: the presence of a ``contracts/sdd-fast/`` work-item selects the
    fast route. Best-effort; any read failure falls back to the default full route.
    """
    route = _read_intake_route(project_root)
    if route == "fast":
        return "sdd-fast"
    if route == "full":
        return None

    # No readable intake contract: fall back to directory-presence (cascade).
    fast_dir = project_root / ".agentalloy" / "contracts" / "sdd-fast"
    try:
        if fast_dir.is_dir() and any(fast_dir.glob("*.md")):
            return "sdd-fast"
    except OSError:
        return None
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
