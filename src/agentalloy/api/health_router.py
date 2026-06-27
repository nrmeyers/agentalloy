"""Enhanced health endpoint with dependency checks (NXS-775).

Also exposes ``/readiness`` (container deployment) which reports bootstrap
state by reading filesystem markers written by the container entrypoint
script. ``/health`` answers "is the service up and dependencies healthy";
``/readiness`` answers "is bootstrap complete or still warming up".
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agentalloy.embed_provider import EmbedClient
from agentalloy.lm_client import LMClientError
from agentalloy.storage.ladybug import LadybugStore
from agentalloy.storage.vector_store import VectorStore

router = APIRouter()

# ---------------------------------------------------------------------------
# Health (dependency) models and checker (existing)
# ---------------------------------------------------------------------------

DepStatus = Literal["ok", "unavailable"]
OverallStatus = Literal["healthy", "degraded", "unavailable"]


class DependencyStatus(BaseModel):
    status: DepStatus
    impact: str | None = None
    detail: str | None = None


class LMAssistConfigView(BaseModel):
    """Effective Stage B (LM fragment re-rank) config, for benchmark preflight.

    The service owns this config (env-driven); the ``composed-lm`` benchmark arm
    reads it to fail fast when ``LM_ASSIST`` is not ``arbitrate``."""

    mode: str
    model: str
    keep_threshold: float
    timeout_ms: int


class HealthResponse(BaseModel):
    status: OverallStatus
    dependencies: dict[str, DependencyStatus] | None = None
    lm_assist: LMAssistConfigView | None = None


class HealthChecker:
    def __init__(
        self,
        store: LadybugStore,
        lm: EmbedClient,
        vector_store: VectorStore,
        embedding_model: str,
        *,
        runtime_load_error: str | None = None,
    ) -> None:
        self._store = store
        self._lm = lm
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._runtime_load_error = runtime_load_error

    async def check(self) -> HealthResponse:
        store_ok, tel_ok, embed_ok = await asyncio.gather(
            asyncio.to_thread(self._probe_runtime_store),
            asyncio.to_thread(self._probe_telemetry_store),
            asyncio.to_thread(self._probe_embed_model),
        )

        # NXS-777: reflect startup cache load result
        cache_err = self._runtime_load_error

        # Stage B reranker health (read-only rolling-window read — adds no latency).
        rerank_err = self._probe_reranker()

        deps: dict[str, DependencyStatus] = {
            "runtime_store": DependencyStatus(
                status="ok" if store_ok is None else "unavailable",
                impact="compose and retrieve requests will fail" if store_ok else None,
                detail=store_ok,
            ),
            "telemetry_store": DependencyStatus(
                status="ok" if tel_ok is None else "unavailable",
                impact="trace persistence degraded; runtime requests remain successful"
                if tel_ok
                else None,
                detail=tel_ok,
            ),
            "embedding_runtime": DependencyStatus(
                status="ok" if embed_ok is None else "unavailable",
                impact="semantic retrieve and compose will fail; by-id retrieve and read-skill remain available"
                if embed_ok
                else None,
                detail=embed_ok,
            ),
            "runtime_cache": DependencyStatus(
                status="ok" if cache_err is None else "unavailable",
                impact="compose and retrieve requests will fail; restart required to reload active data"
                if cache_err
                else None,
                detail=cache_err,
            ),
            "reranker": DependencyStatus(
                status="ok" if rerank_err is None else "unavailable",
                impact="Stage B fragment re-rank is timing out; falling back to deterministic selection"
                if rerank_err
                else None,
                detail=rerank_err,
            ),
        }

        # A failing reranker degrades (Stage B fails open to deterministic
        # selection) — it never makes the service unavailable.
        if store_ok is not None or cache_err is not None:
            overall: OverallStatus = "unavailable"
        elif embed_ok is not None or tel_ok is not None or rerank_err is not None:
            overall = "degraded"
        else:
            overall = "healthy"

        from agentalloy.retrieval.lm_assist import load_config as _lm_config

        cfg = _lm_config()
        lm_view = LMAssistConfigView(
            mode=cfg.mode.value,
            model=cfg.model,
            keep_threshold=cfg.keep_threshold,
            timeout_ms=cfg.timeout_ms,
        )

        return HealthResponse(status=overall, dependencies=deps, lm_assist=lm_view)

    def _probe_runtime_store(self) -> str | None:
        try:
            self._store.scalar("RETURN 1")
            return None
        except Exception as exc:
            return str(exc)

    def _probe_telemetry_store(self) -> str | None:
        try:
            # DuckDB ``composition_traces`` lives in the same VectorStore.
            # A successful count() proves the table is open and queryable.
            self._vector_store.count_traces()
            return None
        except Exception as exc:
            return str(exc)

    def _probe_embed_model(self) -> str | None:
        """FastFlowLM hides the embedding slot from /v1/models, so we probe by
        actually embedding a short string. A 1024-dim (or any non-empty) result
        proves both the endpoint and the model are responsive."""
        try:
            vectors = self._lm.embed(model=self._embedding_model, texts=["health"])
            if not vectors or not vectors[0]:
                return f"embed model {self._embedding_model!r} returned empty vector"
            return None
        except LMClientError as exc:
            return str(exc)
        except Exception as exc:
            return str(exc)

    def _probe_reranker(self) -> str | None:
        """Detail string when Stage B's recent outcomes are timeout/error-dominant,
        else None. Read-only: it queries the scorer's process-local rolling outcome
        window (updated by the scorer itself), so it never makes a live reranker
        call and adds no latency to /health. Returns None when Stage B is disabled
        (no attempts ever run) or the window has no failing majority. Any unexpected
        error is swallowed — the probe must never break /health."""
        try:
            from agentalloy.retrieval.lm_assist import reranker_status

            return reranker_status()
        except Exception:  # pragma: no cover — defensive; probe must not raise
            return None


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and dependency readiness",
)
async def health(request: Request) -> HealthResponse:
    checker: HealthChecker | None = getattr(request.app.state, "health_checker", None)
    if checker is None:
        return HealthResponse(status="healthy")
    return await checker.check()


# --------------------------------------------------------------------------- #
# Readiness — container bootstrap state                                       #
# --------------------------------------------------------------------------- #

ReadinessStatus = Literal["ready", "warming_up", "error"]

# Stale-lock threshold: if the bootstrap lock file's mtime is older than this,
# we treat the previous bootstrap as crashed. The design picks 2 hours because
# full pack ingest + re-embed takes 15-25 minutes in practice; 2h gives ample
# headroom while still surfacing genuinely-stuck containers.
_STALE_LOCK_SECONDS = 2 * 3600


class ReadinessResponse(BaseModel):
    status: ReadinessStatus
    progress: dict[str, Any] | None = None


class ReadinessChecker:
    """Maps filesystem state to a readiness status.

    The container entrypoint script writes four markers:
      - ``.bootstrap-lock``         — bootstrap in progress (mtime = start)
      - ``.bootstrap-complete``     — bootstrap finished
      - ``.bootstrap-progress``     — atomic JSON snapshot of progress
      - ``.bootstrap-checkpoints``  — per-pack checkpoint log (JSONL)

    Decision order (highest priority first):
      1. ``.bootstrap-complete`` exists → ``ready`` (covers crash-after-done).
      2. ``.bootstrap-lock`` exists and is stale → ``error: stale_lock``.
      3. ``.bootstrap-lock`` exists and is fresh → ``warming_up`` with progress.
      4. Neither file exists → ``ready`` (no bootstrap was needed, or this
         service was started outside the container flow).
    """

    def __init__(self, app_dir: Path) -> None:
        self._app_dir = Path(app_dir)

    def check(self) -> ReadinessResponse:
        complete = self._app_dir / ".bootstrap-complete"
        lock = self._app_dir / ".bootstrap-lock"

        if complete.exists():
            return ReadinessResponse(status="ready")

        if not lock.exists():
            return ReadinessResponse(status="ready")

        # Lock file present — decide warming_up vs stale. We prefer the
        # ISO8601 timestamp the entrypoint writes into the file (survives
        # container restarts cleanly) and fall back to mtime if parsing
        # fails. Either way: age compared against _STALE_LOCK_SECONDS.
        lock_age = self._lock_age_seconds(lock)
        if lock_age is None:
            # Couldn't determine age at all — treat as fresh rather than
            # poisoning a live bootstrap with a false stale_lock.
            return ReadinessResponse(status="warming_up", progress=self._read_progress())

        if lock_age > _STALE_LOCK_SECONDS:
            return ReadinessResponse(
                status="error",
                progress={"error": "stale_lock", "lock_age_seconds": int(lock_age)},
            )

        return ReadinessResponse(status="warming_up", progress=self._read_progress())

    @staticmethod
    def _lock_age_seconds(lock: Path) -> float | None:
        """Return seconds since the lock was created, or None if unknown.

        Tries the ISO8601 timestamp inside the file first (the entrypoint
        writes ``date -Iseconds > .bootstrap-lock`` so this is the canonical
        source); falls back to mtime when the content is missing or
        unparseable.
        """
        try:
            content = lock.read_text().strip()
        except OSError:
            content = ""
        if content:
            try:
                # ``fromisoformat`` accepts the ``%z`` offset produced by
                # ``time.strftime("%Y-%m-%dT%H:%M:%S%z")`` and by ``date -Iseconds``.
                parsed = datetime.fromisoformat(content)
            except ValueError:
                parsed = None
            if parsed is not None:
                now = datetime.now(parsed.tzinfo) if parsed.tzinfo is not None else datetime.now()
                return (now - parsed).total_seconds()
        try:
            return time.time() - lock.stat().st_mtime
        except OSError:
            return None

    def _read_progress(self) -> dict[str, Any]:
        """Return progress dict; falls back to zeroed counts on any failure.

        Returning zeros (rather than ``{}``) lets callers render a stable
        "0 / N packs" line without a None-check; the host-side wait loop
        treats this as "no signal yet, show elapsed time" anyway.
        """
        default = {"packs_ingested": 0, "packs_total": 0}
        progress_file = self._app_dir / ".bootstrap-progress"
        try:
            raw = progress_file.read_text()
        except OSError:
            return default
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return default
        if not isinstance(parsed, dict):
            return default
        # Merge so missing keys still surface defaults — atomic writes can
        # still race with a reader if a writer is mid-rename, and a partial
        # dict is more useful than a wholesale fallback.
        merged = dict(default)
        merged.update(parsed)
        return merged


@router.get(
    "/readiness",
    summary="Container bootstrap readiness (ready / warming_up / error)",
)
async def readiness(request: Request) -> ReadinessResponse:
    from fastapi.responses import JSONResponse as _JSONResponse  # noqa: PLC0415

    checker: ReadinessChecker | None = getattr(request.app.state, "readiness_checker", None)
    if checker is None:
        # No checker wired (e.g. native deployment) — service is ready by
        # definition; there is no bootstrap to wait on.
        return ReadinessResponse(status="ready")
    # Filesystem stat is cheap, but run off the event loop to keep the
    # endpoint non-blocking under load.
    result = await asyncio.to_thread(checker.check)

    # Degraded mode: bootstrap completed but the runtime cache failed to load
    # (e.g. "Table Skill does not exist" — corpus unusable). Report 503 with
    # the reason so the installer polling loop surfaces it to the user instead
    # of silently reporting the container as ready when the corpus is broken.
    if result.status == "ready":
        runtime_load_error: str | None = getattr(request.app.state, "runtime_load_error", None)
        if runtime_load_error is not None:
            body = ReadinessResponse(
                status="error",
                progress={
                    "error": "corpus_unavailable",
                    "detail": runtime_load_error,
                },
            )
            return _JSONResponse(  # type: ignore[return-value]
                status_code=503,
                content=body.model_dump(),
            )

    return result
