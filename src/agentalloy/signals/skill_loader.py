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
    from agentalloy.storage.state_store import DuckDBStateStore, PhaseState

__all__ = [
    "FLOW_MODES",
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
    "_read_transitioned_by",
    "_write_announced_atomic",
    "_write_composed_atomic",
    "_write_cursor_atomic",
    "_write_lifecycle_mode",
    "_write_phase_atomic",
    "exit_gates_for_phase",
    "read_flow_state",
]

# Per-repo lifecycle modes (see ``_read_lifecycle_mode``). ``full`` is the
# historical default; ``off`` lets a repo with its own agents and workflows
# opt out of AgentAlloy's intake front-door, phase machine, and composition.
LIFECYCLE_MODES = ("full", "off")
_DEFAULT_LIFECYCLE_MODE = "full"

# Per-repo flow modes, stored as an optional ``mode`` field in the phase row
# (see ``read_flow_state``). ``workflow`` (the default — an absent/unknown
# ``mode`` reads as workflow) is today's full SDD steering; ``free`` pauses ALL
# workflow steering (orientation, banners, exit gates, transitions, intake)
# while keeping domain-skill composition. Entering free-flow never changes the
# ``phase`` value, so resume returns to exactly the prior phase.
FLOW_MODES = ("workflow", "free")


# ---------------------------------------------------------------------------
# Phase helpers — the store is the only source
# ---------------------------------------------------------------------------
#
# Phase lives in ``sdd_state`` kind ``phase``, one JSON blob per repo. Nothing
# here touches ``.agentalloy/phase``. DuckDB is single-writer, so these helpers
# borrow the service's handle (``process_store``) rather than opening one; a
# CLI process, where nothing is bound, reaches the same row over HTTP.


def _phase_view(project_root: Path) -> DuckDBStateStore | None:
    """The store handle re-scoped to *project_root*'s repo, or ``None``.

    ``None`` means the store is out of reach from this process — not that the
    repo has no phase. Callers distinguish the two.
    """
    from agentalloy.storage.state_store import process_store

    store = process_store()
    if store is None:
        return None
    from agentalloy.api.state_router import _repo_key_for  # noqa: PLC0415

    return store.for_repo(_repo_key_for(str(project_root)))


def _phase_state(project_root: Path) -> PhaseState | None:
    """The full :class:`PhaseState` for *project_root*, or ``None``.

    In-process only. The three projections below (``_read_phase``,
    ``read_flow_state``, ``_read_transitioned_by``) all come off one call to
    this on the proxy path, so a concurrent transition can never hand a single
    turn a mixed view of phase, mode, and actor.
    """
    view = _phase_view(project_root)
    if view is None:
        return None
    try:
        return view.read_phase()
    except Exception:
        logger.warning("state store phase read failed for %s", project_root, exc_info=True)
        return None


def _read_phase(project_root: Path) -> str | None:
    """The active phase for *project_root*, or ``None`` when none is recorded.

    Deliberately store-only, with no HTTP fallback: the signal layer runs
    *inside* the service, which owns the writer handle, so looping back over the
    state API would be the service calling itself. Out-of-process callers (the
    CLI) reach the same row through the HTTP client; task 06
    (``cli-state-required``) makes an unreachable service fatal there rather
    than letting an outage read as a fresh repo.

    (The state HTTP client is named by description rather than by symbol on
    purpose — ``tests/api/test_state_router.py::TestTE2`` greps this module's
    source for that name to prove the in-process path never loops back.)
    """
    state = _phase_state(project_root)
    return state.phase if state else None


def read_flow_state(project_root: Path) -> tuple[str, str | None]:
    """The per-repo flow mode as ``(mode, free_since)``.

    ``mode`` is ``"free"`` only when the phase row carries ``mode: free``;
    anything else (no row, absent field, unknown value) reads as ``"workflow"``
    — the historical behavior. ``free_since`` is the ISO timestamp recorded
    when free-flow was entered (drives the daily reminder), or ``None``.
    Never raises.
    """
    state = _phase_state(project_root)
    if state is not None and (state.mode or "").lower() == "free":
        return "free", state.free_since or None
    return "workflow", None


def _read_transitioned_by(project_root: Path) -> str | None:
    """The session key (if any) that caused the current phase's last transition.

    ``None`` means "don't know" — no session-aware writer recorded an actor for
    this phase (a bare CLI ``phase set`` outside a tracked session, a repo
    predating the field, or nothing has transitioned yet). Callers must treat
    ``None`` as ambiguous, not as evidence the current session caused the
    transition — see :func:`_write_phase_atomic`.
    """
    state = _phase_state(project_root)
    return (state.transitioned_by or None) if state else None


