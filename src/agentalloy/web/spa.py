"""Serve the built web UI (``frontend/dist``) as a single-page app.

Mounted last in ``create_app`` so every API route wins first; the mount then
catches ``/`` and static assets. The SPA uses hash routing, so serving
``index.html`` at ``/`` is the only fallback needed — no per-route rewrites.

Resolution order for the dist directory: ``AGENTALLOY_WEB_DIST`` env override,
then the repo-layout ``<repo>/frontend/dist``. When neither exists (core
install without the web build), ``/`` answers 501 with build instructions
instead of a bare 404 — the API surface is unaffected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


def _dist_dir() -> Path | None:
    override = os.environ.get("AGENTALLOY_WEB_DIST")
    if override:
        p = Path(override)
        return p if (p / "index.html").is_file() else None
    repo_dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    return repo_dist if (repo_dist / "index.html").is_file() else None


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
                        "No frontend build found. Run `pnpm install && pnpm build` in "
                        "frontend/ (or set AGENTALLOY_WEB_DIST to a built dist dir), "
                        "then restart the service. The API is unaffected."
                    ),
                },
            )

        logger.info("web UI: no frontend/dist build found — serving API only")
        return

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="web-ui")
    logger.info("web UI: serving SPA from %s", dist)
