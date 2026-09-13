"""Serve the built web UI (``frontend/dist``) as a single-page app.

Mounted last in ``create_app`` so every API route wins first; the mount then
catches ``/`` and static assets.  History-based routing (no hash) means any
non-API path must serve ``index.html`` so the client router can resolve it.

Resolution order for the dist directory: ``AGENTALLOY_WEB_DIST`` env override,
then the repo-layout ``<repo>/frontend/dist`` (dev checkouts), then the
version-matched downloaded bundle at
``~/.local/share/agentalloy/web-dist/<version>/`` installed by
``agentalloy pull-web``. When none exists, ``/`` answers 501 with instructions
instead of a bare 404 — the API surface is unaffected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agentalloy import __version__

logger = logging.getLogger(__name__)

# Prefixes that are API routes — never serve index.html for these.
_API_PREFIXES = (
    "/api/",
    "/code/",
    "/state/",
    "/telemetry/",
    "/diagnostics/",
    "/contracts/",
    "/skills/",
    "/retrieve",
    "/compose",
    "/health",
    "/readiness",
    "/corpus/",
)


def _user_data_dist() -> Path:
    # Deliberately duplicates the XDG resolution in config.py so the runtime
    # service keeps zero dependency on the install module.
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentalloy" / "web-dist" / __version__


def _dist_dir() -> Path | None:
    override = os.environ.get("AGENTALLOY_WEB_DIST")
    if override:
        p = Path(override)
        return p if (p / "index.html").is_file() else None
    repo_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if (repo_dist / "index.html").is_file():
        return repo_dist
    pulled = _user_data_dist()
    return pulled if (pulled / "index.html").is_file() else None


def _is_api_path(path: str) -> bool:
    """True when the path belongs to the API surface, not the SPA."""
    if path == "/":
        return False
    return any(path.startswith(prefix) or path == prefix.rstrip("/") for prefix in _API_PREFIXES)


def mount_web_ui(app: FastAPI) -> None:
    """Mount the SPA at ``/`` if a build exists; otherwise register a 501 hint."""
    dist = _dist_dir()
    if dist is None:

        @app.get("/", include_in_schema=False)
        async def _web_ui_unavailable() -> JSONResponse:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "web_ui_not_built",
                    "detail": (
                        "No web UI bundle found. Run `agentalloy pull-web` to download "
                        "the prebuilt bundle (or `pnpm install && pnpm build` in "
                        "frontend/ on a dev checkout, or set AGENTALLOY_WEB_DIST), "
                        "then restart the service. The API is unaffected."
                    ),
                },
            )

        logger.info("web UI: no bundle found (run `agentalloy pull-web`) — serving API only")
        return

    # Mount static assets under /assets so they resolve before the catch-all.
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="web-ui-assets")

    dist_resolved = dist.resolve()
    index_html = str(dist / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def _spa_catchall(request: Request, full_path: str) -> FileResponse | JSONResponse:
        path = f"/{full_path}" if full_path else "/"

        # Serve existing static files (favicon, manifest, etc.).
        # Resolve and require containment: Starlette decodes %2e to "."
        # before routing but leaves literal ".." segments intact, so an
        # unvalidated join would escape dist (unauthenticated file read).
        if full_path:
            resolved = (dist / full_path).resolve()
            if resolved.is_file() and resolved.is_relative_to(dist_resolved):
                return FileResponse(str(resolved))

        # API paths should never reach here (FastAPI routing handles them),
        # but guard anyway.
        if _is_api_path(path):
            return JSONResponse(status_code=404, content={"detail": "not found"})

        # Everything else → index.html (SPA handles routing)
        return FileResponse(index_html)

    logger.info("web UI: serving SPA from %s (history-based routing)", dist)