def _write_phase_atomic(project_root: Path, phase: str, *, session_key: str | None = None) -> None:
    """Write *phase* to the store for *project_root*, then re-seed the cursor.

    The write itself is :meth:`DuckDBStateStore.write_phase`, which does the
    read-modify-write inside a transaction and owns the preservation rules the
    file version used to hand-roll: ``mode``/``free_since`` carry forward so an
    auto-transition never silently drops the repo out of (or into) free-flow,
    and ``transitioned_by`` is stamped only on a *real* transition
    (``prev != phase``) — an idempotent rewrite keeps the prior actor, which is
    what lets a *different* session's next turn recognize "the phase moved and
    it wasn't me" (``_boundary_confirm_directives``'s "swept" case).

    ``session_key`` is that actor. The name keeps its ``_atomic`` suffix: the
    row is written under a lease inside one BEGIN/COMMIT, so the atomicity
    guarantee the callers depend on is unchanged — only its mechanism moved
    from ``os.replace`` to the store.

    Raises when no process store is bound. This is called on the proxy's
    auto-advance path, which always runs inside the service; silently dropping
    a phase transition would leave the repo one phase behind with nothing to
    show for it.
    """
    view = _phase_view(project_root)
    if view is None:
        raise RuntimeError(
            "no state store bound in this process — phase writes require the service"
        )
    prev = _read_phase(project_root)
    view.write_phase(phase, actor=session_key)
    # On a real phase transition, SEED the work-item cursor to the first work-item of
    # the new phase (filename order — 01-, 02-, …) so "which task is current" is
    # reliably set without waiting for the agent's first `agentalloy task next`. The
    # cursor is the single source of truth both consumers read: the proxy (Tier 2
    # compose) and the `lessons_recorded` codify gate — neither guesses. A phase with
    # no contracts yet clears the cursor (nothing to seed); `task next` advances it as
    # the agent works down the build fan-out. This replaces the old clear-to-none +
    # newest-by-mtime fallback (mtime is fragile: git checkout/clone reset it). An
    # in-phase idempotent rewrite (prev == phase) leaves a deliberately-set cursor
    # untouched. See B2 in docs/feedback-bcal-run-fixes.md.
    if prev != phase:
        from agentalloy.contracts import first_workitem_id

        # Clear stale scoped cursors from the old phase, then seed the shared cursor
        # (the universal per-phase default any session falls back to; per-session
        # `task start`/`task next` layer scoped cursors on top — see cursor_state_name).
        _clear_all_cursors(project_root)
        seed = first_workitem_id(project_root, phase)
        if seed:
            _write_cursor_atomic(project_root, seed)


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


# Proxy-exclusive cadence keys. These churn on (nearly) every proxied turn, and
# a write inside the repo tree mid-session trips harness file-watchers — Claude
# Code injects a "a background process modified <file>" reminder that reads as
# suspicious to the agent and the user. Only the proxy reads/writes these keys,
# so they can live outside the repo without splitting state with the host CLI
# (which touches only ``cursor``); ``AGENTALLOY_RUNTIME_STATE_DIR`` (set by the
# container run to a path on the persistent data volume) relocates them, keyed
# by the repo's ``/proj`` token. Unset (host CLI, dev, tests) they stay
# repo-local as before.
_RUNTIME_STATE_KEYS = frozenset({"announced", "composed", "banner-turns", "free-reminded"})


def _state_file(project_root: Path, name: str) -> Path:
    """Resolve the backing file for cadence key *name* of *project_root*."""
    runtime_dir = os.environ.get("AGENTALLOY_RUNTIME_STATE_DIR")
    if runtime_dir and name in _RUNTIME_STATE_KEYS:
        from agentalloy.api.proxy_context import encode_proj_token

        return Path(runtime_dir) / encode_proj_token(project_root) / name
    return project_root / ".agentalloy" / name


def _read_state(project_root: Path, name: str) -> str | None:
    """Read a single-line cadence-state file (see ``_state_file`` for location).

    Returns ``None`` when the file is absent, unreadable, or empty. Shared by the
    announce-state (``announced``), the work-item cursor (``cursor``), the
    last-composed cursor (``composed``), and the banner pacer (``banner-turns``) —
    all single-value durable cadence keys. A relocated key falls back to the
    legacy in-repo ``.agentalloy/<name>`` so pre-relocation cadence survives the
    move (the legacy copy is removed on the next write).
    """
    state_file = _state_file(project_root, name)
    legacy_file = project_root / ".agentalloy" / name
    for candidate in (state_file, legacy_file):
        if not candidate.exists():
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip() or None
        except OSError:
            value = None
        if value is not None:
            return value
    return None


