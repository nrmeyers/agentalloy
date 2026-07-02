"""Serve the built web UI (``frontend/dist``) as a single-page app.

Mounted last in ``create_app`` so every API route wins first; the mount then
catches ``/`` and static assets. The SPA uses hash routing, so serving
``index.html`` at ``/`` is the only fallback needed — no per-route rewrites.

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

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agentalloy import __version__

logger = logging.getLogger(__name__)


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

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="web-ui")
    logger.info("web UI: serving SPA from %s", dist)