def _write_state_atomic(project_root: Path, name: str, value: str) -> None:
    """Atomically write *value* to the cadence key *name*.

    Mirrors ``_write_phase_atomic``: a per-writer temp file + ``os.replace`` so
    the watcher and the async proxy never leave a half-written file when they
    race without a shared lock. For a relocated key, any legacy in-repo copy is
    removed best-effort so per-turn churn in the repo stops immediately.
    """
    state_file = _state_file(project_root, name)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f"{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"{value}\n", encoding="utf-8")
        os.replace(tmp, state_file)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    legacy_file = project_root / ".agentalloy" / name
    if legacy_file != state_file:
        with contextlib.suppress(OSError):
            legacy_file.unlink()


def _clear_state(project_root: Path, name: str) -> None:
    """Remove the cadence key *name* if present (both locations). Never raises."""
    for candidate in {_state_file(project_root, name), project_root / ".agentalloy" / name}:
        with contextlib.suppress(OSError):
            candidate.unlink()


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


def cli_session_key() -> str | None:
    """The current session's id for cursor scoping, from the harness shell env.

    Claude Code exports ``CLAUDE_CODE_SESSION_ID`` (the same UUID it sends the proxy
    as ``x-claude-code-session-id``), so a CLI ``task``/``phase`` write scopes the
    cursor to the exact key the proxy reads back. ``None`` when unset (other
    harnesses, dev, tests) → the shared cursor, i.e. today's behavior.
    """
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() or None


def _read_cursor(project_root: Path, session_key: str | None = None) -> str | None:
    """Read the current work-item cursor, session-scoped when a key is given.

    Reads the scoped file (``cursor.<hash>``) first, then falls back to the shared
    ``cursor``. The value is a contract id relative to ``.agentalloy/contracts/``
    (e.g. ``build/cache-write.md``). ``None`` means no explicit cursor — the proxy
    falls back to the phase's incoming contract.
    """
    from agentalloy.contracts import cursor_state_name

    name = cursor_state_name(session_key)
    if name != "cursor":
        scoped = _read_state(project_root, name)
        if scoped is not None:
            return scoped
    return _read_state(project_root, "cursor")


def _write_cursor_atomic(project_root: Path, cursor: str, session_key: str | None = None) -> None:
    """Atomically set the current work-item cursor (advanced by ``task next``).

    Writes the session-scoped file when a key is known, else the shared one —
    scoped-only, so a session never clobbers another's cursor (Bug C).
    """
    from agentalloy.contracts import cursor_state_name

    _write_state_atomic(project_root, cursor_state_name(session_key), cursor)


def _has_legacy_contract_layout(contracts_root: Path) -> bool:
    """Cheap check for any legacy flat-layout indicator (fast path for the auto
    migrate). True when flat ``*.md`` sit in the contracts root, a legacy
    per-phase dir exists, ``archive/`` holds flat ``*.md``, or ``_superseded/``
    exists. Once migrated, all of these are gone and this returns False."""
    if not contracts_root.is_dir():
        return False
    if any(contracts_root.glob("*.md")):
        return True
    for d in ("intake", "spec", "design", "build", "qa", "ship", "sdd-fast", "add-skill"):
        if (contracts_root / d).is_dir():
            return True
    if (contracts_root / "_superseded").is_dir():
        return True
    archive = contracts_root / "archive"
    return archive.is_dir() and any(archive.glob("*.md"))


def ensure_migrated(project_root: Path) -> int:
    """Auto-migrate legacy flat contracts into the tree on first read.

    Idempotent and cheap: returns immediately when no legacy indicator is
    present (the steady state after one migration). On the first read of an
    unmigrated repo it relocates every placeable contract into
    ``active/<phase>/`` + ``archive/<phase>/`` and rewrites any cursor that
    pointed at a moved file. Best-effort — never raises into the signal path;
    a failure just leaves the repo as-is (the reader then sees the old files as
    before). Returns the number of files moved.
    """
    from agentalloy.contracts import (
        apply_contracts_migration,
        contracts_root,
        cursor_after_migration,
        plan_contracts_migration,
    )

    root = contracts_root(project_root)
    if not _has_legacy_contract_layout(root):
        return 0
    try:
        plan = plan_contracts_migration(project_root)
        if not plan.moves:
            return 0
        done = apply_contracts_migration(plan)
        if not done:
            return 0
        # Follow the moves: rewrite the shared cursor and every scoped cursor.
        names = ["cursor"]
        with contextlib.suppress(OSError):
            names += [f.name for f in (project_root / ".agentalloy").glob("cursor.*")]
        for name in names:
            val = _read_state(project_root, name)
            new = cursor_after_migration(val, done, root)
            if new is not None and new != val:
                _write_state_atomic(project_root, name, new)
        logger.info("auto-migrated %d contract(s) into the tree layout under %s", len(done), root)
        if plan.collisions:
            logger.warning(
                "contracts migration: %d file(s) left in place (destination "
                "already occupied) — run `agentalloy contracts migrate` to review",
                len(plan.collisions),
            )
        return len(done)
    except Exception:
        logger.debug("auto-migrate skipped (non-fatal)", exc_info=True)
        return 0


def _clear_all_cursors(project_root: Path) -> None:
    """Remove the shared cursor AND every session-scoped ``cursor.<hash>``.

    Used on a phase transition (including ``phase set intake``) so a stale
    per-session cursor from the old phase cannot bleed its work-item into the new
    one — the scoped file resolves by filename, not by phase. Never raises.
    """
    _clear_state(project_root, "cursor")
    with contextlib.suppress(OSError):
        for f in (project_root / ".agentalloy").glob("cursor.*"):
            with contextlib.suppress(OSError):
                f.unlink()


def _read_composed(project_root: Path) -> str | None:
    """Read the last-composed cursor from ``.agentalloy/composed``.

    Records the cursor whose Tier 2 (domain) block was last injected. Tier 2
    fires once per work-item: when ``composed != cursor``.
    """
    return _read_state(project_root, "composed")


def _write_composed_atomic(project_root: Path, cursor: str) -> None:
    """Atomically record *cursor* as the last-composed work-item (Tier 2 cadence)."""
    _write_state_atomic(project_root, "composed", cursor)


def _read_banner_turn(project_root: Path) -> tuple[str | None, str | None, int]:
    """Read ``.agentalloy/banner-turns`` as ``(phase, session_key, count)``.

    Stores ``"<phase>\\t<session_key>\\t<count>"`` — the carrier-turn counter that
    paces the per-turn banner (emit once every N turns rather than every turn).
    ``(None, None, 0)`` when absent/unreadable/malformed, which the caller treats as
    a fresh start (count 0 → emit the banner this turn).
    """
    raw = _read_state(project_root, "banner-turns")
    if raw is None:
        return None, None, 0
    parts = raw.split("\t")
    if len(parts) != 3:
        return None, None, 0
    phase, session_key, count_str = parts
    try:
        count = int(count_str)
    except ValueError:
        count = 0
    return (phase or None), (session_key or None), count


def _write_banner_turn_atomic(
    project_root: Path, phase: str, session_key: str | None, count: int
) -> None:
    """Atomically record the banner carrier-turn counter for *(phase, session_key)*.

    Written eagerly at evaluate time (not via the deferred commit seam): the banner is
    best-effort and the commit path is a no-op on quiet/banner-only turns, so a one-off
    miscount on an upstream error is harmless.
    """
    _write_state_atomic(project_root, "banner-turns", f"{phase}\t{session_key or ''}\t{count}")


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

    Queries the store for active intake contracts and returns the newest one's
    ``route`` (``"full"`` | ``"fast"`` | ``"add-skill"``). Best-effort: any
    failure returns ``None``. Never raises.
    """
    try:
        from agentalloy.storage.state_store import open_state_store

        db_path = project_root / ".agentalloy" / "state.db"
        store = open_state_store(db_path)
        contracts = store.list_contracts(phase="intake", status="active")
        if not contracts:
            return None
        # Return the most recently updated contract's route
        newest = max(contracts, key=lambda c: c.get("updated_at", ""))
        return newest.get("route")
    except Exception:
        return None


def _intake_route_hint(project_root: Path) -> str | None:
    """Next-phase hint when leaving intake — the intake contract's ``route`` rules.

    Routing is authoritative on the intake contract's ``route`` field: ``fast``
    selects the compressed ``sdd-fast`` lane, ``full`` (the default) advances the
    linear graph intake → spec. The field is trusted directly — intake's exit gate
    is route-agnostic, so the destination phase composes against whatever work-item
    exists.

    When no intake contract is readable, fall back to the store: the presence of
    an active sdd-fast work-item selects the fast route. Best-effort; any read
    failure falls back to the default full route.
    """
    route = _read_intake_route(project_root)
    if route == "fast":
        return "sdd-fast"
    if route == "add-skill":
        return "add-skill"
    if route == "full":
        return None

    # No readable intake contract: fall back to store-presence (cascade).
    try:
        from agentalloy.storage.state_store import open_state_store

        db_path = project_root / ".agentalloy" / "state.db"
        store = open_state_store(db_path)
        fast_contracts = store.list_contracts(phase="sdd-fast", status="active")
        if fast_contracts:
            return "sdd-fast"
    except Exception:
        pass
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
    session_key: str | None = None,
    store: Any = None,
) -> PredicateContext:
    """Build a ``PredicateContext`` for gate evaluation.

    ``store`` is the in-process ``DuckDBStateStore`` handle. Pass it from callers
    that hold the store directly (compose, proxy_signal). Out-of-process callers
    (CLI, web UI) pass ``None`` and query the store over HTTP instead.
    """
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
        store=store,
        session_key=session_key,
    )
